/**
 * 【订单详情】页的 **layui 复刻页 + 接真后端**（对标物 `/Order/OrderView`，证据件渲染）。
 *
 * ⚠️ **挂载顺序**（别调换）：`await bootstrapIsland(...)` 挂样式 → 取证据件 → `root.innerHTML`
 * → `initIslandElements(layui)`。顺序反了会先画出没穿样式的骨架。
 *
 * ## ⚠️ 已知会不同 / 未接（逐条给理由）
 *
 * | # | 项 | 处置与理由 |
 * |---|---|---|
 * | 1 | 页头「订单号」的值 | 取 `source_order_no`，**为空回落 `order_no`**。对标那一格渲染的是 **19 位平台单号 `jionOrder`**（`<title>` 用的也是它，与 URL 参数**不是同一个值**）。`jionOrder` 是 19 位来源单号，对上 `source_order_no`；URL 里的 `orderId` 是 20 位系统单号。绩效页正文同样取来源单号，为空就留空；本页页头为空才回落系统单号 |
 * | 2 | `viewType` | **忽略**。对标 `?viewType=10/20/30/40/50` **五取值渲染逐字节相同**（结构文档 §1.1 实测）⇒ 收下但不读；证据件里的 `#viewType value="20"` 照对标原文留着 |
 * | 3 | 左列 **15 颗按钮** | **不做**（业务方原话「左侧的按钮都不需要」），只留 3 颗：刷新本界面 / 复制订单内容 / 继续拆分订单。逐条清单在证据件头部注释里。⚠️ 业务方另行点名的客户群聊 / 打手进客户群聊码 / 通知玩家提供上号验证码，与对标自己就是死按钮的「上传登号验证二维码」，也一并删了 |
 * | 4 | 游戏账号 / 登录密码区的 `复制` / `取扫码链接` / `更改密码` | **不渲染**（业务方「直接展示即可」）；两个值 span 保留 |
 * | 5 | `登录密码` / `暗号/口令` 两行 | **恒空**。我方订单表**没有密码列**（`apps/business/models.py` 的 `Order` 无 `pwd`/`password`；两路证据：模型 grep 零命中 + 证据件该行按钮已删） |
 * | 6 | `订单计时`（`1天0小时0分钟` + `还剩`） | **时长格恒空**。我们**没有结构化时效**；`time_requirement` 是**时间要求（自由文本）**，与「已用时 / 剩余时长」不是一回事，拿它填这一格就是编数据，本页**不显示它**（业务方「完全照对标」：追加卡片已整体删除）。⚠️ 该行第 2 格的**「还剩」是对标原文的字面量**（`<span class="left">还剩<span …></span></span>`），**保留不动**——填值逻辑整行都不写 |
 * | 7 | `当前接手` | **恒空**。对标那一格是**接手的工作室名**；我方接单人挂在**绩效行**（`Performance.user`）上、**不在订单详情载荷里** |
 * | 8 | `指定电话` | **恒空**。我方没有这一列（对标是「指定上号电话」，服务端按权限渲染）。⚠️ 该行的 span 是对标原文的 `class=""`，**没有** `.left`，所以填值逻辑不会去碰它 |
 * | 9 | `订单内容`（`textarea#desc`） | **恒空**。对标那个框的真实来源是「提取虚拟电话」的回调（结构文档 §12.7：实测两单都是空），不是订单正文；我方订单正文 `content`（客户原始需求）因业务方裁定「完全照对标」（追加卡片已整体删除）在本页**不显示**（对标 `#desc` 实测为空，属预期差异） |
 * | 10 | `联系电话` | **留空、不映射**。对标那一格装的是**虚拟电话**（「提取虚拟电话」的回调写入）；我方 `customer_phone` 是**客户电话**，同名不同义（E-0068 的教训：别按标签映射）⇒ 业务方裁定「完全照对标」不显示。我方**没有虚拟号**（不自建电信能力，`AGENTS.md` §4），**本页没有任何一处显示虚拟号** |
 * | 11 | `角色信息` 的后 3 格 | **恒空**。只填第 1 格（`game_role_name`）；对标实测后三格为空、语义未考证 |
 * | 12 | `游戏区服` 的后 3 格 | **恒空**。只填第 1 格（游戏名）与第 2 格（`/` + `server` 名）；对标本单是 `三角洲行动` `/全区` `/全服` `/全部` `/全部`，后四格**语义未考证**（【推断】第 2 格是区服），只认我们真有的那个 |
 * | 13 | `代练项目` 的绿色徽章 | 文案 = `order_type` 的显示值（`代练` / `陪玩`）——对标那格是 `layui-badge layui-bg-green` 的「代练」，我方 `OrderType` 两取值逐字相同（【推断】同义）。取值为空时**整个徽章元素移除**，不留绿色空壳 |
 * | 14 | `订单状态` 的颜色 | **固定 `#289f16`**（= 对标「已完成」那一格的实测值）。对标那格的颜色由服务端按状态渲染，而我们只观测到这一个取值 ⇒ 不猜别的色值（D-0025：视觉值必须从对标产物取）。如实登记：recon 抓过的另一单（状态 `代练中`，单号不回显——原始记录只在**不进 git** 的 `.tools/recon/`）**没有记状态色** ⇒ 写死 `#289f16` 在**非「已完成」**状态上可能与对标不一致，**无凭据不猜色**（D-0025；⛔ 不为补色去重扒对标） |
 * | 15 | 「我方字段」追加卡片（**已删**） | 业务方裁定**完全照对标** ⇒ 该卡片**已整卡删除**（`extraCardsHtml` 及其调用、`extraGroups` / `RECORD_GROUP` / `MAPPED_FIELDS` 全删）；对标左列只有那 16 行，没有我方多出来的行 |
 * | 16 | 追加卡片里的空值 / 金额 / 时间（**已随 #15 作废**） | 追加卡片已整卡删除，本条随之作废 |
 * | 17 | 追加卡片里的 4 个 FK（**已随 #15 作废**） | 追加卡片已整卡删除；四个配置接口（`@/api/config`）与 `NameDicts` **仍保留**——「对标行」的游戏 / 区服 / 分类 / 项目要用 |
 * | 18 | `<title>` | `<页头单号>订单详情`（照对标 `<19位平台单号>订单详情` 的形状） |
 * | 19 | 两块日志的**内容** | **已接真**：`#logdiv` 与 `#tableList_actionlog` 取 `GET /orders/<单号>/audit/`（两块同源、一次取数），「添加」走 `POST /notes/`；`复制订单内容` / `继续拆分订单` / `刷新本界面` 的接线见第 31–33 条。有意偏离逐条见第 20–29 条 |
 * | 20 | `操作日志`表的取数方式 | **不走 layui 的 `url`**（对标是 `url: '/OrderAjax/GetSysmteLogByOrderList?orderId='+orderId`）：走 `url` 就绕开了我们的统一信封、CSRF 与错误映射（house 口径），而且读码可知**只要 config 里还有 `url`，`reloadData(id, {data})` 就走 ajax 分支、忽略我们传的 `data`**（先例 `system-sourelist.ts:448-463`）⇒ 用 `data:` 模式：页面自己 `fetchOrderAudit()` 一次，两块日志共用这份数组 |
 * | 21 | 订单日志行的**文本来源** | 对标渲染它自己的业务事件流文本（`GetOrderLogs` 的 `Note`，如「【打手A】将订单录入系统」）；我们渲染**我方 `summary`**（如「添加订单日志：xxx」）——本仓**没有**那条独立事件流模型（计划 §2.2 的两路否定证据），两块日志同源。⚠️ 句子的拼接**只在服务端**（`OperationLog.summary`，spec §5.9 硬规矩），前端一个字都不拼、也不做对标那句 `.replace("添加了订单备注","")` |
 * | 22 | 订单日志行的**灰色恒为 `#999`**（不做染色分档） | 对标的红 `#E03636` / 橙 `#F3723A` 是按它自己的 `Note` 关键词命中分档的（recon §7.3）；我们的 `summary` 没有那套关键词语义 ⇒ 凭猜染色会给出**错的分档**、不做。默认灰（对标未命中时的取值）逐字保留 |
 * | 23 | **不做**行内「回复」按钮、不做**行丢弃**规则 | ① 「回复」与其通道 `POST /OrderAjax/ReplyOrderCustomerMsg` 是「回复客户消息」功能，我们没有那条通道（计划 §2.2 的**有意不做**）；② 行丢弃规则命中的是「的系统里一键发送到了本系统中」——那是**上下家同步**概念，我们不做上下家（同上） |
 * | 24 | 订单日志行用 **DOM 文本节点**而不是拼 HTML | 对标是 `'<p …>【'+createTime+'】'+Note+reBtn+'</p>'` 直接拼串（recon §7.1）。我们的 `summary` 含**用户输入的日志内容**（`order.note` 模板 =「添加订单日志：<内容>」）⇒ 照抄拼串就是一条 XSS。按标准 §5「对标自己的缺陷不复刻」用 `textContent` 渲染；DOM 形状（标签 / class / 内联样式）逐字保留 |
 * | 25 | **2 分钟自动刷新**照做 + **卸载时 `clearInterval`** | 对标 `setInterval(getOrderLogs, 1000*60*2)`（recon §7.1）⇒ `LOG_REFRESH_MS = 2*60*1000`（120000），到点重新取 `/audit/` 并**整块重绘**两块。⚠️ **有意偏离**：对标的定时器**从不清理**（它是整页刷新），我们在 iframe 里挂载 / 卸载 ⇒ `mount` 的卸载函数里必须 `clearInterval`，否则每进出一次就多一个定时器（泄漏、并让已卸载页面继续发请求） |
 * | 26 | 「添加」成功后**两块同源同步** | 对标的两块日志来自**两个端点** ⇒ 它添加后只 prepend `#logdiv`、操作日志表**不刷新**；我们的两块**同源**（同一个 `/audit/`）⇒ 只 prepend 一处会让同一屏两个页签自相矛盾。做法：`#logdiv` prepend 新行的**同时**把同一行 prepend 进操作日志表的**本地数据**（`table.reloadData(id, {data})`，**不再发第二次请求**） |
 * | 27 | 空内容判据比对标**严一档**（纯空白也拦）+ 空内容提示停 **1000ms** | 对标 `if (data.field.remark == "")` 只拦空串、纯空白会发给服务端（`layer.msg('备注内容不能为空！', { time: 1000 })`）；我们按 `.trim() === ''` 拦（零请求 + 同一句原文）。后端 `OrderNoteSerializer` 对纯空白也是 `1000`（`validate_content` 的 `strip()`）⇒ 拦下来只是**省一次往返**、不改语义。失败提示仍走本页的 `errMsg`（6000ms） |
 * | 28 | 「添加」成功**不弹提示** | 对标弹服务端 `msg`（`layer.msg(sdata, {time:1000})`）；我们的 `request<T>()` 成功只回 `data`（`msg` 只在失败时经 `BizError.message` 出来）⇒ 成功路径**拿不到那句话**。⛔ 不自造文案（标准 §5 的写操作规矩）⇒ 成功反馈 = 新行出现 + 输入框清空 |
 * | 29 | `created_at` 要**按偏移换算**才能显示对的墙钟 | `GET /orders/<单号>/audit/` 的 `created_at` 是 **UTC**（尾部 `+00:00`，实测；兄弟端点 `/api/audit/logs/` 走 DRF 才是 `+08:00`）⇒ 照 `api/auditLogs.ts::formatCreatedAt` 直接切字符会**整整差 8 小时**。本页用 `formatLogTime` 按串里自带的偏移换算到项目 `TIME_ZONE`（`Asia/Shanghai`，固定 +08:00）。实测证据见该函数的注释 |
 * | 30 | 「添加」成功后 prepend 的那一行，对标用的是**另一个模板**，我们没有逐字分两套 | 对标添加成功后走的是**另一份拼串模板**（`orderview.js:168-176`）：`<p class="linespan" style="line-height:28px;margin-left:5px;">【M-D H:mm】remark</span>`——① **没有 inline 颜色**（落到页内 `<style>` 的 `.linespan{color:#808080}`，而首屏那些行自带 inline `color:#999`）；② 时间是**浏览器本地时钟现取、不补零**（`myDate.getMonth()+1` / `getDate()` / `getHours()` / `getMinutes()`）<br>我们**有意不拆成两套模板**：prepend 与首屏整块重绘都走同一个 `buildLogLine`（inline `color:#999` + 服务端 `created_at` 按偏移换算到 +08:00 + 补零）。**理由**：浏览器本地时钟会让**同一次取数**的两行显示不同时间（首屏那几行来自服务端 `created_at`）、且**随用户时区变**（我们固定按项目时区 `Asia/Shanghai`，见第 29 条）⇒ 逐字复刻等于引入「同一份数据两个时钟」。⚠️ 本条与第 26 条（两块同源）出自同一次「不自相矛盾」判断 |
 * | 31 | ⭐ **`/orders/:order_no(\d{20})` 路由页换成同一份复刻 + 旧 EP 详情页删除** | 组件从 `views/orders/OrderDetailView.vue` 换成 `LayuiIslandHost`（`meta.islandPage = 'order-orderview'`），宿主把路由参数逐个拼进 iframe query（`LayuiIslandHost.vue::islandSrc`）⇒ 本页仍从 `window.location.search` 读 `order_no`。⛔ **`OrderDetailView.vue` 与 `views/orders/__tests__/OrderDetailView.spec.ts` 已删除**（业务方裁定：**不留两套长得不一样的订单详情**）。⚠️ **随页移除 3 个动作**（改价 / 状态流转 / 取消退单）——它们仍可从**订单管理页**做到（改价在列表的「改价」入口、改状态在列表的「更改订单状态」）。`meta.title` 仍未设（页签回退到 `route.name`），保持路由既有行为 |
 * | 32 | ⭐ **「刷新本界面」不接线、证据件原文一字未改** | 那颗 `<a>` 自带 `href="javascript:location.reload(true);"`（证据件 L276 逐字）。在 island **iframe** 里点它 = **重载本页 iframe ⇒ 重新取数**（订单详情 + 两块日志都重取），语义与对标一致（对标也是整页 `location.reload(true)`）。⇒ **不加任何 JS**；⛔ 不要为了「统一」把它改成 `void(0)` 再挂处理器（那会改掉对标的标记）。判据：`order-orderview-buttons.spec.ts` 按**字面 `href`** 钉住它仍然在，真浏览器点击后观察重新取数 |
 * | 33 | ⭐ **「复制订单内容」的文本是**我们自拼的整单文本**（业务方拍板）** | 对标那一颗是 clipboard.js 声明式：`data-clipboard-text` **实测为空串**（recon §9；它只在已删的「提取虚拟电话」回调里才被写入）⇒ 对标点它**复制到的是空内容**、却仍提示成功。我方按业务方拍板改成**自拼文本**：`buildCopyText(root)` 读**本页已经渲染出来的那些行**（`STANDARD_ROW_LABELS` 的标签 + 该行 `span.left` 的显示值，一行一条、`\n` 连接）⇒「复制到的」与「屏幕上看到的」必然同源，不引入第二份字段口径。出口 = layui 自带的 `layui.lay.clipboard.writeText({text, done, error})`（vendored 2.13.9 实有；签名读码 + 真浏览器探针双证——⚠️ **不是** `layui.util.clipboard`：`util` 模块的键里没有 `clipboard`）。提示逐字照对标：成功 `已成功复制订单内容到剪贴板` / 失败 `复制订单内容失败了，请手动复制`（后者第二参 `'error'` 也照对标）。⚠️ `#copybtn` 的 `data-clipboard-text` **保持证据件的空串**（不改标记）。⛔ **有意不做**：对标成功回调里还会 `POST /OrderAjax/CopyOrderLog`（复制留痕）——我方**没有那条端点**，且本页只负责复制 ⇒ **零请求** |
 * | 34 | ⭐⭐ **「继续拆分订单」已接线（业务方拍板）** | 点 `.spliteorder` ⇒ `layer.prompt` 收「每行一个代练项目」⇒ 真发 `POST /api/business/orders/<单号>/split/`（**推断级设计**：对标那个弹层零抓件，见常量处的说明）⇒ 成功后提示 + 重取本页。⚠️ 那颗 `<a>` 的 class / `href` / `lay-tips` 仍照证据件原样 |
 * | 35 | ⭐ **游戏账号 / 登录密码区「只展示」** | 「复制」/「取扫码链接」/「更改密码」三颗按钮已删、两个值 span 保留（第 4 条）。复核判据：证据件里那三颗 `a.copyacc` / `a.copysmurl` / `a.copypwd` **不存在**（由 `order-orderview.spec.ts` 的「15 颗按钮都不在」钉住），`登录密码` / `暗号/口令` 两行的 `span.left` 仍在且值为空 |
 * | 36 | ⭐ **新增外层「代练通」页签（业务方要求；静态复刻骨架，动作其后接真——见第 37 / 41–52 条）** | 外层页签从对标的 1 个（`本系统订单`）变成 **2 个**（多一个 `代练通`），面板内再分 `交易平台订单信息` / `截图` 两个子页签。**逐条如实登记**：<br>① 对标那个平台页签是**前端按 `GetPlatfromOrderIdList` 的结果动态 `tabAdd`** 的（`orderview.js:1005-1032`），本版没有抛单链路 ⇒ **静态固定一个「代练通」**（标题就是平台名，不带对标的 `[接]` / `(历史)` 后缀——那两种后缀由平台单状态决定，无数据不猜）；<br>② 面板标记**逐字照** `platfromorderview.html` L154-421（标签 / class / 内联样式 / `lay-tips` / 默认 `display:none`），但**值一律留空**（⛔ 不拿 `0.00` / 假账号冒充；对标那些值是服务端按真实平台单渲的）；<br>③ 只为**另一个 iframe 页**才有的外壳不搬：`<form>`、5 个隐藏域、`.applyPlatorderRevokeContent`（撤销申请弹层）——本版没有那些动作；<br>④ 所有动作按钮（补款 / 改价 / 从平台撤回 / 确认完单 / 提交异常 / 申请撤销 / 取消撤销 / 同意撤销 / 申请仲裁 / 取消仲裁 / 补时 / 修改密码 / 锁定 / 解锁 / 拉黑 / 解黑 / 发送 / 底部 6 颗）**点了只提示「尚未开放」、零请求**；唯一例外是「点我刷新当前页」——它自带 `href="javascript:location.reload();"`，保留原生 reload（同第 32 条）；截图上传区点了提示「截图上传」尚未开放；<br>⑤ 平台留言 / 截图列表 / 议价表（`#yjTableList`）**恒空**：不取 `GetTransctionOrderMessageList` / `GetTransctionOrderImgList` / `GetBargainingRecordList`（都属抛单链路）——其后留言 / 进度图已接真（第 43 条），议价表仍不取。<br>⚠️ 对标那些接口的**成功码不是 `code==0`**（留言是 `5`、详情/截图是 `1`，实测见 `pushjdp-configured-recon.md` §6.8）——将来接线时按端点记，⛔ 别写死 `code==0` |
 * | 37 | ⭐⭐ **「代练通」页签接真（取代第 36 条的静态复刻口径）** | 后端 T10a 读面 + T10b 四个动作已上线，本页从「静态复刻」升级：<br>① **切到「代练通」页签时**取一次 `GET /api/business/orders/<单号>/platform-push/`（挂载时不取）；有推送行填 **10 格**（平台单号 / 发单平台 / 订单价格 / 安全保证金 / 效率保证金 / 要求时限 / 交易状态 / 撤销状态 / 最近同步 / 失败原因）；没有 ⇒ 空态「本单尚未发到交易平台」+「去发单」；取数失败 ⇒ `layer.msg` 后端原文 **+ 面板里一行可见的「加载失败：…」**（⛔ 不静默空白）。<br>② **四颗动作按钮接真**：`从平台撤回`（`layer.confirm`，文案写清「未接手 = 删除并即时退款 / 已接手 = 协商撤销」）/ `改价`（`layer.prompt`，>0 且最多两位小数）/ `补时`（`layer.prompt`，正整数）/ 留言「发送」（`layer.prompt` 文本域，空内容零请求）。四颗都在提交中加 `layui-btn-disabled` + 在途闸（连点只发一次），成功后**重新取数**再提示；失败原样弹后端中文原文。**没有推送行时**点这四颗 ⇒ 只提示「本单尚未发到交易平台，请先发单」、零请求（不进弹层）；另外面板按**四态**（还没取 / 读取中 / 有行 / 读取失败）区分提示——读取中提示「正在读取，请稍候」、读取失败提示「读取失败，请刷新后重试」，⛔ 不许把「加载失败」的页面说成「没有推送行」；**弹层开着时连点同一颗按钮只开一层**（补时是增量，第二个弹层确认后会重复加时）。<br>③ 还没接真的按钮继续只提示「尚未开放」、零请求。`bindPlatformNotOpen` 按 `PLATFORM_WIRED_SELECTOR` 放过已接真的，其余照旧拦下。确认完单、截图上传、编辑与撤回见第 44 条。<br>④ **我方追加的两行**：`最近同步` / `失败原因`——对标那张平台详情页**没有**渲染它们（`platfromorderview.html` 的字段表里没有），但推送行有这两个值、对账要用 ⇒ 作为**多出来的行**如实登记（`失败原因` 带 `color:#ff0000`）。<br>⑤ **空态「去发单」**复用订单列表那颗地球按钮的打开方式（`layer.open` 装 `order-pushjdp` 岛页、`order_no` 走 20 位系统单号、标题用展示单号并转义）：弹层参数逐值照 `order-orderlist.ts::openPushJdpLayer`，但**弹层参数在本文件里有一份独立实现**（两个页面各自是独立 island 入口；不抽公共模块是为了不动订单列表那条「开层调用点计数」网）——⚠️ 改一边必须同一次改另一边。<br>⑥ 平台留言 / 截图列表 / 议价表**仍然不取**（那三条是对标侧的抛单链路接口，不是本仓的推送行读面）。 |
 * | 38 | ⭐ **「代练通」页签动作的提示按「真实结果」说** | ① 撤回话术不再复述后端固定 `msg`「撤回已完成」：`state=closed` ⇒「已撤回并退款」；`remote_cancel_status` 进入协商态 ⇒「已提交协商撤销，等打手/客服处理」（`withdrawResultMessage()`，⛔ 只看 HTTP 成功不算）。② 动作失败按 `classifyPushError` 分 `failed` / `unknown`：后端明确拒绝才原样弹中文；网络 / 超时 / 5xx 说「已发出，请刷新核对」并**强制重拉面板**（补时是增量、改价动钱，谎报失败会诱导重复提交）。 |
 * | 39 | ⭐ **「交易状态」的本地状态中文改用后端 `state_display`（优先级其后按第 45 条改准）** | 优先级：`state=closed` ⇒「已关闭」→ 回读到的 `remote_status` 中文 → **后端下发的 `state_display`**。⛔ 本页不再自译第三张本地状态词表；`state_display` 缺失（旧后端 / 形状漂移）时才回落到 `platformPushStatusLabel`——那一条回落登记在这里，不是静默行为。 |
 * | 40 | ⚠️ **动作失败的 `failed` 里混着后端「已提交但没核验到」（如实登记）** | 后端对**显式拒绝**与**已提交但没核验到**都回 `code=1002`（`_service_failure` 固定 `ErrorCode.BUSINESS`），响应里没有可判别字段 ⇒ 前端**不靠中文文案猜分档**（那是脆的）。现行处置：两种失败都**强制重拉面板**并原样显示后端 `msg`（那句话本身已写明「已提交…请核对」或拒绝原因）；`unknown` 档仍只用于网络 / 超时 / 5xx。要真正把「已提交但没核验」升成 `unknown`，需要后端给这类错误单独的业务码（本线未改后端）。 |
 * | 42 | ⭐ **第三组：详情页缺的动作** | 逐条对标 `platfromorderview.html` 的按钮（class 一字未改）：<br>① **补款**（`a.appenddPlatorderPrice`）接真 ⇒ `POST …/platform-push/up-price/`，载荷**恰好** `{up_price}`。⚠️ **有意不同**：证据件那个输入层写「请输入要追加的金额-不能有小数」（它对的是自己只收整数的端点），我方 `UpPriceSerializer` 收两位小数 ⇒ 标题按我方契约写，⛔ 不照抄一句与行为相反的话。另：我方后端加价只允许**未接手**（`Status=11`），对标的「接手后加款」语义未支持（缺口）。<br>② **全平台改价**（`a.reChangePrice`）接真 ⇒ 输入层标题**逐字**照证据件 `changePlatorderPriceAll(20)`，确认后按**平台去重**逐个 `POST …/platform-push/price/`（载荷恰好 `{price}`）。⚠️ 本仓没有「一次改所有平台」的批量端点，且 `price/` 不接受平台参数 ⇒ 今天只可能改代练通一发；多平台需要后端契约（缺口）。<br>③ **重发此单**（`a.repushorder`）接真 ⇒ 落点就是证据件那句 `/order/pushjdp`：**打开发单页弹层**（`page=order-pushjdp`、20 位系统单号），标题逐字「重发平台订单」，本页零 API。⛔ 不在这里自己拼发单载荷——价格 / 双金 / 时限 / 区服 / 门槛的唯一出处是发单页的 `platform-push/preview/`。<br>④ **锁定 / 解锁**改成**可填原因**的输入层（后端 `reason` 可选、上限 128 字）：填了就送 `reason`（trim 后）、没填**不送这个键**；超长页内先挡（与后端同一句中文；取代第 41 条 ② 的「没有收原因」）。<br>⑤ 确认完单、编辑当前 / 全平台订单、截图上传、留言历史 / 进度图都已接上，见第 43–44 条。 |
 * | 43 | ⭐ **「平台留言」历史 + 进度图接真（业务方点名「平台留言功能也没做」）** | 后端读面已就绪（`GET …/platform-push/messages/` / `…/platform-push/progress/`）⇒ 第 37 ⑥ 与第 42 ⑤ 里「留言历史 / 进度图恒空」被本条取代：<br>① **切到「代练通」页签时**并发取推送行 + 两块读面（没有平台单时后端会回「不能做平台读取」——那是预期前提，页面把它收成**空态**，⛔ 不报成读失败）：留言渲染进 `#dltlogdiv`（时间 + 正文；`is_system` 加蓝底「系统」徽标）、进度图渲染进 `.platfromOrderImgList`（时间 / 说明 / `url` 非空才 `<img>`，为空显示「无预览」而不画破图标）。<br>② 两块列表都有**三态**：加载中（`正在加载留言…` / `正在加载进度图…`）/ 空（`暂无留言` / `暂无进度图`）/ 失败（**中文原因 + 「重试」**）。⚠️ 后端读面失败是 **HTTP 200 + `{error: 中文}`** ⇒ `error` 非空也算失败，⛔ 不许当成「没有留言」（否则就是静默空）。<br>③ **「发送」改为直接读面板输入框**（对标 `platfromorderview.js::searchDltmsg` 的口径；原来再开一层 `layer.prompt` 的做法作废）：空 / 纯空白**零请求** + `留言内容不能为空`；载荷**恰好** `{msg}`；提交中禁用 + 在途闸（连点只发一次）；成功**清空输入框 + 重取留言列表**；失败原样显示后端中文（网络 / 5xx 仍说「已发出，请刷新核对」，同其它写动作）。<br>④ 「重试」是**我方追加**的控件（对标无此按钮）：它重取的是**整块面板**（推送行 + 两块读面），因为「读不到留言」也可能是「没有推送行」造成的。 |
 * | 44 | ⭐⭐ **详情页「代练通」补接六件事——确认完单、编辑、截图、留言都在本条** | 后端路由与服务层都已就绪，本版逐条接上：<br>① **确认完单** `POST …/platform-push/finish/`：照证据件只在 `remote_status=13` 露出来；确认框写明「按订单价扣平台余额 + 本系统订单状态不会自动改变」→ 成功后重取面板；失败原样显示后端中文。<br>② **从所有平台撤回** `POST …/platform-pushes/withdraw-all/`：确认文案**逐字**照对标「确定将该订单从所有平台撤回吗？」；返回 `results[]` **逐平台**展示（整单摘要 + 明细弹层），部分成功如实说「部分平台撤回成功（成功 X / 失败 Y）」，⛔ 不把部分成功说成整单成功。<br>③ **编辑当前平台订单 / 全平台编辑订单**：弹窗表单（标题 / 订单说明 / 代练要求 / 区服编码 / 发单频道 / 标签1 / 标签2）；当前平台打 `edit/`、全平台打 `modify/`（后端同一个视图）；**只提交填了内容的项**（载荷恰好），一项都没填 ⇒ 页内提示 + 零请求。⚠️ 读面推送行**不下发**这 7 个字段 ⇒ 弹窗无法预填、也无法区分「要清空」与「没改」（缺口，见报告）。全平台编辑今天只可能作用于代练通（后端只有这一个平台客户端）。<br>④ **补款**改打 `POST …/platform-push/amount/`（载荷恰好 `{amount}`，**整数元 ≥ 1**、代练中 / 待验收都能做）；「加价」仍走 `up-price/`（未接手、两位小数）。前端先挡非整数 / 小于 1。<br>⑤ **进度图点开大图**：有 `url` 的缩略图挂 `.dltProgressImage`，点它 `layer.open` 开大图；`url` 为空仍显示「无预览」（⛔ 不画破图标、也不给空图挂可点 class）。<br>⑥ **截图上传**：点拖拽区 / 拖入文件 ⇒ 运行期补的隐藏 `<input type="file">`；前端先挡大小（18 MB）、图种（按**字节魔数**只收 jpg / png，⛔ 不信 `File.type`）与说明长度（200 字）；`multipart` 送 `image` + **非空**才送 `caption`；上传中禁用 + 在途闸；成功清空说明并**只重取进度图**；失败原样显示后端中文。⚠️ 证据件「图片说明」框 `maxlength="50"`，后端允许 200 ⇒ 页面实际最多 50 字（更严，不是缺口）。<br>⑦ **平台留言历史**：`GET …/platform-push/messages/`。后端没有「已读 / 未读」这一列（也不写库）⇒ 页面不画读态。发送方渲染 `sender`（系统消息保留「系统」徽标、同一句不重复画两遍）。留言响应附带 `images`（来自进度列表），在「平台留言」区块下方按缩略图墙展示、点开看大图，`url` 为空显示「无预览」。<br>⑧ **申请客服介入的可用条件**（真机定案，`docs/DAILIANTONG-API.md` §6.9）：只有「协商撤销中（`CancelStatus=11`）且已满 1 小时」能申请。后端下发 `arbitration_available` + `arbitration_blocked_reason`（含剩余分钟）⇒ 页面**只按这两个字段**决定按钮可不可点、并把原因作为提示，⛔ 不自己算时间；字段缺席（后端未升级）⇒ 保持可点、点了把上游原文如实显示（⛔ 不本地禁用）。<br>⑨ **补款可见性**：后端 `amount/` 在 `remote_status=12`（正在代练）与 `13`（待验收）都允许 ⇒ 页面不按 12 单独收按钮（证据件那颗本来就是常显，状态闸在后端）。 |
 * | 41 | ⭐ **代练通「有、我们没用上」的动作接真** | 本页新增六颗动作按钮，走与第 37 条同一套出口（`runPlatformAction`：在途闸 + 提交中禁用 + 成功重取 + 失败原样显示后端中文）：<br>① **申请客服介入**：`a.applyArbitration` `layer.confirm` 后 `POST …/platform-push/arbitration/`（空载荷）。「取消仲裁」`a.unapplyArbitration` **仍没接**（上游只有「申请」，没有取消动作）。<br>② **锁定 / 解锁**：证据件里那两颗 `a.lockBtn` / `a.unlockBtn` 接真，`POST …/platform-push/lock/`，点哪颗发 `{locked: 1|0}`（原因入参其后补上，见第 42 条 ④）。<br>③ **拉黑打手**：`a.addBlackRnameBtn` 接真，`POST …/platform-push/blacklist/` 载荷 `{flag: 0}`——**UID 由后端从推送行的 `accept_uid` 取**，页面不送（⛔ 页面拿不到也不该猜）。「解除黑名单」`a.removeBlackRnameBtn` **仍没接**（本版只做业务方点名的「拉黑」）。<br>④ **加价**（`a.upPlatorderPrice`）/ **评价打手**（`a.valuePlatorder`）/ **上架**（`a.onSellPlatorder`）/ **下架**（`a.offSellPlatorder`）四颗是**我方追加**——对标那张平台详情页**没有**这四颗（`platfromorderview.html` 里没有任何「加价 / 评价 / 上架 / 下架」按钮）；业务方点名要补，故新增 class 并登记在此。<br>⑤ **可见性按远端状态**（`syncPlatformActionVisibility()`，只在 `fillPlatformRows` 里跟数据一起算；其后按第 46 条放宽）：加价 / 上下架只在 `remote_status=11`（未接手）、评价只在 `17`（已结算）、锁定只在 `12`、解锁只在 `15`、仲裁只在「已接手且未结算且撤销状态不在 14/15」、拉黑只在「已接手且有 `accept_uid`」；没有推送行 / 取数失败 ⇒ 全部收起。<br>⑥ **评价是两步入参**（先满意度码值 10–13、再可留空的评价内容）：`layer.prompt` 一次只收一个值，而让用户直接敲码值又容易错 ⇒ 两步；页码值判据在页内先挡、仍由后端 `ValueSerializer` 兜底。载荷**恰好** `{eval_type, comment}`（内容为空时**不送** `comment` 键）。<br>⑦ 六颗都**没有页内状态机**：状态闸在后端（点了会回中文业务错误），页面只决定「哪颗露出来」；⛔ 不在前端写第二套 `Status` 码表（码表只有 `platformPush.ts` 一处）。 |
 * | 45 | ⭐ **「交易状态」跟着 `CancelStatus` 走（改准第 39 条的优先级口径）** | 真机现场：远端 `Status=16`（撤销中）+ `CancelStatus=12`（协商已处理 = 打手已同意撤销），本地订单已经是「已退单」，本页「交易状态」却一直显示「撤销中」——因为它只读 `remote_status`。<br>① 现在这一格走 `platformPush.ts::platformDisplayStatus`：`closed` ⇒「已关闭」→ **后端下发的 `display_status_label`** → 本地按 `(Status, CancelStatus)` 推导（`12/15/16` ⇒ 已撤销 / 已强制撤销；`11` ⇒ 撤销中；`14` ⇒ 客服介入中；`13` 撤销已取消 ⇒ 按 `Status`）→ 后端 `state_display`（本地状态中文的唯一权威）。<br>② ⛔ **不许只读 `remote_status`**：旧回包缺 `display_status_label` 时也必须一起看 `remote_cancel_status`（三个读面对同一条撤销码必须说同一句话）。<br>③ 「撤销状态」那一格不变（仍逐字取 `REMOTE_CANCEL_STATUS_LABELS` 的码表：协商已处理 / 客服已处理 …）。 |
 * | 46 | ⭐ **`Status=18`「已下架」：码表补一档 + 上下架可见性放宽（改准第 41 条 ⑤ 的可见性口径）** | 真机现场：这张单被**下架**后上游回 `Status=18`（上游码表 `docs/DAILIANTONG-API.md` §5 只有 10–17）。当时的两个症状：① 本页「交易状态」把它显示成**未知码**（`remote_status` 没中文 ⇒ 落到本地状态兜底）；② 后端的上下架闸只认 `Status=11` ⇒ **下架后永远上不了架**（业务方实测被卡；用原始接口 `LevelOrderOffSell(Flag=1)` 实测能上架回 11）。<br>① 码表：`platformPush.ts::REMOTE_STATUS_LABELS` 补 `18: '已下架'`（后端 `dailiantong_actions.py::REMOTE_STATUS_LABELS` 同值，两边各有测试钉字面值）；综合状态侧 `off_shelf` / 「已下架」也补上（后端 `display_status_*` 同值下发）。<br>② 底色：`off_shelf` 取**既有灰档**（对标 `pltmOrderStaRow` switch 没有「已下架」这一档，最接近的是「未接手 / default」的灰）——这一档是**推断**，不新造颜色。<br>③ 可见性（`syncPlatformActionVisibility`）：`18` 时**只露「上架」**（它已不在可接列表里，再下架没有意义、后端下架闸也只放 11）；`18` 也不再算「已接手」⇒ 仲裁 / 拉黑不露。加价仍只在 `11`（上游「只有未接手状态才能修改价格」）。 |
 * | 47 | ⭐⭐ **撤销区按「谁发起 + 撤销状态」决定按钮组合（取代第 36 ④ / 37 ③ / 41 条的「只提示尚未开放」口径）** | 业务方现场：「交易状态：撤销中」旁是「从平台撤回」；「撤销状态：协商撤销中」旁是「申请撤销」「申请仲裁」——**打手主动申请撤销后我们这边没有「同意撤销」**，而我们自己的「申请撤销」在协商撤销中再点也没意义。<br>① 契约（后端另一条线在加，页面按契约写、**缺键时降级**）：`cancel_initiator`（`"taker"` / `"us"` / `""`）+ `agree_cancel_available`（bool）；`arbitration_available` / `arbitration_blocked_reason` 是既有字段。<br>② 显示规则（`syncPlatformActionVisibility`，**撤销区**三颗）：`CancelStatus=10`（未撤销）且远端可协商（`Status` 12–15）⇒ **只露「申请撤销」**（我们发起）；`CancelStatus=11`（协商撤销中）且 `cancel_initiator="taker"` ⇒ **「同意撤销」（主按钮 `layui-btn-danger`）+「申请仲裁」**；`="us"` ⇒ 只露「申请仲裁」并说明「已提交协商撤销，等打手同意」；发起方判不出（空串 / 键缺席）⇒ 两颗都露、点「同意撤销」由后端判并原样显示中文；`CancelStatus ∈ {12,15,16}` ⇒ 两颗都收起、撤销区显示结果文案「撤销已处理：<码表中文>」。`agree_cancel_available === false` ⇒ 不摆「同意撤销」（只认明确 false，键缺席按发起方降级）。<br>③ **「申请撤销」接真**：确认后 `POST …/platform-push/withdraw/`（载荷**恰好**空对象 ⇒ 后端按详情现值支付代练费 / 赔偿保证金），话术按**返回行**判（`withdrawResultMessage`）。<br>④ **「同意撤销」接真**（`a.unRevokeBtn`，证据件原文 class）：确认框写清「**同意后平台会按详情现值支付代练费 / 赔偿保证金**，且撤销不可撤回」→ `POST …/platform-push/agree-cancel/`（**无入参**，空对象）；**成功重取面板、失败原样显示后端中文且⛔ 不刷新**（业务方口径）。<br>⑤ **没有「拒绝协商」按钮**：上游没有这个动作（不同意只能等协商满 1 小时后申请客服介入）⇒ 页面不摆一颗点了没用的按钮，改成「不同意只能等协商满 1 小时后申请客服介入」一句可读说明（`span.dltCancelHint`，我方追加）。<br>⑥ 「取消撤销」/「取消仲裁」维持原状（上游没有对应能力，仍走「尚未开放」）。 |
 * | 48 | ⭐ **「取消撤销」接真（取代第 47 条 ⑥ 的「维持原状」）** | 真机实测（`Status=16 / CancelStatus=11`）：`LevelOrderCancel(Flag=1, Comment=…)` ⇒ `{"Result":1,"Err":"取消撤销成功."}` ⇒ 状态回到 **`14/10`**（异常单原样还在）；**`Flag=1` 不需要 `PayPass`、不需要金额**。<br>① 可见性：**只有 `CancelStatus=11` 且 `cancel_initiator="us"`（我们自己发起）**才露「取消撤销」（次按钮，证据件原文 class `a.cancelUnRevokeBtn`）；打手发起的（等我们同意 / 仲裁）、`12/15/16`、发起方判不出 ⇒ 都不露。<br>② 动作：确认框写清「**取消后撤销申请作废，订单回到原状态**」→ `POST …/platform-push/cancel-request/`（**空载荷**，端点不收任何入参）；成功提示「已取消撤销申请」+ 重取面板；失败原样显示后端中文且**不刷新**。后端未上线时按「结果未知」降级（「已发出，请刷新核对」，⛔ 不谎报成功）。<br>③ 「取消仲裁」`a.unapplyArbitration` 仍未接（上游没有该能力，仍走「尚未开放」）。 |
 * | 49 | ⭐⭐ **「撤销申请结算 / 赔付」两格填上 + 申请撤销改成三格弹窗 + 打手要多少必须看得见** | 业务方现场：① 「撤销申请结算 / 赔付」两格**永远是空的**——`fillPlatformRows` 从来没给它们赋值（读面已经下发 `pay_level_bal` / `rep_ensure_bal`）；② 打手申请撤销后（`CancelStatus=11`）「协商撤销」按钮消失，客服打不开弹窗、看不到对方要多少钱。<br>① **两格填值**：新增 `PLATFORM_FIELD_SELECTORS.payLevelBal`（`span.left.ppayamount2`）/ `repEnsureBal`（`span.left.ppcamount2`），值走 `formatMoney`（有值 ⇒ 两位小数；`null` / 缺键 / 空串 ⇒「—」，⛔ 不编 `0.00`）。<br>② **「申请撤销」改成三格弹窗**（`layer.open` 的 `type:1`，`buildPlatformRevokeForm` / `readPlatformRevokePayload`）：支付代练费 / 赔偿保证金**用推送行现值预填、可改**，另加一格「撤销理由」（选填）；载荷 = 填了的金额 + 非空理由（**恰好**，两格都空 ⇒ 仍是空载荷、后端按详情现值）；金额页内先挡（0 或正数、最多两位小数，`0` 合法）⇒ 不合法**不关弹层**、零请求。<br>③ **打手要多少必须看得见**：`CancelStatus=11` + 打手发起 / 判不出时，撤销区说明行直接写「打手申请的撤销条件：支付代练费 X / 赔偿保证金 Y；…」，并且「同意撤销」确认框正文也带同一句（只读）——⛔ 不能只给一颗同意按钮、让人不知道在同意多少钱。金额键缺失时退回原文案/原说明（降级）。<br>④ 中文碎片只有一个出口 `cancelMoneySummary()`（说明行与确认框同一句话，⛔ 不写两套）。 |
 * | 50 | ⭐ **「已结代练费 / 已赔保证金」两格填上（业务方点名）** | 这两格此前**恒空**（`fillPlatformRows` 没赋值）。后端新增 `settled_pay_level_bal` / `settled_rep_ensure_bal`（**只在远端 `Status` 17 已结算 / 16 撤销中给值，其它 `null`**）⇒ 新增 `PLATFORM_FIELD_SELECTORS.settledPayLevel`（`span.left.ppayamount`）/ `settledRepEnsure`（`span.left.ppcamount`），值走 `formatMoney`：有值 ⇒ 两位小数；`null` / 键缺席（后端未升级）/ 空串 ⇒「—」（⛔ 不编 `0.00`，也不报错）。 |
 * | 51 | ⭐⭐ **「申请撤销」弹层点了没反应（改准第 49 ② 的 `content` 传法）** | 根因：**裸 DOM 节点当 `content`**。vendored layui 2.13.9 的 layer 对 object 型 `content` 走 **jQuery 分支**（`creat` 里 `r.parents('.layui-layer')` / `r.data(...)` / `.wrap(...)`）；裸节点没有 `.parents` ⇒ 在 `creat` 里抛错、层建不完整（这次连遮罩都没有，表现成「点了没反应」）。<br>① 修法：新增 `layerNode(el)` = 先 `document.body.appendChild(el)`（jQuery 分支要在文档里 `parents()` / 回填位置）→ `content: layui.$(el)`（**jQuery 形状**）→ `end: () => el.remove()`（layui 关层只 `unwrap`、**不删节点**，不清会每次点都留一个）。⛔ 别改回「content 直接传节点」，也别改传 HTML 字符串（表单上的监听 / 引用会丢）。<br>② **本文件 `layer.open` 全查过**（4 处）：`openPushJdpPage`（`type:2` + URL **字符串**，本来就对，未改）；`openPlatformProgressPreview`（进度图大图，**裸节点 → 已改**）；`openPlatformEditDialog`（编辑平台订单，**裸节点 → 已改**）；撤销区「申请撤销」弹窗（**裸节点 → 已改**）。`layer.confirm` / `prompt` / `msg` / `alert` 的 content 全是**字符串**，不涉及。<br>③ 判据：单测钉「`content` 是 jQuery 形状（有 `.parents` / `.data`）+ 关层后节点从 `document.body` 移除」，三层（申请撤销 / 编辑 / 大图）都覆盖；`body` 上 layui 自己的全局拖拽辅助节点 `.layui-layer-move` 不算残留。 |
 * | 52 | ⭐ **弹层闸挂 `end` / 编辑空载荷不关层 / 「参数错误」文案改中性 / failed·unknown 的交易状态色对齐** | ① **弹层闸永远释放**：真 layui 的 `layer.msg` 内部 `closeAll('dialog')` 会**静默**关掉 confirm（只回调 `end`、不走 `cancel`/`yes`）⇒ 旧写法只挂 `cancel` 让 `platformDialogOpen` 永真，之后所有平台动作按钮点了都被 `if (platformDialogOpen) return` 静默吞掉。现在**所有** `layer.confirm` / `layer.prompt` 的 options 都补 `end: closePlatformDialog`（任何关闭路径都放闸）；两个表单弹层（申请撤销 / 编辑平台订单）的 `end` 合成 `节点.remove() + closePlatformDialog()`（节点清理与放闸都不漏）。单测「confirm 开着时收到一次 layer.msg（模拟 closeAll 只回调 end）⇒ 闸释放、同一颗按钮能再开一层」。<br>② **编辑弹层空载荷不关层**：一项都没填时**先提示、弹层不关**（用户改完 / 补一项后还能继续提交）；只有真正提交时才 `closePlatformDialog + layer.close`。单测：空载荷提交 ⇒ `layer.close` 未调、表单节点仍在 body、零请求；补一项后同一层能提交成功。<br>③ **「参数错误」文案改中性**：上游只回了「参数错误」，把它定性成「状态不允许」是**推断** ⇒ 文案改成「代练通没有接受这次撤销（原话：参数错误）；常见原因是当前状态不允许」（`withdrawFailureMessage`；其它原因仍原样透出）。<br>④ **交易状态底色对齐**（`platformPush.ts::platformStatusBadgeClass`）：没有可判远端状态时，本地 `state=failed` ⇒ 红、`unknown` ⇒ 橙（与列表页 `PUSH_BADGE_CLASSES` 一致）；`sent` / `pending` 仍 default 灰。 |

 */
import {
  fetchCategories,
  fetchGames,
  fetchProjects,
  fetchServers,
  type ConfigRow,
} from '@/api/config'
import {
  addOrderPlatformPushAmount,
  addOrderPlatformPushTime,
  agreeOrderPlatformPushCancel,
  blacklistOrderPlatformPush,
  cancelOrderPlatformPushRequest,
  changeOrderPlatformPushPrice,
  DAILIANTONG_PLATFORM,
  editOrderPlatformPush,
  fetchOrderPlatformPushes,
  fetchOrderPlatformPushMessages,
  fetchOrderPlatformPushProgress,
  finishOrderPlatformPush,
  lockOrderPlatformPush,
  modifyOrderPlatformPush,
  offSellOrderPlatformPush,
  requestOrderPlatformPushArbitration,
  sendOrderPlatformPushMessage,
  upPriceOrderPlatformPush,
  uploadOrderPlatformPushScreenshot,
  valueOrderPlatformPush,
  PLATFORM_ACTIVE_PUSH_STATES,
  withdrawAllOrderPlatformPushes,
  withdrawOrderPlatformPush,
  type DailiantongPushRow,
  type EditPlatformPushPayload,
  type PlatformPushMessage,
  type PlatformPushProgressItem,
  type WithdrawAllPlatformsResult,
  type WithdrawPushPayload,
} from '@/api/dailiantongPush'
import { HttpError } from '@/api/errors'
import {
  fetchMyOrderDetail,
  type MyOrderDetail,
} from '@/api/myOrders'
import {
  fetchOrder,
  fetchOrderAudit,
  createOrderNote,
  splitPeerOrder,
  fetchSplitMembers,
  VIEW_CREDENTIALS_ACTION,
  type AuditEntry,
  type Order,
} from '@/api/orders'
import { ORDER_MONEY_FIELDS, formatMoney } from '@/config/money'
import { orderStatusLabel } from '@/config/orderStatus'
import { escapeHtml } from '@/layui-island/escape'
import { closeIslandLayer } from '@/layui-island/layerHost'
import {
  platformWriteAllowedNow,
  resolveOrderViewPermissions,
  resolvePlatformWritePermission,
} from '@/layui-island/perms'
import {
  classifyPushError,
  platformDisplayStatus,
  platformPushStatusLabel,
  PUSH_JDP_SENT_MESSAGE_TYPE,
  PUSH_JDP_SENT_TEXT,
  remoteCancelStatusLabel,
} from '@/layui-island/platformPush'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import { formatDateTime } from '@/m/utils/datetime'

/**
 * 本页在 island 注册表里的 key（= 文件名，`layui-island/page.ts` 按 `import.meta.glob` 发现）。
 * ⛔ `/Order/OrderView` 不在对标的渲染菜单里 ⇒ 不进 `config/replicaRoutes.ts`（导出供豁免集取用）。
 */
export const PAGE_KEY = 'order-orderview'
/** 证据件（页内两段 `<style>` + 裁剪过的 body 标记，**零转义**）。 */
export const ARTIFACT_URL = 'replica/order-orderview.html'
/** URL 参数名：**20 位系统单号**（同 `order-addorder.ts` 的 `order_no`）。 */
export const ORDER_NO_PARAM = 'order_no'
/** 对标 `<title>` 是 `<单号>` + 这四个字（服务端拼接）。 */
export const TITLE_SUFFIX = '订单详情'
/** 错误提示停留 **6000ms**（权限 / 业务失败要看清楚；与 `emp-myemp.ts` / `order-myorderlist.ts` 同值）。 */
export const ERROR_MSG_MS = 6000

/**
 * 「复制订单内容」的两句提示——**逐字**照对标；导出供测试断言**字面值**（E-0024：期望值不写生产常量）。
 */
export const COPY_SUCCESS_MSG = '已成功复制订单内容到剪贴板'
export const COPY_ERROR_MSG = '复制订单内容失败了，请手动复制'

/* ── 受限模式 ──────────────────────────────────────────────
 *
 * 打手（无 `order.view_detail` / `order.view_content`）点蓝链进来时的**脱敏详情**；
 * 客户联系方式类行**不渲染**（受限载荷里本来就没有这些键——不是渲染成空）。
 */
/** 受限端点也 403（不是本人的单 / 无 `my_orders` 节点）时的**人话**提示。 */
export const RESTRICTED_FORBIDDEN_TEXT = '这张单不是你接的，或无权查看'
/** 取数中的可见提示（与 H5 详情页的「加载中…」同一句）。 */
export const RESTRICTED_LOADING_TEXT = '加载中…'
/** 群聊二维码为空时的占位（H5 同句）。 */
export const RESTRICTED_QR_EMPTY_TEXT = '暂无群码'
/** 受限模式复制键的两句提示（H5 详情页 T4 自拟白名单的同两句，两端共用）。 */
export const RESTRICTED_COPY_SUCCESS_MSG = '已复制'
export const RESTRICTED_COPY_FAIL_MSG = '复制失败'
/** 非 403 失败的「重试」与 403 的「关闭」两颗按钮的文案。 */
export const RESTRICTED_RETRY_BTN_TEXT = '重试'
export const RESTRICTED_CLOSE_BTN_TEXT = '关闭'
/** 受限模式整页容器与三颗行内按钮的 class（测试锚点；类名与选择器分成两个常量，E-0061 ①）。 */
export const RESTRICTED_PAGE_CLASS = 'ttRestrictedDetail'
export const RESTRICTED_COPY_BTN_CLASS = 'ttRestrictedCopy'
export const RESTRICTED_RETRY_BTN_CLASS = 'ttRestrictedRetry'
export const RESTRICTED_CLOSE_BTN_CLASS = 'ttRestrictedClose'

export const SPLIT_PROMPT_TITLE = '继续拆分订单：每行一个代练项目（至少 2 行）'
/** 提交值不足两行时的提示（与后端 `SplitOrderSerializer` 那句同一个口径）。 */
export const SPLIT_TOO_FEW_MSG = '拆分订单至少需要 2 个代练项目'
/** 成功提示（我方自撰；后端成功信封的 `msg` 是空串，与改类型 / 改价几处同形）。 */
export const SPLIT_SUCCESS_MSG = '拆分成功'

/**
 * 把输入层里那段文本切成 `projects` 列表：**每行一个**（去空行、去每行首尾空白）。
 * ⛔ 展示层组装，不是第二套业务判据：「至少 2 项 / 每项非空」仍由后端 `SplitOrderSerializer` 回答。
 */
export function splitProjectsFromText(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== '')
}

/**
 * 两块日志的刷新周期；导出是为了让测试断言**这个常量本身**（`=== 120000`），而不是把数字抄两遍。
 */
export const LOG_REFRESH_MS = 2 * 60 * 1000
/** 操作日志表的 `elem`。 */
export const ACTIONLOG_TABLE_ELEM = '#tableList_actionlog'
/** `table.reloadData` / `table.on` 要的是**实例 id**（不带 `#`）——传 `#id` 是静默 no-op（E-0035）。 */
export const ACTIONLOG_TABLE_ID = 'tableList_actionlog'

// 「订单日志」页签要过滤「查看游戏账号密码及详情」这个**审计动作码**：行为理由与
// ⛔ 边界（只过滤这一个页签、不动取数）见下方 `passesOrderLogFilter`（唯一过滤出口）。
// ⚠️ 它的常量 `VIEW_CREDENTIALS_ACTION` 住在 `@/api/orders`，⛔ 本文件里不写字面量
// ——岛页目录里的「带点」字面量会被 `tests/test_rbac_menu_alignment.py` 的源 3
// 当成「声明的权限点」并当场变红（见 E-0187）。

/** 外层页签里「代练通」那一项的 `lay-id`（固定值，与真实 tid 无关）。 */
export const PLATFORM_TAB_ID = 'dlt'
/** 外层页签标题（= 平台名「代练通」）。 */
export const PLATFORM_TAB_TITLE = '代练通'
/** 代练通面板里两个子页签的标题。 */
export const PLATFORM_INFO_TAB_TITLE = '交易平台订单信息'
export const PLATFORM_SHOTS_TAB_TITLE = '截图'
/** 子页签的 `lay-filter`。 */
export const PLATFORM_SUB_TAB_FILTER = 'tabimg'
/** 外层页签的 `lay-filter`。 */
export const ORDER_TAB_FILTER = 'tab1'

/* ── 代练通页签**接真** ──────────────────────────────────────
 *
 * ⚠️ 选择器与 class 名**分成两个常量**（AGENTS.md §5-G 陷阱①）：`xxx_CLASS` 给标记拼串用、
 * `xxx_SELECTOR` 给查找 / 委托用，⛔ 不要在一处写 `'.foo'` 又在另一处写 `'foo'`。
 */
/** 外层「代练通」页签项（点它 ⇒ 取数）。 */
export const PLATFORM_TAB_ITEM_SELECTOR = `.layui-tab[lay-filter="${ORDER_TAB_FILTER}"] > .layui-tab-title > li[lay-id="${PLATFORM_TAB_ID}"]`

/**
 * 页签里各字段值的节点（选中 `span.left` 的 class，**逐格**对应后端推送行的字段）：
 * `tid`→交易平台单号（`serial_no`，空 ⇒「—」）、`PlatformType`→交易平台、`pprice`→订单价格、
 * `psafeamount` / `pdiffamount`→双金、`phour`→要求时限、`pstatus`→交易状态、`pcancelstatus`→撤销状态、
 * `plastSynced`→最近同步、`plastError`→失败原因（红字）、`ppayamount2` / `ppcamount2`→撤销申请结算 / 赔付
 * （`pay_level_bal` / `rep_ensure_bal`）、`ppayamount` / `ppcamount`→已结代练费 / 已赔保证金。
 * 后六行的来历（我方追加 / 后端补齐）登记在文件头「已知会不同」第 37 / 49 / 50 条。
 */
export const PLATFORM_FIELD_SELECTORS = {
  serialNo: 'span.left.tid',
  platform: 'span.left.PlatformType',
  price: 'span.left.pprice',
  ensure1: 'span.left.psafeamount',
  ensure2: 'span.left.pdiffamount',
  timeLimit: 'span.left.phour',
  status: 'span.left.pstatus',
  settledPayLevel: 'span.left.ppayamount',
  settledRepEnsure: 'span.left.ppcamount',
  cancelStatus: 'span.left.pcancelstatus',
  payLevelBal: 'span.left.ppayamount2',
  repEnsureBal: 'span.left.ppcamount2',
  lastSynced: 'span.left.plastSynced',
  lastError: 'span.left.plastError',
} as const

/** 面板顶部那条「空态 / 取数失败」状态区（默认 `display:none`）。 */
export const PLATFORM_STATUS_CLASS = 'dltStatus'
export const PLATFORM_STATUS_SELECTOR = `.${PLATFORM_STATUS_CLASS}`
/** 状态区里那行文字（空态 / 错误共用）。 */
export const PLATFORM_STATUS_TEXT_CLASS = 'dltStatusText'
export const PLATFORM_STATUS_TEXT_SELECTOR = `.${PLATFORM_STATUS_TEXT_CLASS}`
/** 空态里那颗「去发单」（复用订单列表地球按钮的打开方式）。 */
export const DLT_PUSH_BTN_CLASS = 'dltPushBtn'
export const DLT_PUSH_BTN_SELECTOR = `a.${DLT_PUSH_BTN_CLASS}`

/**
 * 两块读面（留言 / 进度图）失败时的「重试」（**我方追加**的控件）。
 * ⚠️ 必须进 `PLATFORM_WIRED_SELECTOR`：`bindPlatformNotOpen` 是捕获阶段委托，
 * 不排除的话点「重试」会先被它拦下并 `stopPropagation`。
 */
export const DLT_RETRY_BTN_CLASS = 'dltRetryBtn'
export const DLT_RETRY_BTN_SELECTOR = `a.${DLT_RETRY_BTN_CLASS}`
/** 重试按钮文案。 */
export const DLT_RETRY_BTN_TEXT = '重试'

/**
 * **已接真**的动作按钮：class 选择器（照证据件原文的 class，⛔ 不新增 class）。
 * ⚠️ 必须从 `bindPlatformNotOpen` 的「尚未开放」捕获委托里**排除**，否则点下去先被它拦下。
 */
export const PLATFORM_ACTION_SELECTORS = {
  withdraw: 'a.cancelPlatorder',
  price: 'a.changePlatorderPrice',
  time: 'a.appenddPlatorderHour',
  message: 'a.dltmsgBtn',
  upPrice: 'a.upPlatorderPrice',
  arbitration: 'a.applyArbitration',
  value: 'a.valuePlatorder',
  onSell: 'a.onSellPlatorder',
  offSell: 'a.offSellPlatorder',
  lock: 'a.lockBtn',
  unlock: 'a.unlockBtn',
  blacklist: 'a.addBlackRnameBtn',
  appendPrice: 'a.appenddPlatorderPrice',
  repriceAll: 'a.reChangePrice',
  repush: 'a.repushorder',
  complete: 'a.completePlatOrder',
  withdrawAll: 'a.cancelAllPlatorder',
  edit: 'a.editPlatformOrder',
  editAll: 'a.editPlatformOrderAll',
  applyCancel: 'a.revokeBtn',
  agreeCancel: 'a.unRevokeBtn',
  requestCancel: 'a.cancelUnRevokeBtn',
} as const
/**
 * 全部**已接真**动作按钮的**合并选择器**（在途 / 无数据闸用；逗号分隔的 `matches()` / `closest()` 入参）。
 */
export const PLATFORM_ACTION_SELECTOR = Object.values(PLATFORM_ACTION_SELECTORS).join(', ')
/** 「确认完单」那颗按钮的选择器（独立常量供 `syncPlatformActionVisibility` / 测试具名使用）。 */
export const PLATFORM_COMPLETE_SELECTOR = PLATFORM_ACTION_SELECTORS.complete

/** 空态那颗 + 读面「重试」+ 截图上传区（`bindPlatformNotOpen` 的排除集）。 */
export const PLATFORM_UPLOAD_DRAG_SELECTOR = '.layui-upload-drag'
export const PLATFORM_WIRED_SELECTOR = [
  PLATFORM_ACTION_SELECTOR,
  DLT_PUSH_BTN_SELECTOR,
  DLT_RETRY_BTN_SELECTOR,
  PLATFORM_UPLOAD_DRAG_SELECTOR,
].join(', ')

/** 「尚未开放」以外的**已接真**按钮点了要加这个 layui 类（提交中禁用，防连击）。 */
export const PLATFORM_BUSY_CLASS = 'layui-btn-disabled'
/**
 * 后端说「现在不能点」的**持久**标记：`runPlatformAction` 收尾会 `setPlatformBusy(false)`，
 * 只靠禁用类会把这条持久禁用**误清掉**——有这条属性在，收尾时把禁用类补回去（判据只认 `=== false`）。
 */
export const PLATFORM_BLOCKED_ATTR = 'data-dlt-blocked'

/**
 * 面板「有没有可用的推送行」的状态：`idle` / `loading` ⇒「正在读取」、`none` ⇒「请先发单」、
 * `failed` ⇒「刷新重试」、`ready` ⇒ 正常开层。
 * ⛔ **别把它压回一个布尔**：「加载失败」的面板点了动作会提示「请先发单」——页面在说假话（实测踩过）。
 */
export type PlatformPanelState = 'idle' | 'loading' | 'none' | 'failed' | 'ready'

/** 空态文案（没有推送行时显示；⛔ 不用「暂无数据」这类不说明下一步的空话）。 */
export const PLATFORM_EMPTY_TEXT = '本单尚未发到交易平台'
/** 空态按钮文案。 */
export const DLT_PUSH_BTN_TEXT = '去发单'
/** 取数失败时面板里那行错误的前缀（后面接**后端原文**）。 */
export const PLATFORM_LOAD_ERROR_PREFIX = '加载失败：'
/** 没有推送行时点四颗动作按钮的提示：直接给**下一步**，别让用户进弹层再被后端拒一次。 */
export const PLATFORM_NEED_PUSH_MSG = '本单尚未发到交易平台，请先发单'
/**
 * 还没取到推送行（`idle` / `loading`）时点四颗动作按钮的提示。
 * ⚠️ 三种「不能点」必须各说各的下一步（加载中等 / 失败刷新 / 没行发单）——
 * 都说「先发单」会与屏幕上那条「加载失败：…」自相矛盾。
 */
export const PLATFORM_LOADING_MSG = '正在读取，请稍候'
/** 取数失败时点四颗动作按钮的提示（与面板里那行「加载失败：…」同一个事实，给它下一步）。 */
export const PLATFORM_FAILED_MSG = '读取失败，请刷新后重试'

/* ── 平台留言历史 / 进度图读面 ──────────────────────────────
 * ⚠️ 选择器与 class 名分成两个常量（AGENTS.md §5-G 陷阱①），⛔ 不要一处写 `'.foo'`、另一处写 `'foo'`。
 */

/** 留言历史容器（`#dltlogdiv`）。 */
export const PLATFORM_MSG_LIST_SELECTOR = '#dltlogdiv'
/** 面板底部「平台留言」输入框（发送时**直接读它**，不再开第二层输入层）。 */
export const PLATFORM_MSG_INPUT_SELECTOR = '#dltmsginput'
/** 加载中 / 空态 / 失败态的文案（三态都必须**可见**，⛔ 不静默空白）。 */
export const PLATFORM_MSG_LOADING_TEXT = '正在加载留言…'
export const PLATFORM_MSG_EMPTY_TEXT = '暂无留言'
export const PLATFORM_MSG_LOAD_ERROR_PREFIX = '留言加载失败：'
/** 系统留言的标记文案（`is_system` 为真时加在时间行上）。 */
export const PLATFORM_MSG_SYSTEM_LABEL = '系统'
/** 系统标记 span 的 class（测试按它找；同时挂 `layui-badge layui-bg-blue` 取颜色）。 */
export const PLATFORM_MSG_SYSTEM_CLASS = 'dltMsgSystem'
/** 发送方 span 的 class（留言要显示「发送方 + 时间 + 内容」）。 */
export const PLATFORM_MSG_SENDER_CLASS = 'dltMsgSender'
/**
 * 留言同一响应里的图片容器（`images`，后端从进度列表附带）。
 * ⚠️ 与「截图」子页签的 `.platfromOrderImgList` 是**两块不同的落点**（那块带上传区）。
 */
export const PLATFORM_MSG_IMAGE_LIST_SELECTOR = '.dltMsgImageList'
export const PLATFORM_MSG_IMAGE_CLASS = 'dltMsgImage'
export const PLATFORM_MSG_IMAGE_LOADING_TEXT = '正在加载图片…'
export const PLATFORM_MSG_IMAGE_EMPTY_TEXT = '暂无图片'
export const PLATFORM_MSG_IMAGE_LOAD_ERROR_PREFIX = '图片加载失败：'
/** 进度图列表容器（「截图」子页签的 `.platfromOrderImgList`）。 */
export const PLATFORM_PROGRESS_LIST_SELECTOR = '.platfromOrderImgList'
export const PLATFORM_PROGRESS_LOADING_TEXT = '正在加载进度图…'
export const PLATFORM_PROGRESS_EMPTY_TEXT = '暂无进度图'
export const PLATFORM_PROGRESS_LOAD_ERROR_PREFIX = '进度图加载失败：'
/** `url` 为空时的占位文案（⛔ 不给 `<img src="">`，那会画一个破图标）。 */
export const PLATFORM_PROGRESS_NO_PREVIEW_TEXT = '无预览'
/**
 * 可点开的进度图缩略图 class（「点缩略图开大图」）。
 *
 * ⚠️ 只挂在**有 `url`** 的 `<img>` 上；`url` 为空的那一格是占位块，点了没有大图可开。
 */
export const PLATFORM_PROGRESS_IMAGE_CLASS = 'dltProgressImage'
export const PLATFORM_PROGRESS_IMAGE_SELECTOR = `img.${PLATFORM_PROGRESS_IMAGE_CLASS}`
/** 大图弹层的默认标题（`caption` 为空时用）。 */
export const PLATFORM_PROGRESS_PREVIEW_TITLE = '进度图'

/* ── 截图上传 ──────────────────────────────────────────────
 *
 * 后端 `DailiantongScreenshotView`：`multipart` 的 `image`（必填）+ `caption`（可选 ≤200 字）；
 * 图种按**字节魔数**只收 jpg / png（⛔ 不信 `Content-Type`）；上限 18 MB。
 * 页面发请求**之前**先挡一轮（大小 / 魔数 / 说明长度），判据仍在后端。
 */
export const PLATFORM_SCREENSHOT_MAX_BYTES = 18 * 1024 * 1024
export const PLATFORM_SCREENSHOT_TOO_LARGE_MSG = '图片不能超过 18 MB'
export const PLATFORM_SCREENSHOT_TYPE_MSG = '只支持 jpg / png 图片'
export const PLATFORM_SCREENSHOT_CAPTION_MAX_LENGTH = 200
export const PLATFORM_SCREENSHOT_CAPTION_TOO_LONG_MSG = `进度说明不能超过 ${PLATFORM_SCREENSHOT_CAPTION_MAX_LENGTH} 个字符`
export const PLATFORM_SCREENSHOT_SUCCESS_MSG = '截图已追加'
/** 运行时插进面板的隐藏 `<input type="file">`。 */
export const PLATFORM_UPLOAD_INPUT_CLASS = 'dltUploadInput'
export const PLATFORM_UPLOAD_INPUT_SELECTOR = `input.${PLATFORM_UPLOAD_INPUT_CLASS}`
/** 「图片说明」输入框（证据件里的 `#imgRemark`）。 */
export const PLATFORM_SCREENSHOT_CAPTION_SELECTOR = '#imgRemark'

/** 「从平台撤回」的确认文案：**必须说清两档后果**（未接手 = 删除并退款；已接手 = 只能协商撤销）。 */
export const WITHDRAW_CONFIRM_TEXT =
  '未接手的单会直接删除并即时退款；已被接手的单只能申请协商撤销，能否撤回由对方决定。确定要从平台撤回这张单吗？'

/**
 * 三颗动作按钮的**成功提示**（撤回的话术另算，见 `withdrawResultMessage()`）。
 * 与后端成功信封的 `msg` 逐字相同，但 `request<T>()` 成功只回 `data` ⇒ 页面只能复述一次，
 * 改后端文案时两处要**同一次改**。⛔ 撤回不能复述后端固定的「撤回已完成」——
 * 「已接手 ⇒ 只登记协商撤销」也回同一句，照抄就是在说假话，话术必须按**返回行**判。
 */
export const PRICE_SUCCESS_MSG = '改价已完成'
export const TIME_SUCCESS_MSG = '补时已完成'
export const MESSAGE_SUCCESS_MSG = '留言已提交'

/** 未接手撤回的真实结果：后端已删除并即时退款（`push.state = closed`）。 */
export const WITHDRAW_CLOSED_MSG = '已撤回并退款'
/** 已接手撤回的真实结果：只登记了协商撤销，等对方 / 客服处理。 */
export const WITHDRAW_CANCEL_PENDING_MSG = '已提交协商撤销，等打手/客服处理'
/** 后端回了成功、但返回行既不是终态也没进协商态（理论不可达）⇒ 让人去核对，⛔ 不谎称成功。 */
export const WITHDRAW_UNVERIFIED_MSG = '撤回请求已提交，请刷新核对'
/**
 * 动作请求「不知道成没成」时的提示（网络 / 超时 / 5xx）。
 * 补时是增量、改价动钱：说成「失败」会诱导用户再点一次 ⇒ 只能让他先刷新核对。
 */
export const PLATFORM_ACTION_UNKNOWN_MSG = '已发出，请刷新核对'

/**
 * 撤回成功提示：**只按后端返回行判**，⛔ 不看 HTTP 是否成功。
 *
 * | 返回行 | 真实语义 | 话术 |
 * |---|---|---|
 * | `state=closed` | 未接手 ⇒ 已删除并退款 | `WITHDRAW_CLOSED_MSG` |
 * | `remote_cancel_status` 进入协商态（非空、非 10） | 已接手 ⇒ 只登记了协商撤销 | `WITHDRAW_CANCEL_PENDING_MSG` |
 * | 其它 | 后端说成功但没核验到终态 | `WITHDRAW_UNVERIFIED_MSG` |
 */
export function withdrawResultMessage(
  push: Pick<DailiantongPushRow, 'state' | 'remote_cancel_status'>,
): string {
  if (push.state === 'closed') return WITHDRAW_CLOSED_MSG
  if (typeof push.remote_cancel_status === 'number' && push.remote_cancel_status !== 10) {
    return WITHDRAW_CANCEL_PENDING_MSG
  }
  return WITHDRAW_UNVERIFIED_MSG
}

/**
 * 申请撤销被上游判「参数错误」时的触发词与人话翻译。
 * ⛔ 其它原因照旧透出后端原文（不吞、也不把所有失败都说成「状态不允许」——那会让真正的原因看不见）；
 * 翻译时把命中的原话留在括号里，仍看得出上游到底说了什么。
 */
export const WITHDRAW_UPSTREAM_INVALID_PARAM_TRIGGERS = [
  '参数错误',
  '无效的参数',
] as const

/** 申请撤销被上游判「参数错误」时显示的人话（不是给用户看的后端原句）。 */
export function withdrawFailureMessage(message: string): string {
  const raw = message ?? ''
  const hit = WITHDRAW_UPSTREAM_INVALID_PARAM_TRIGGERS.find((word) => raw.includes(word))
  if (hit === undefined) return raw
  // ⚠️ 上游只说了「参数错误」⇒ 别把「状态不允许」写成事实（那是推断）。
  return `代练通没有接受这次撤销（原话：${hit}）；常见原因是当前状态不允许`
}

/* ── 撤销区的按钮组合与可读说明 ─────────────────────────────
 * ⛔ **没有「拒绝协商」按钮**：上游没有这个动作 ⇒ 不摆一颗点了没用的按钮，改成一句说明。
 */

/** 撤销区那句说明的 class（**我方追加**）。 */
export const DLT_CANCEL_HINT_CLASS = 'dltCancelHint'
export const DLT_CANCEL_HINT_SELECTOR = `span.${DLT_CANCEL_HINT_CLASS}`

/** 我们自己发起的协商撤销：等对方表态。 */
export const CANCEL_WAIT_TAKER_MSG = '已提交协商撤销，等打手同意'
/** 上游没有「拒绝协商」：不同意只有一条路（协商满 1 小时后申请客服介入）。 */
export const CANCEL_NO_REJECT_MSG = '不同意只能等协商满 1 小时后申请客服介入'
/** 撤销已经处理完（`CancelStatus` 12/15/16）时那句结果文案的前缀（后面接码表中文）。 */
export const CANCEL_DONE_PREFIX = '撤销已处理：'

/* ── 「申请撤销」三格弹窗 ──────────────────────────────────────
 * 两个金额**预填推送行现值**（可改）+ 一格撤销理由（可空）。
 * ⚠️ 金额是**上游要多少钱**（`0` 合法；`AGENTS.md` §5-E 的 `is not None`）；缺金额键 ⇒ 格子留空、
 * 两个键都不送 ⇒ 仍是后端按详情现值（降级）。
 */
export const APPLY_CANCEL_DIALOG_TITLE = '申请撤销'
export const APPLY_CANCEL_SUBMIT_TEXT = '确定'
/** 页内先挡一轮（省一次往返）；判据仍在后端 `WithdrawSerializer`（0 合法、两位小数、非负）。 */
export const APPLY_CANCEL_AMOUNT_INVALID_MSG =
  '撤销申请金额必须是 0 或正数（最多两位小数）'
/** 弹窗三格（顺序 = 显示顺序；`name` 就是提交键，与 `WithdrawPushPayload` 同名）。 */
export const APPLY_CANCEL_FORM_FIELDS = [
  {
    name: 'pay_level_bal',
    label: '支付代练费',
    placeholder: '按详情现值预填，可改（最多两位小数）',
  },
  {
    name: 'rep_ensure_bal',
    label: '赔偿保证金',
    placeholder: '按详情现值预填，可改（最多两位小数）',
  },
  { name: 'comment', label: '撤销理由', placeholder: '选填（≤500 字）' },
] as const

/**
 * 撤销申请金额的页内判据：**0 或正数、最多两位小数**（空串由调用方单独处理 = 不送）。
 * `0` 合法、负数非法（`AGENTS.md` §5-E：金额 0 是边界，不能用真值判）。
 */
export function isValidCancelMoney(value: string): boolean {
  return /^\d+(\.\d{1,2})?$/.test(value)
}

/**
 * 两格金额的中文碎片（「支付代练费 12.50 / 赔偿保证金 5.00」）；一个都没有 ⇒ 空串。
 * 说明行与「同意撤销」确认框**必须同一句话**（⛔ 不写两套，不能只给同意按钮不说钱）。
 */
export function cancelMoneySummary(push: DailiantongPushRow | null): string {
  const parts: string[] = []
  const pay = push?.pay_level_bal
  const rep = push?.rep_ensure_bal
  if (pay !== null && pay !== undefined && String(pay).trim() !== '') {
    parts.push(`支付代练费 ${formatMoney(pay)}`)
  }
  if (rep !== null && rep !== undefined && String(rep).trim() !== '') {
    parts.push(`赔偿保证金 ${formatMoney(rep)}`)
  }
  return parts.join(' / ')
}

/**
 * 本页「按哪条推送行取值」的**唯一出口** = **最新一条有效（活跃）行**。
 * ⛔ 不能直接用 `platformRows[0]`：那可能是已关闭 / 已结算的历史行，而后端只给活跃行下发
 * 撤销两格金额 ⇒ 用 `[0]` 会让撤销预填**落空**（判据与 `PLATFORM_ACTIVE_PUSH_STATES` 同源）。
 * 没有活跃行 ⇒ 返回 `null` ⇒ 撤销弹窗两格**留空要求手填**（⛔ 不编 `0`、也不把 `0` 照发出去）。
 * ⚠️ 第三方页（`order-coreordercenter`）的撤销预填业务方要「默认 0」，⛔ 别把这条搬过去。
 */
export function latestActivePushRow(
  rows: readonly DailiantongPushRow[],
): DailiantongPushRow | null {
  return rows.find((row) => PLATFORM_ACTIVE_PUSH_STATES.includes(row.state)) ?? null
}

/**
 * 「申请撤销」弹窗的表单（`layer.open` 的 `type:1`；⛔ 不拼 HTML 字符串）。
 * 两个金额**用推送行的现值预填**（`null` / 缺键 ⇒ 留空）；理由留空。
 */
export function buildPlatformRevokeForm(
  push: DailiantongPushRow | null,
): HTMLFormElement {
  const form = document.createElement('form')
  form.className = 'dltRevokeForm'
  form.setAttribute('style', 'padding:16px 20px 4px')
  form.addEventListener('submit', (event) => event.preventDefault())
  const prefill: Record<string, string> = {
    pay_level_bal:
      push?.pay_level_bal === null || push?.pay_level_bal === undefined
        ? ''
        : String(push.pay_level_bal),
    rep_ensure_bal:
      push?.rep_ensure_bal === null || push?.rep_ensure_bal === undefined
        ? ''
        : String(push.rep_ensure_bal),
  }
  for (const field of APPLY_CANCEL_FORM_FIELDS) {
    const row = document.createElement('div')
    row.className = 'layui-form-item'
    const label = document.createElement('label')
    label.className = 'layui-form-label'
    label.textContent = field.label
    row.appendChild(label)
    const block = document.createElement('div')
    block.className = 'layui-input-block'
    const input = document.createElement('input')
    input.type = 'text'
    input.className = 'layui-input'
    input.name = field.name
    input.placeholder = field.placeholder
    input.value = prefill[field.name] ?? ''
    block.appendChild(input)
    row.appendChild(block)
    form.appendChild(row)
  }
  return form
}

/**
 * 从「申请撤销」表单读载荷（`WithdrawPushPayload`）。
 *
 * 返回 `null` = **金额不合法**（调用方页内提示、零请求）；返回 `{}` = 两格都空 ⇒
 * 两个金额键都不送，后端按上游详情现值（与改前的空载荷同一降级）。
 * `comment` trim 后为空 ⇒ 不送这个键（载荷恰好）。
 */
export function readPlatformRevokePayload(
  form: HTMLFormElement,
): WithdrawPushPayload | null {
  const payload: WithdrawPushPayload = {}
  for (const name of ['pay_level_bal', 'rep_ensure_bal'] as const) {
    const control = form.querySelector<HTMLInputElement>(`[name="${name}"]`)
    const raw = (control?.value ?? '').trim()
    if (raw === '') continue
    if (!isValidCancelMoney(raw)) return null
    payload[name] = raw
  }
  const commentEl = form.querySelector<HTMLInputElement>('[name="comment"]')
  const comment = (commentEl?.value ?? '').trim()
  if (comment !== '') payload.comment = comment
  return payload
}

/**
 * 撤销要求原文的兜底（`cancel_cmt` / `cancel_reason` 都没下发时）。
 * ⛔ 不要拿「0.00」之类的金额冒充原文：这里只说「去哪里看」，不编内容。
 */
export const CANCEL_REQUEST_FALLBACK_TEXT = '打手申请撤销，条件见留言'

/**
 * 代练通拼好的**撤销要求原文**：`cancel_cmt` → `cancel_reason` → `CANCEL_REQUEST_FALLBACK_TEXT`。
 * ⚠️ 返回的是**原样文本**（可能带换行）；调用方负责转义 / 排版，⛔ 不在这里改写或截断。
 */
export function cancelRequestText(push: DailiantongPushRow | null): string {
  const cmt = (push?.cancel_cmt ?? '').trim()
  if (cmt !== '') return cmt
  const reason = (push?.cancel_reason ?? '').trim()
  if (reason !== '') return reason
  return CANCEL_REQUEST_FALLBACK_TEXT
}

/**
 * 「同意撤销」确认框正文：**打手的撤销要求原文必须原样在正文里**（只读、不可改）。
 * 版式 = 原文（`pre-wrap` 保留换行、默认露两行再滚动，⛔ 不截断）+ 两个金额 + 后果说明。
 * ⚠️ 原文来自第三方（可含任意字符）⇒ 一律 `escapeHtml` 后拼进 HTML，⛔ 不拼裸串（XSS）。
 */
export function buildAgreeCancelConfirmText(push: DailiantongPushRow | null): string {
  const requestText = cancelRequestText(push)
  const money = cancelMoneySummary(push)
  const rows = [
    `<div style="white-space:pre-wrap;line-height:1.6;max-height:3.2em;overflow-y:auto">${escapeHtml(requestText)}</div>`,
  ]
  if (money !== '') {
    rows.push(`<div style="margin-top:6px">本次金额：${escapeHtml(money)}</div>`)
  }
  rows.push(`<div style="margin-top:6px">${escapeHtml(AGREE_CANCEL_CONFIRM_TEXT)}</div>`)
  return rows.join('')
}

/** 「同意撤销」确认框标题 / 文案（业务方要求写清后果：同意后按详情现值付钱 / 赔保证金）。 */
export const AGREE_CANCEL_CONFIRM_TITLE = '同意撤销'
export const AGREE_CANCEL_CONFIRM_TEXT =
  '同意后平台会按详情现值支付代练费 / 赔偿保证金，且撤销不可撤回。确定要同意这次的撤销申请吗？'
/** 同意撤销成功后的提示（后端成功信封的 `msg` 由 `request<T>()` 吞掉 ⇒ 页面只能复述一次）。 */
export const AGREE_CANCEL_SUCCESS_MSG = '已同意撤销'

export const CANCEL_REQUEST_CONFIRM_TITLE = '取消撤销'
export const CANCEL_REQUEST_CONFIRM_TEXT =
  '取消后撤销申请作废，订单回到原状态。确定要取消这次撤销申请吗？'
/** 取消撤销成功后的提示（同上：`request<T>()` 读不到信封 `msg` ⇒ 页面复述一次）。 */
export const CANCEL_REQUEST_SUCCESS_MSG = '已取消撤销申请'

/**
 * 撤销区那句说明的**唯一**出口（渲染在 `span.dltCancelHint`）：
 * 协商撤销中 + 我们发起 ⇒「已提交协商撤销，等打手同意…」；打手发起 / 判不出 ⇒ 撤销要求原文
 * + 金额 +「不同意只能等…」；已处理（12/15/16）⇒「撤销已处理：<码表中文>」；其余 ⇒ 空串。
 * ⚠️ 结果文案里的中文**逐字取 `platformPush.ts::REMOTE_CANCEL_STATUS_LABELS`**，
 * ⛔ 不在这里另写一张撤销码表。
 */
export function platformCancelHint(push: DailiantongPushRow | null): string {
  if (push === null) return ''
  const cancel = push.remote_cancel_status
  if (cancel === 11) {
    const initiator = push.cancel_initiator ?? ''
    if (initiator === 'us') return `${CANCEL_WAIT_TAKER_MSG}；${CANCEL_NO_REJECT_MSG}`
    // 打手发起 / 判不出：把**撤销要求原文**直接摆出来（客服不用点开弹窗就能看到对方要什么）。
    // ⚠️ 说明行是 `textContent`（原文里的引号 / 换行按字面显示）；换行靠 `white-space:pre-wrap`、超长靠滚动。
    const requestText = cancelRequestText(push)
    const money = cancelMoneySummary(push)
    const head = money === '' ? requestText : `${requestText}；需支付：${money}`
    return `${head}；${CANCEL_NO_REJECT_MSG}`
  }
  if (cancel === 12 || cancel === 15 || cancel === 16) {
    return `${CANCEL_DONE_PREFIX}${remoteCancelStatusLabel(cancel) ?? ''}`
  }
  return ''
}

/* ── 确认完单 / 从所有平台撤回 / 编辑平台订单 ─────────────────
 * 口径同其它动作：**载荷恰好**、提交中禁用、成功刷新、失败原样显示后端中文。
 */

/**
 * 确认完单会扣平台余额，等代练通确认结算后同步团团通订单。
 */
export const FINISH_CONFIRM_TEXT =
  '确认完单会按订单价从代练通余额扣款。代练通确认结算后，团团通会同步为已完成。确定要确认完单吗？'
export const FINISH_CONFIRM_TITLE = '确认完单'
export const FINISH_SUCCESS_MSG = '确认完单已完成'

/** 「从所有平台撤回」的确认文案；逐平台结果提交后展示，部分成功必须如实报（后端部分成功也回 `code=0`）。 */
export const WITHDRAW_ALL_CONFIRM_TEXT = '确定将该订单从所有平台撤回吗？'
export const WITHDRAW_ALL_CONFIRM_TITLE = '从所有平台撤回'
export const WITHDRAW_ALL_RESULT_TITLE = '撤回结果'

/**
 * 整单摘要（后端成功信封的 `msg` 读不到 ⇒ 这里**从数据算**）。
 * ⚠️ `failed > 0` 时⛔ 不能说「已全部撤回」：部分成功要如实说「部分平台撤回成功」。
 */
export function withdrawAllSummary(result: WithdrawAllPlatformsResult): string {
  const succeeded = result.succeeded
  const failed = result.failed
  if (failed === 0) return `已从 ${succeeded} 个平台撤回`
  if (succeeded === 0) return `${failed} 个平台撤回失败，原因见逐平台结果`
  return `部分平台撤回成功（成功 ${succeeded} / 失败 ${failed}）`
}

/**
 * 逐平台结果明细（弹层正文；HTML 里每个动态值都过 `escapeHtml`）。
 * ⚠️ `serial_no` 可能是空串（还没拿到平台单号）⇒ 不画一对空括号。
 */
export function withdrawAllDetailHtml(result: WithdrawAllPlatformsResult): string {
  if (result.results.length === 0) return '没有可撤回的平台推送'
  return result.results
    .map((item) => {
      const serial = (item.serial_no ?? '').trim()
      const head = serial === ''
        ? escapeHtml(item.platform_label)
        : `${escapeHtml(item.platform_label)}（${escapeHtml(serial)}）`
      const verdict = item.ok ? '成功' : '失败'
      const message = (item.message ?? '').trim()
      return `<p>${head}：${verdict}${message === '' ? '' : ` —— ${escapeHtml(message)}`}</p>`
    })
    .join('')
}

/**
 * 编辑平台订单的弹窗（「编辑当前平台订单」与「全平台编辑订单」共用同一张表单）。
 * ⚠️ 字段**初始为空、留空 = 不提交**：读面不下发上游现值 ⇒ 无法预填（缺口见文件头第 44 条）。
 */
export const EDIT_CURRENT_TITLE = '编辑当前平台订单'
export const EDIT_ALL_TITLE = '全平台编辑订单'
export const EDIT_SUBMIT_TEXT = '提交'
export const EDIT_EMPTY_MSG =
  '这次没有任何要修改的内容，请至少填一项（标题 / 订单说明 / 代练要求 / 区服 / 频道 / 标签）'
export const EDIT_SUCCESS_MSG = '编辑已提交'
/** 表单字段（顺序 = 弹窗里的顺序；`textarea` 与 `input` 的分档只影响控件形状）。 */
export const EDIT_FORM_FIELDS = [
  { name: 'title', label: '标题', kind: 'input', placeholder: '留空则不修改（2–128 字）' },
  { name: 'memo', label: '订单说明', kind: 'textarea', placeholder: '留空则不修改（≤2048 字）' },
  {
    name: 'requirement',
    label: '代练要求',
    kind: 'textarea',
    placeholder: '留空则不修改（≤2048 字）',
  },
  {
    name: 'zone_server_id',
    label: '区服编码',
    kind: 'input',
    placeholder: '留空则不修改（代练通区服编码，≤32 字）',
  },
  { name: 'groups', label: '发单频道', kind: 'input', placeholder: '留空则不修改' },
  { name: 'label1', label: '标签1', kind: 'input', placeholder: '留空则不修改' },
  { name: 'label2', label: '标签2', kind: 'input', placeholder: '留空则不修改' },
] as const
export type EditFormFieldName = (typeof EDIT_FORM_FIELDS)[number]['name']

/**
 * 编辑弹窗的**表单 DOM**（与 `layer.open` 的 `type:1` 配合；⛔ 不用字符串拼 HTML）。
 * 点「提交」时由 `readPlatformEditPayload()` 收值：**留空（trim 后空串）的字段不提交**。
 */
export function buildPlatformEditForm(): HTMLFormElement {
  const form = document.createElement('form')
  form.className = 'dltEditForm'
  form.setAttribute('style', 'padding:16px 20px 4px')
  // 输入框里按 Enter 会触发原生表单提交（在 iframe 里等于导航走）⇒ 一律拦掉，只认弹层按钮。
  form.addEventListener('submit', (event) => event.preventDefault())
  for (const field of EDIT_FORM_FIELDS) {
    const row = document.createElement('div')
    row.className = 'layui-form-item'
    const label = document.createElement('label')
    label.className = 'layui-form-label'
    label.textContent = field.label
    row.appendChild(label)
    const block = document.createElement('div')
    block.className = 'layui-input-block'
    let control: HTMLInputElement | HTMLTextAreaElement
    if (field.kind === 'textarea') {
      const textarea = document.createElement('textarea')
      textarea.rows = 3
      textarea.className = 'layui-textarea'
      control = textarea
    } else {
      const input = document.createElement('input')
      input.type = 'text'
      input.className = 'layui-input'
      control = input
    }
    control.name = field.name
    control.placeholder = field.placeholder
    block.appendChild(control)
    row.appendChild(block)
    form.appendChild(row)
  }
  return form
}

/**
 * 从编辑表单读提交载荷：**只收填了内容的字段**（`trim()` 后为空 ⇒ 不送这个键，载荷恰好）。
 * ⚠️ 空串在部分字段上原本有「清空」语义，但读面不下发旧值、无法区分「没填」与「要清空」
 * ⇒ 本版不提供清空（缺口见文件头第 44 条）。
 */
export function readPlatformEditPayload(form: HTMLFormElement): EditPlatformPushPayload {
  const payload: EditPlatformPushPayload = {}
  for (const field of EDIT_FORM_FIELDS) {
    const control = form.querySelector<
      HTMLInputElement | HTMLTextAreaElement
    >(`[name="${field.name}"]`)
    if (control === null) continue
    const raw = control.value ?? ''
    if (raw.trim() === '') continue
    // 标题 / 区服 / 频道 / 标签由后端 trim；说明与要求保留原文（空白有意义的那两格）。
    if (field.kind === 'textarea') payload[field.name] = raw
    else payload[field.name] = raw.trim()
  }
  return payload
}

/**
 * 按**字节魔数**判图种（与后端同一张表）：jpg = `ff d8 ff`、png = `89 50 4e 47 0d 0a 1a 0a`。
 * ⚠️ 页面**不信 `File.type`**（客户端可控）；这里只是先挡一轮，最终判据仍在后端。
 * ⚠️ 读不出来（FileReader 失败）⇒ 回 `null`（拒收），⛔ 不默认放行。
 */
export function sniffPlatformImageKind(file: Blob): Promise<'image/jpeg' | 'image/png' | null> {
  return new Promise((resolve) => {
    const reader = new FileReader()
    reader.onerror = () => resolve(null)
    reader.onload = () => {
      const buffer = reader.result
      if (!(buffer instanceof ArrayBuffer)) {
        resolve(null)
        return
      }
      const head = new Uint8Array(buffer)
      if (head.length >= 3 && head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff) {
        resolve('image/jpeg')
        return
      }
      const png = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
      if (head.length >= png.length && png.every((byte, i) => head[i] === byte)) {
        resolve('image/png')
        return
      }
      resolve(null)
    }
    reader.readAsArrayBuffer(file.slice(0, 8))
  })
}

/** 三个输入层的标题与页内校验文案（发请求前的省一次往返；判据仍在后端）。 */
export const PRICE_PROMPT_TITLE = '改价：请输入新的代练通价格（大于 0，最多两位小数）'
export const PRICE_INVALID_MSG = '改价金额必须是大于 0 的数字（最多两位小数）'
export const TIME_PROMPT_TITLE = '补时：请输入要补的小时数（正整数）'
export const TIME_INVALID_MSG = '补时小时数必须是正整数'
export const MESSAGE_EMPTY_MSG = '留言内容不能为空'

/* ── 加价 / 客服介入 / 评价 / 上下架 / 锁单 / 拉黑 ─────────────
 * 口径同其它动作：**载荷恰好**、提交中禁用、成功重取面板、失败原样展示后端中文。
 * 前置状态由**后端**判；页面只决定「这颗按钮该不该露出来」，⛔ 不写第二套状态机（码表只在 `platformPush.ts`）。
 */

/** 加价：金额是**增量**，> 0 且最多两位小数（判据在后端 `UpPriceSerializer`）。 */
export const UP_PRICE_PROMPT_TITLE = '加价：请输入要加的金额（大于 0，最多两位小数）'
export const UP_PRICE_INVALID_MSG = '加价金额必须是大于 0 的数字（最多两位小数）'
export const UP_PRICE_SUCCESS_MSG = '加价已完成'
/** 加价金额判据（与改价同一形状：正数 + 最多两位小数）。 */
export function isValidPlatformUpPrice(value: string): boolean {
  return isValidPlatformPrice(value)
}

/** 申请客服介入：只「申请」，结果靠对方处理（后端会回读 `CancelStatus` 核验）。 */
export const ARBITRATION_CONFIRM_TEXT =
  '申请客服介入后，由代练通客服处理这张单的纠纷（结果以对方处理为准）。确定要申请吗？'
export const ARBITRATION_SUCCESS_MSG = '已提交客服介入申请'
/**
 * 后端明确说「现在不能申请」、又没给原因时的兜底提示。
 * 正常路径用后端下发的 `arbitration_blocked_reason`（含剩余分钟）；⛔ 页面不自己算时间。
 */
export const ARBITRATION_UNAVAILABLE_MSG = '当前不能申请客服介入（条件未满足）'

/** 评价打手：**两步入参**（①满意度码值 10–13、②可留空的评价内容）——`layer.prompt` 一次只收一个值。 */
export const VALUE_RATING_PROMPT_TITLE =
  '评价打手：请输入满意度编号（10 非常满意 / 11 满意 / 12 一般 / 13 很差）'
export const VALUE_RATING_INVALID_MSG = '满意度编号只能是 10 / 11 / 12 / 13'
export const VALUE_COMMENT_PROMPT_TITLE = '评价打手：请输入评价内容（可留空）'
export const VALUE_SUCCESS_MSG = '评价已提交'
/** 满意度码值 → 中文（与后端 `EVAL_TYPE_LABELS` 同值；只服务提示文案）。 */
export const VALUE_RATING_LABELS: Record<string, string> = {
  '10': '非常满意',
  '11': '满意',
  '12': '一般',
  '13': '很差',
}
/** 把用户输入归一成满意度码值；不是 10–13 的数字 ⇒ `null`（页内先挡一道）。 */
export function parsePlatformEvalType(value: string): number | null {
  const text = value.trim()
  if (!/^(10|11|12|13)$/.test(text)) return null
  return Number(text)
}

/** 上架 / 下架：两档后果必须说清（上架 = 重新可被接；下架 = 不再被接）。 */
export const OFF_SELL_CONFIRM_TEXT = {
  0: '下架后这张单在代练通不再出现在可接列表里（不会退款）。确定要下架吗？',
  1: '上架后这张单会重新出现在代练通可接列表里。确定要上架吗？',
} as const
export const OFF_SELL_SUCCESS_MSG = { 0: '下架已完成', 1: '上架已完成' } as const

/**
 * 锁定 / 解锁的结果提示。
 * ⛔ 原因输入层就是那个**确认框**（有「确定 / 取消」+ 后果说明），⛔ 不在它前面再套一层
 * `layer.confirm`——先 confirm 再 prompt 等于让用户确认两遍。
 */
export const LOCK_SUCCESS_MSG = { 1: '已锁定', 0: '已解锁' } as const
/** 锁定 / 解锁的**原因输入层**标题（后端 `reason` 可选、上限 128 字）；标题里把后果说清。 */
export const LOCK_REASON_PROMPT_TITLE = {
  1: '锁定：请输入锁定原因（可留空，锁定后打手无法继续提交进度 / 完单）',
  // 解锁**不保证**回到「正在代练」——它回到锁定前的状态（14 / 12 / 13 都可能）⇒ 标题不写死一档。
  0: '解锁：请输入解锁原因（可留空，解锁后恢复到锁定前的状态）',
} as const
/** 原因上限（与后端 `LOCK_REASON_MAX_LENGTH = 128` 同值；页内先挡一道省一次往返）。 */
export const LOCK_REASON_MAX_LENGTH = 128
/** 原因超长的页内提示（与后端 `lock_push_order` 那句逐字相同）。 */
export const LOCK_REASON_TOO_LONG_MSG = `锁定原因不能超过 ${LOCK_REASON_MAX_LENGTH} 个字符`

/* ── 补款 / 全平台改价 / 重发此单 ──────────────────────────────
 * 口径同上：**载荷恰好**、提交中禁用、成功重取、失败原样展示后端中文。
 */

/**
 * 补款 = 给**已接手**的平台单加钱（打后端 `amount/`）：载荷**恰好** `amount`、
 * **整数元、≥ 1**（代练通不收小数）、正在代练 / 待验收都能做。
 * ⚠️ 与「加价」分开（`up-price/`：只有未接手可做、收两位小数）。
 */
export const APPEND_PRICE_PROMPT_TITLE = '补款：请输入要补的金额（整数元，至少 1 元）'
export const APPEND_PRICE_INVALID_MSG = '补款金额必须是大于等于 1 的整数元（代练通不接受小数）'
export const APPEND_PRICE_SUCCESS_MSG = '补款已完成'

/** 补款金额判据：**纯整数 + ≥ 1**（与 `isValidPlatformPrice` 的两位小数**不同**——补款端点只收整数）。 */
export function isValidPlatformAmount(value: string): boolean {
  const text = value.trim()
  if (!/^\d+$/.test(text)) return false
  return Number(text) >= 1
}

/**
 * 全平台改价：按**平台**去重后逐个调 `price/`（本仓没有「一次改所有平台」的批量端点）；
 * 今天读面只可能有代练通一档 ⇒ 实际就是一发。多平台需按平台传参，属后端契约缺口。
 */
export const REPRICE_ALL_PROMPT_TITLE = '您正在修改全平台发单价，请输入新价格'
export const REPRICE_ALL_INVALID_MSG = '全平台改价金额必须是大于 0 的数字（最多两位小数）'
export const REPRICE_ALL_SUCCESS_MSG = '全平台改价已完成'

/**
 * 重发此单：**打开发单页弹层**，本页零 API。
 * ⛔ 不在这里自己拼发单载荷——价格 / 双金 / 时限 / 区服 / 门槛的唯一出处是发单页的预览接口。
 */
export const REPUSH_LAYER_TITLE = '重发平台订单'

/** 拉黑打手：把这张单的接单打手加入黑名单（之后不能再接你的单）。 */
export const BLACKLIST_CONFIRM_TEXT =
  '拉黑后这名打手不能再接你的单（代练通侧生效，可解拉黑）。确定要拉黑吗？'
export const BLACKLIST_SUCCESS_MSG = '已拉黑该打手'

/**
 * 改价金额判据：`^\d+(\.\d{1,2})?$` + `> 0`。
 * ⚠️ 与 `@/config/money::validateMoneyInput` 不同（那个服务于「可为负」的场景）——
 * 改代练通发单价必须**大于 0**，⛔ 别为了「复用」把负金额放进来。
 */
export function isValidPlatformPrice(value: string): boolean {
  if (!/^\d+(?:\.\d{1,2})?$/.test(value)) return false
  return Number(value) > 0
}

/** 补时小时数判据：**正整数**（后端 `IntegerField(min_value=1, max_value=720)`；>720 交给后端答）。 */
export function isValidPlatformHours(value: string): boolean {
  return /^\d+$/.test(value) && Number(value) >= 1
}

/** 平台 id → 面板「交易平台」那一格的中文（本版只有代练通；未知 id 原样显示数字）。 */
export function platformDisplayName(platform: number): string {
  return platform === DAILIANTONG_PLATFORM ? PLATFORM_TAB_TITLE : String(platform)
}

/**
 * 「代练通」面板的标记（值一律留空，⛔ 不拿 `0.00` / 假账号冒充数据）。
 * ⚠️ 这里**不出现**任何真实单号 / 账号 / 密码。
 * ⚠️ 本函数返回的是**模板字符串** ⇒ 注释里别写反引号（会提前结束模板）。
 */
export function buildPlatformTabMarkup(): string {
  return `
<style>
  /* 对标「平台交易单详情」页自己的页内 <style>（platfromorderview.html L51-152）里**与本面板有关**的
     取值。⚠️ 必须**按子页签作用域**（[lay-filter="tabimg"] 前缀）：本页（订单详情）自己的页内
     样式把 .itemTitle 定成 75px，而对标平台页定的是 90px——直接把平台页的规则搬进本文档会
     把「本系统订单」那半边的行标签也改宽（那不是复刻、是回归）。 */
  .layui-tab[lay-filter="tabimg"] .itemTitle { float: left; width: 90px; text-align: right; }
  .layui-tab[lay-filter="tabimg"] .platfromOrderImgList li { height: 285px; float: left; display: inline-block; margin-left: 10px; padding: 2px 2px; margin-top: 20px; border-color: #e2dede; border-width: 1px; border-style: solid; }
  .layui-tab[lay-filter="tabimg"] .platfromOrderImgList li img { width: 220px; height: 200px; }
  /* g3（2026-10-28）：可点开的缩略图给一个「能点」的手型（对标那张页没有大图预览，属我方追加）。 */
  .layui-tab[lay-filter="tabimg"] .platfromOrderImgList li img.dltProgressImage { cursor: zoom-in; }
  .layui-tab[lay-filter="tabimg"] .platfromOrderImgList li div { text-align: center; margin-top: 2px; width: 220px; height: auto; }
  .layui-tab[lay-filter="tabimg"] .btnbox .layui-btn { margin-bottom: 5px; margin-right: 10px; }
  .layui-tab[lay-filter="tabimg"] .btnbox .layui-btn + .layui-btn { margin-left: 0; }
  .layui-tab[lay-filter="tabimg"] #uploadInput { line-height: 36px; border: 1px solid #ccc !important; position: relative; margin: 10px auto; font-size: 18px; }
  .layui-tab[lay-filter="tabimg"] [contenteditable="true"]:focus { outline: none; }
  .layui-tab[lay-filter="tabimg"] .uploadTips { position: absolute; color: #999; cursor: text; left: 10px; line-height: 36px; top: 0px; font-size: 18px; }
</style>
<div class="layui-tab" lay-filter="${PLATFORM_SUB_TAB_FILTER}">
  <ul class="layui-tab-title">
    <li class="layui-this" lay-id="0">${PLATFORM_INFO_TAB_TITLE}</li>
    <li lay-id="1" class="clientdiv">${PLATFORM_SHOTS_TAB_TITLE}</li>
  </ul>
  <div class="layui-tab-content" style="margin-left:10px">
    <div class="layui-tab-item layui-show">
      <!-- T11b：空态 / 取数失败的状态区（默认隐藏；文案与「去发单」按钮都是**我方追加**的，
           对标那张平台详情页没有它——详见文件头「已知会不同」第 37 条）。 -->
      <div class="${PLATFORM_STATUS_CLASS}" style="display:none;padding:20px 10px">
        <p class="${PLATFORM_STATUS_TEXT_CLASS}" style="color:#999;margin-bottom:10px"></p>
        <a class="layui-btn layui-btn-sm layui-btn-normal ${DLT_PUSH_BTN_CLASS}" style="" href="javascript:;">${DLT_PUSH_BTN_TEXT}</a>
      </div>
      <div class="layui-row">
        <div class="layui-col-sm12 layui-col-xs12 layui-col-md7 layui-col-lg7">
          <div class="layui-form-item">
            <label class="itemTitle">交易平台单号：</label>
            <span class="left tid" style="font-size:14px;color:#ff6a00;margin-right:10px"></span>
            <a class="layui-btn layui-btn-normal layui-btn-xs joinOrderBtn" style="display:none" href="#"
               lay-tips="如果这笔订单在管理订单列表里不显示交易平台，但实际还在交易平台在打，你可以点击这里将这笔交易单和系统关联，方便同步和管理">以这笔交易单为主</a>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">交易平台：</label>
            <span class="left PlatformType" style="font-size:14px;"></span>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">平台账号：</label>
            <span class="left PlatformPushAcc" style="font-size:14px;"></span>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">系统单号：</label>
            <span class="left viewOrderId" style="font-size:14px;color:#ff6a00;"></span>
            <a class="layui-btn layui-btn-xs layui-btn-normal splitOrderBtn" style="margin-left:10px;border-color:#009688"
               lay-tips="一键拆分订单为新单，订单项目默认和当前一致" href="#">一键拆单</a>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">订单来源：</label>
            <span class="left soure" style="font-size:14px;"></span>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">旺旺：</label>
            <span class="left ww" style="font-size:14px;"></span>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">标题：</label>
            <span class="left ptitle" style=""></span>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">发/接时间：</label>
            <span class="left prectime" style=""></span>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">订单价格：</label>
            <span class="left pprice" style="color:#ff0000"></span>
            <a class="layui-btn layui-btn-xs layui-btn-normal appenddPlatorderPrice" style="margin-left:10px;border-color:#009688"
               lay-tips="给这笔订单追加发单价" href="#">补款</a>
            <a class="layui-btn layui-btn-xs layui-btn-normal changePlatorderPrice" style="margin-left:10px;border-color:#009688"
               lay-tips="没被接单前可以点我单独改这个平台的发单价" href="#">改价</a>
            <!-- ⭐ dlt-d：加价（**我方追加**；对标没有这颗按钮）。
                 只有未接手能加价，后端会再判一次状态并回读价格。 -->
            <a class="layui-btn layui-btn-xs layui-btn-normal upPlatorderPrice" style="margin-left:10px;border-color:#009688;display:none"
               lay-tips="没被接单前可以给这张单加价（在原价基础上加）" href="#">加价</a>
          </div>
          <div class="layui-form-item">
            <div class="layui-inline">
              <label class="itemTitle">安全保证金：</label>
              <span class="left psafeamount" style="color: #ff0000"></span>
            </div>
            <div class="layui-inline">
              <label class="itemTitle">效率保证金：</label>
              <span class="left pdiffamount" style="color: #ff0000"></span>
            </div>
          </div>
          <div class="layui-form-item">
            <div class="layui-inline">
              <label class="itemTitle">已结代练费：</label>
              <span class="left ppayamount" style="color: #ff0000"></span>
            </div>
            <div class="layui-inline">
              <label class="itemTitle">已赔保证金：</label>
              <span class="left ppcamount" style="color: #ff0000"></span>
            </div>
          </div>
          <div class="layui-form-item">
            <div class="layui-inline">
              <label class="itemTitle">撤销申请结算：</label>
              <span class="left ppayamount2" style="color: #ff0000"></span>
            </div>
            <div class="layui-inline">
              <label class="itemTitle">撤销申请赔付：</label>
              <span class="left ppcamount2" style="color: #ff0000"></span>
            </div>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">交易状态：</label>
            <span class="left pstatus" style="color:#5BB75B"></span>
            <a class="layui-btn layui-btn-xs layui-btn-normal cancelPlatorder" style="margin-left:10px;border-color:#009688;" href="#">从平台撤回</a>
            <a class="layui-btn layui-btn-xs layui-btn-normal completePlatOrder" style="margin-left:10px;border-color:#009688;display:none" href="#">确认完单</a>
            <a class="layui-btn layui-btn-xs layui-btn-normal exception" style="margin-left: 10px; border-color: #009688; display: none" href="#">提交异常</a>
            <!-- ⭐ dlt-d：上架 / 下架 / 评价打手（**我方追加**；对标没有这三颗）。
                 可见性由 syncPlatformActionVisibility() 按 remote_status 决定
                 （未接手才显示上下架、已结算才显示评价）；状态语义仍由后端判。 -->
            <a class="layui-btn layui-btn-xs layui-btn-normal onSellPlatorder" style="margin-left:10px;border-color:#009688;display:none" href="#">上架</a>
            <a class="layui-btn layui-btn-xs layui-btn-normal offSellPlatorder" style="margin-left:10px;border-color:#009688;display:none" href="#">下架</a>
            <a class="layui-btn layui-btn-xs layui-btn-normal valuePlatorder" style="margin-left:10px;border-color:#009688;display:none" href="#">评价打手</a>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">撤销状态：</label>
            <span class="left pcancelstatus" style=""></span>
            <a class="layui-btn layui-btn-xs layui-btn-normal applyPlatorderRevoke revokeBtn" style="margin-left:10px;border-color:#009688" href="#">申请撤销</a>
            <a class="layui-btn layui-btn-xs layui-btn-normal applyPlatorderRevoke cancelUnRevokeBtn" style="margin-left:10px;border-color:#009688;display:none" href="#" data-change="20">取消撤销</a>
            <a class="layui-btn layui-btn-xs layui-btn-danger applyPlatorderRevoke unRevokeBtn" style="margin-left:10px;border-color:#009688;display:none" href="#" data-change="30">同意撤销</a>
            <a class="layui-btn layui-btn-xs layui-btn-warm platfromArbitration applyArbitration" style="margin-left:10px;border-color:#009688;display:none" href="#" data-change="10">申请仲裁</a>
            <a class="layui-btn layui-btn-xs layui-btn-normal platfromArbitration unapplyArbitration" style="margin-left:10px;border-color:#009688;display:none" href="#" data-change="20">取消仲裁</a>
            <!-- ⭐ h14：撤销区那句可读说明（**我方追加**；对标平台页没有这一格）。
                 内容由 platformCancelHint() 按 (CancelStatus, 撤销发起方) 决定：
                 等打手同意 / 打手给的撤销要求原文 / 「不同意只能等协商满 1 小时后申请客服介入」/「撤销已处理：…」。
                 ⚠️ 不挂 left 这个 class（那一族的 span 会被「值一律留空」的守卫测试逐个检查）。
                 ⚠️ 原文可能带换行 ⇒ white-space:pre-wrap 保留排版；太长只滚动、默认露两行（⛔ 不截断）。 -->
            <span class="dltCancelHint" style="margin-left:10px;color:#999;white-space:pre-wrap;display:inline-block;vertical-align:top;max-height:42px;overflow-y:auto;max-width:100%"></span>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">要求时限：</label>
            <span class="left phour" style="color: #ff6a00"></span>
            <a class="layui-btn layui-btn-xs layui-btn-normal appenddPlatorderHour" style="margin-left:10px;border-color:#009688" href="#">补时</a>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">剩余时间：</label>
            <span class="left psurplushour" style="color:#ff6a00"></span>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">最近同步：</label>
            <span class="left plastSynced" style=""></span>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">失败原因：</label>
            <span class="left plastError" style="color:#ff0000"></span>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">游戏密码：</label>
            <span class="left paccountpwd" style=""></span>
            <a class="layui-btn layui-btn-xs layui-btn-normal changePlatorderPwd" style="margin-left:10px;border-color:#009688" href="#">修改密码</a>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">角色名：</label>
            <span class="left pgamerolename" style=""></span>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">发单说明：</label>
            <span class="left orderDescription" style=""></span>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">是否锁定：</label>
            <span class="left pislock" style=""></span>
            <a class="layui-btn layui-btn-xs layui-btn-normal changePlatorderLock lockBtn" style="margin-left:10px;border-color:#009688" href="#" data-change="1">锁定</a>
            <a class="layui-btn layui-btn-xs layui-btn-normal changePlatorderLock unlockBtn" style="margin-left:10px;border-color:#009688" href="#" data-change="0">解锁</a>
          </div>
          <div class="layui-form-item">
            <label class="itemTitle">对方昵称：</label>
            <span class="left precnickname" style=""></span>
            <a class="layui-btn layui-btn-xs layui-btn-normal addBlackRnameBtn" style="margin-left:10px;border-color:#009688" href="#" data-change="1">拉黑名单</a>
            <a class="layui-btn layui-btn-xs layui-btn-normal removeBlackRnameBtn" style="margin-left:10px;border-color:#009688" href="#" data-change="1">解除黑名单</a>
          </div>
          <div class="layui-form-item">
            <div class="layui-inline">
              <label class="itemTitle">对方QQ：</label>
              <span class="left precqq" style=""></span>
              <span class="recuserid" style="display:none"></span>
            </div>
            <div class="layui-inline">
              <label class="itemTitle">对方手机：</label>
              <span class="left precphone" style=""></span>
            </div>
          </div>
          <div class="layui-form-item btnbox">
            <a class="layui-btn layui-btn-lg layui-btn-normal" style="" href="javascript:location.reload();" lay-tips="刷新这个界面">点我刷新当前页</a>
            <a class="layui-btn layui-btn-lg editPlatformOrder" style="" href="javascript:;"
               lay-tips="此项仅编辑当前平台的订单，可以在不撤回情况下修改发单价、双金、标题、要求等">编辑当前平台订单</a>
            <a class="layui-btn layui-btn-lg editPlatformOrderAll" style="background-color:#16b777" href="javascript:;"
               lay-tips="此项可全平台编辑交易订单，可在不撤回情况下修改发单价、双金、标题、要求等">全平台编辑订单</a>
            <a class="layui-btn layui-btn-lg layui-btn-normal reChangePrice" style="" href="javascript:;"
               lay-tips="没有被接单前，可以点我操作全平台改价">全平台改价</a>
            <a class="layui-btn layui-btn-lg layui-btn-warm repushorder" style="" href="javascript:;"
               lay-tips="重新将此单发布到这个平台">重发此单</a>
            <a class="layui-btn layui-btn-lg layui-btn-danger cancelAllPlatorder" style="" href="javascript:;"
               lay-tips="从所有交易平台撤回该订单">从所有平台撤回此单</a>
          </div>
        </div>
        <div class="layui-col-sm12 layui-col-xs12 layui-col-md5 layui-col-lg5 clientdiv">
          <fieldset class="layui-elem-field" style="">
            <legend>平台留言</legend>
            <div class="layui-field-box platfromOrderLogdiv" style="padding: 7px 0px; overflow: hidden; overflow-y: auto; max-height: 430px" id="dltlogdiv"></div>
            <div class="dltMsgImageList" style="display:flex;flex-wrap:wrap;gap:6px;max-height:180px;overflow:auto;padding:2px 5px"></div>
            <div class="layui-form-item">
              <input type="text" name="dltmsginput" id="dltmsginput" lay-verify="" autocomplete="off"
                     placeholder="可以在此输入要留言的内容" class="layui-input">
              <a class="layui-btn layui-btn-sm layui-btn-normal dltmsgBtn" style="">发送</a>
            </div>
          </fieldset>
          <table class="layui-hide" id="yjTableList" lay-filter="yjTableList"></table>
        </div>
      </div>
    </div>
    <div class="layui-tab-item clientdiv">
      <div class="layui-row">
        <div class="layui-form-item layui-form-pane">
          <label class="layui-form-label">图片说明</label>
          <div class="layui-input-block">
            <input type="text" name="imgRemark" id="imgRemark" autocomplete="off"
                   placeholder="请输入图片说明，不超过50个字符" maxlength="50" class="layui-input">
          </div>
        </div>
        <div style="position: relative; width: 100%; margin: 0 auto;">
          <div class="uploadTips">点击此处可粘贴图片</div>
          <div id="uploadInput" contenteditable="true"></div>
        </div>
        <div class="layui-form-item">
          <div class="layui-upload-drag" style="display: block;" id="ID-upload-demo-drag">
            <i class="layui-icon layui-icon-upload"></i>
            <div>点击上传，或将文件拖拽到此处</div>
            <div class="layui-hide" id="ID-upload-demo-preview">
              <hr> <img src="" alt="上传成功后渲染" style="max-width: 100%">
            </div>
          </div>
          <div class="layui-progress" lay-filter="uppercent">
            <div class="layui-progress-bar layui-bg-red" lay-percent="0%"></div>
          </div>
        </div>
        <ul class="platfromOrderImgList"></ul><img width="100" />
      </div>
    </div>
  </div>
</div>`
}

/**
 * 往「本系统订单」旁边追加「代练通」页签（标题 + 面板），返回外层页签容器。
 * ⚠️ **必须在 `initIslandElements(layui)` 之前调**：页签绑定在注入之后才看到这两个 `li`。
 */
export function injectPlatformTab(root: HTMLElement): HTMLElement | null {
  const tab = root.querySelector<HTMLElement>(`.layui-tab[lay-filter="${ORDER_TAB_FILTER}"]`)
  if (!tab) return null
  const title = tab.querySelector<HTMLElement>(':scope > .layui-tab-title')
  const content = tab.querySelector<HTMLElement>(':scope > .layui-tab-content')
  if (!title || !content) return null
  const li = document.createElement('li')
  li.setAttribute('lay-id', PLATFORM_TAB_ID)
  li.style.display = 'none'
  li.textContent = PLATFORM_TAB_TITLE
  title.appendChild(li)
  const panel = document.createElement('div')
  panel.className = 'layui-tab-item'
  panel.innerHTML = buildPlatformTabMarkup()
  content.appendChild(panel)
  return panel
}

/**
 * 代练通页签里**本版没接**的动作按钮的统一收口：点了只提示「尚未开放」（D-0025 §5）。
 * 唯一例外是「点我刷新当前页」——保留原生 `location.reload()`（island iframe 里就是重载本页）。
 * 已接真的按钮在 `PLATFORM_WIRED_SELECTOR` 里须放行（本函数在捕获阶段，不排除会先拦下）。
 * 用**捕获阶段**：平台按钮不该落到外层挂 `root` 冒泡阶段的「复制 / 拆分」两个委托身上。
 */
export function bindPlatformNotOpen(
  root: HTMLElement,
  msg: (text: string) => void,
): () => void {
  const onClick = (event: Event): void => {
    const target = event.target as HTMLElement
    const panel = root.querySelector<HTMLElement>(
      `.layui-tab[lay-filter="${PLATFORM_SUB_TAB_FILTER}"]`,
    )
    if (!panel) return
    const clickable = target.closest<HTMLElement>('a.layui-btn, button, .layui-upload-drag')
    if (clickable === null || !panel.contains(clickable)) return
    // 已接真的按钮：交给页面自己的处理器（它们各自会 preventDefault）。
    if (clickable.matches(PLATFORM_WIRED_SELECTOR)) return
    const href = clickable.getAttribute('href') ?? ''
    if (href.startsWith('javascript:location.reload')) return
    event.preventDefault()
    event.stopPropagation()
    const label = (clickable.textContent ?? '').trim().replace(/\s+/g, ' ')
    if (label === '') return
    msg(`「${label}」尚未开放`)
  }
  root.addEventListener('click', onClick, true)
  return () => root.removeEventListener('click', onClick, true)
}


/**
 * 「对标行」的 14 个标签；它们同时是**运行时按标签找行的键**（中文只在这里出现一次）。
 * ⛔ 这份清单与证据件必须**逐字一致**——守卫测试拿证据件原文核它，不一致**当场变红**
 * （否则症状是「某些行静默不填」）。
 */
export const STANDARD_ROW_LABELS = {
  orderNo: '订单号：',
  gameServer: '游戏区服：',
  roleInfo: '角色信息：',
  gameAccount: '游戏账号：',
  subAccount: '子账号：',
  password: '登录密码：',
  secret: '暗号/口令：',
  category: '项目分类：',
  project: '代练项目：',
  status: '订单状态：',
  timing: '订单计时：',
  taker: '当前接手：',
  contactPhone: '联系电话：',
  assignPhone: '指定电话：',
} as const

/** `STANDARD_ROW_LABELS` 的键（`keyof` 别名字面量，供测试与内部循环用）。 */
export type StandardRowKey = keyof typeof STANDARD_ROW_LABELS

/** 6 个金额字段的集合（判据取 `@/config/money` 的权威列表，不在这里手写镜像）。 */
const MONEY_FIELD_SET: ReadonlySet<string> = new Set<string>(ORDER_MONEY_FIELDS)

/**
 * FK 字段名 → `NameDicts` 的键。
 * ⚠️ 两张表的名字**故意不同**（字段单数 `game` / 字典复数 `games`），所以映射必须显式写出来；
 * 直接拿字段名索引 `NameDicts` 会恒 `undefined` ⇒ **静默显示原始 id**（一个不容易被发现的错）。
 * 由 `order-orderview.spec.ts` 的「FK 显示名字」用例钉住。
 */
const FK_DICT_KEY: Record<string, keyof NameDicts> = {
  game: 'games',
  server: 'servers',
  category: 'categories',
  project: 'projects',
}

/** 4 个 FK 的「id → 名字」字典（每页只拉一次，渲染时不发请求）。 */
export type NameDicts = {
  games: Map<number, string>
  servers: Map<number, string>
  categories: Map<number, string>
  projects: Map<number, string>
}

/** 空字典（拉取失败 / 还没拉回来时的取值 —— FK 会回落成原始 id）。 */
export function emptyNameDicts(): NameDicts {
  return {
    games: new Map(),
    servers: new Map(),
    categories: new Map(),
    projects: new Map(),
  }
}

/** 从 iframe URL 里取 20 位系统单号（与 `order-addorder.ts` 同一口径）。 */
export function orderNoFromSearch(search: string): string {
  return new URLSearchParams(search).get(ORDER_NO_PARAM) ?? ''
}

/* ── 受限模式的渲染出口 ───────────────────────────────────────
 * ⚠️ 全部**只用 DOM API + `textContent`**（⛔ 不拼 HTML、不用 `root.innerHTML`）：
 * 载荷里的自由文本是用户输入，拼串就是 XSS。
 */

/** 空值显示「—」（判据「去空白后为空」，不是真值——E-0021 同族：0 / 空白不算有值）。 */
export function restrictedDash(value: string | null): string {
  if (value === null) return '—'
  return value.trim() === '' ? '—' : value
}

/** 三个账号字段是否至少一个有值（全空 ⇒ 整张「游戏账号」卡不渲染，不留空白卡；H5 同判据）。 */
export function restrictedHasGameAccount(detail: MyOrderDetail): boolean {
  return (
    detail.game_account.trim() !== '' ||
    detail.game_sub_account.trim() !== '' ||
    detail.game_role_name.trim() !== ''
  )
}

/** 受限模式的页头单号：`source_order_no` 非空取它、空回落 `order_no`（D-0301，同 `displayOrderNo`）。 */
export function restrictedDisplayNo(
  detail: Pick<MyOrderDetail, 'order_no' | 'source_order_no'>,
): string {
  return displayOrderNo(detail)
}

/** 受限模式的一张卡（标题 + 若干行）。 */
function restrictedCard(title: string): HTMLElement {
  const card = document.createElement('div')
  card.className = 'ttRestrictedCard'
  card.setAttribute(
    'style',
    'margin:0 0 10px;padding:12px 14px;background:#fff;border:1px solid #e6e6e6;border-radius:4px',
  )
  const head = document.createElement('div')
  head.setAttribute('style', 'font-size:14px;font-weight:600;color:#555;margin:0 0 6px')
  head.textContent = title
  card.appendChild(head)
  return card
}

/** 受限模式卡片里的一行（`copyText` 非空 ⇒ 行尾带「复制」键，点击走页面的委托）。 */
function restrictedRow(label: string, value: string, copyText?: string): HTMLElement {
  const row = document.createElement('div')
  row.className = 'ttRestrictedRow'
  row.setAttribute('style', 'display:flex;align-items:flex-start;gap:8px;padding:4px 0')
  const name = document.createElement('span')
  name.setAttribute('style', 'flex-shrink:0;width:5.5em;color:#888')
  name.textContent = label
  const text = document.createElement('span')
  text.className = 'ttRestrictedValue'
  text.setAttribute('style', 'flex:1;min-width:0;word-break:break-all;color:#333')
  text.textContent = value
  row.appendChild(name)
  row.appendChild(text)
  if (copyText !== undefined && copyText !== '') {
    const copy = document.createElement('a')
    copy.className = `layui-btn layui-btn-xs ${RESTRICTED_COPY_BTN_CLASS}`
    copy.setAttribute('href', 'javascript:;')
    copy.dataset.copy = copyText
    copy.textContent = '复制'
    row.appendChild(copy)
  }
  return row
}

/** 取数中 / 失败两种非数据态的整页占位（⛔ 不静默空白）。 */
function restrictedNotice(text: string): HTMLElement {
  const page = document.createElement('div')
  page.className = RESTRICTED_PAGE_CLASS
  page.setAttribute('style', 'padding:32px 16px;text-align:center;color:#999;font-size:14px')
  page.textContent = text
  return page
}

/**
 * 把受限详情渲染成整页（五张卡；`root` 旧内容整体清掉——受限模式**不装证据件骨架**，
 * 否则又回到「标签在、值全空」的静默骨架）。
 */
export function renderRestrictedDetail(root: HTMLElement, detail: MyOrderDetail): void {
  const page = document.createElement('div')
  page.className = RESTRICTED_PAGE_CLASS
  page.setAttribute('style', 'padding:12px 16px;background:#f2f2f2;min-height:100%')

  const base = restrictedCard('基本信息')
  base.appendChild(restrictedRow('单号', restrictedDisplayNo(detail), restrictedDisplayNo(detail)))
  base.appendChild(restrictedRow('游戏', restrictedDash(detail.game_name)))
  base.appendChild(restrictedRow('区服', restrictedDash(detail.server_name)))
  base.appendChild(restrictedRow('项目', restrictedDash(detail.project_text)))
  base.appendChild(restrictedRow('状态', statusText(detail.status)))
  base.appendChild(restrictedRow('加急', detail.is_urgent ? '加急' : '—'))
  // `claimed_at` 是 `+08:00` 偏移的 ISO 串 ⇒ 按串内偏移换算（H5 同一条 `formatDateTime`）；
  // 形状认不出（空串）⇒「—」，⛔ 不把原始 ISO 串填进页面。
  const claimedAt = formatDateTime(detail.claimed_at)
  base.appendChild(restrictedRow('接手时间', claimedAt === '' ? '—' : claimedAt))
  page.appendChild(base)

  const requireCard = restrictedCard('要求')
  requireCard.appendChild(restrictedRow('订单内容', restrictedDash(detail.content)))
  requireCard.appendChild(restrictedRow('代练要求', restrictedDash(detail.demand)))
  requireCard.appendChild(restrictedRow('时间要求', restrictedDash(detail.time_requirement)))
  requireCard.appendChild(restrictedRow('备注', restrictedDash(detail.remark)))
  page.appendChild(requireCard)

  if (restrictedHasGameAccount(detail)) {
    const account = restrictedCard('游戏账号')
    account.appendChild(restrictedRow('游戏账号', restrictedDash(detail.game_account), detail.game_account))
    account.appendChild(
      restrictedRow('子账号', restrictedDash(detail.game_sub_account), detail.game_sub_account),
    )
    account.appendChild(
      restrictedRow('角色信息', restrictedDash(detail.game_role_name), detail.game_role_name),
    )
    page.appendChild(account)
  }

  const pay = restrictedCard('我的报酬')
  // ⚠️ `temp_amount` 是 DecimalField 序列化出的**字符串**（如 "88.00"），不 parse、原样拼。
  pay.appendChild(restrictedRow('接单金额', `${detail.temp_amount} 元`))
  page.appendChild(pay)

  const qr = restrictedCard('群聊二维码')
  if (detail.group_qr_url !== null && detail.group_qr_url !== '') {
    const img = document.createElement('img')
    img.className = 'ttRestrictedQr'
    img.setAttribute('style', 'display:block;width:180px;height:180px;object-fit:contain')
    img.src = detail.group_qr_url
    img.alt = '群聊二维码'
    qr.appendChild(img)
  } else {
    const empty = document.createElement('p')
    empty.setAttribute('style', 'margin:0;font-size:13px;color:#999;text-align:center')
    empty.textContent = RESTRICTED_QR_EMPTY_TEXT
    qr.appendChild(empty)
  }
  page.appendChild(qr)

  root.replaceChildren(page)
}

/**
 * 受限模式的失败整页：居中一句**人话** + 一颗按钮。
 * 403 ⇒ 固定文案 +「关闭」（`closeIslandLayer`）；其它失败 ⇒ 后端 `msg` 原样 +「重试」。
 * ⛔ 不再弹「标签在、值全空」的骨架。
 */
export function renderRestrictedError(
  root: HTMLElement,
  message: string,
  forbidden: boolean,
): void {
  const page = document.createElement('div')
  page.className = RESTRICTED_PAGE_CLASS
  page.setAttribute('style', 'padding:48px 16px;text-align:center')
  const line = document.createElement('p')
  line.setAttribute('style', 'margin:0 0 14px;font-size:14px;color:#d54941')
  line.textContent = message
  page.appendChild(line)
  const button = document.createElement('a')
  button.className = `layui-btn layui-btn-xs ${
    forbidden ? RESTRICTED_CLOSE_BTN_CLASS : RESTRICTED_RETRY_BTN_CLASS
  }`
  button.setAttribute('href', 'javascript:;')
  button.textContent = forbidden ? RESTRICTED_CLOSE_BTN_TEXT : RESTRICTED_RETRY_BTN_TEXT
  page.appendChild(button)
  root.replaceChildren(page)
}

/**
 * 页头「订单号」：来源单号非空用它，否则回落系统单号。
 * ⚠️ 列表那一格为空就留空（**不**回落）——两处是有意不同，别「统一」。
 */
export function displayOrderNo(order: Pick<Order, 'order_no' | 'source_order_no'>): string {
  return order.source_order_no !== '' ? order.source_order_no : order.order_no
}

function renderPenaltyOrderLinks(root: HTMLElement, order: Order): void {
  root.querySelector('[data-penalty-order-links]')?.remove()
  const related = [
    { label: '罚款原订单', number: order.penalty_parent_order_no },
    ...(order.penalty_children ?? []).map((child) => ({
      label: '罚款子单', number: child.order_no,
    })),
  ]
  const links = document.createElement('div')
  links.className = 'layui-form-item'
  links.dataset.penaltyOrderLinks = ''
  for (const { label, number } of related) {
    if (number == null || !/^\d{20}$/.test(number)) continue
    const link = document.createElement('a')
    link.className = 'layui-btn layui-btn-primary layui-btn-xs'
    link.href = `/orders/${number}`
    link.target = '_blank'
    link.rel = 'noopener'
    link.textContent = `${label} ${number}`
    links.append(link)
  }
  if (links.childElementCount > 0) rowByLabel(root, STANDARD_ROW_LABELS.orderNo)?.after(links)
}

/** 状态枚举 → 本页显示的中文（我方词表；认不出的原样返回，与其它页同口径）。 */
export function statusText(status: string): string {
  return orderStatusLabel(status)
}

/**
 * 后端 `OrderType` 的**枚举名 → 中文**（本页独用，渲染「代练项目」绿色徽章）。
 * 两个值逐字取自模型；认不出的**原样显示**（不猜、不留白）。测试钉字面值。
 */
export const ORDER_TYPE_LABELS: Record<string, string> = {
  BOOST: '代练',
  COMPANION: '陪玩',
}

/** `order_type` 枚举名 → 徽章文案。 */
export function orderTypeText(value: string): string {
  return ORDER_TYPE_LABELS[value] ?? value
}

/** 时间格式化成 `YYYY-MM-DD HH:mm`（与 `order-myorderlist.ts` / `emp-myemp.ts` 同口径）。 */
function formatTime(value: string): string {
  return value.replace('T', ' ').slice(0, 16)
}

/**
 * 本项目 `TIME_ZONE` 的固定偏移（`Asia/Shanghai` = UTC+8，**无夏令时**）：
 * 用它把带偏移的 ISO 串换算成「项目时区的墙钟」。
 */
const DISPLAY_OFFSET_MINUTES = 8 * 60

/**
 * ISO-8601 → `MM-DD HH:mm`：按**串里自带的偏移**把瞬间换算到项目 `TIME_ZONE` 再取墙钟。
 * ⛔ 不用 `new Date('…')` + `getHours()`：那按**浏览器**时区渲染，东八区以外整点平移；
 * 尾部没有偏移的串按「已经是本地墙钟」原样取（与本仓 `formatCreatedAt` 同一口径）；
 * 形状认不出（空值 / 非 ISO）⇒ 空串，**绝不把原始 ISO 串填进列表**。
 * ⚠️ 本端点与兄弟端点的 `created_at` 时区口径不同，直接切字符会整整差 8 小时。
 */
export function formatLogTime(value: string): string {
  // 秒与小数秒可选（后端可能回 `2026-09-23T00:14:40.110591+00:00`）——**必须一起吃掉**，
  // 否则 `slice` 后的「剩余串」以 `:` 开头，偏移就认不出来（会静默按「无偏移」处理）。
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?/.exec(value)
  if (m === null) return ''
  const rest = value.slice(m[0].length)
  let offsetMinutes: number | null
  if (/^Z/i.test(rest)) {
    offsetMinutes = 0
  } else {
    const om = /^([+-])(\d{2}):?(\d{2})/.exec(rest)
    offsetMinutes =
      om === null ? null : (om[1] === '-' ? -1 : 1) * (Number(om[2]) * 60 + Number(om[3]))
  }
  // `null` = 串里没给偏移 ⇒ 视为已经是项目时区的墙钟（位移 0）。
  const shift = offsetMinutes === null ? 0 : DISPLAY_OFFSET_MINUTES - offsetMinutes
  const at = new Date(
    Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5])) +
      shift * 60_000,
  )
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${pad(at.getUTCMonth() + 1)}-${pad(at.getUTCDate())} ${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())}`
}

/**
 * 一条留痕 → 订单日志那一行 `<p class="linespan">`。
 * ⚠️ 文本用 `textContent` 而不是拼 HTML：`summary` 含**用户输入的日志内容**，拼串就是一条 XSS。
 */
export function buildLogLine(entry: AuditEntry): HTMLElement {
  const line = document.createElement('p')
  line.className = 'linespan'
  line.setAttribute('style', 'line-height:28px;margin-left:5px; color:#999')
  line.textContent = `【${formatLogTime(entry.created_at)}】${entry.summary}`
  return line
}

/** 操作日志表的行（**用对标的字段名**，让列的 `field` / 本地排序保持逐字复刻）。 */
export type ActionLogRow = {
  createTime: string
  remark: string
  userName: string
}

/**
 * 留痕行 → 操作日志表的行：`summary`→`remark`、`actor`→`userName`、`created_at`→`createTime`。
 * ⚠️ `userName` 取显示名：优先 `actor_display`（昵称），键**缺席**时回落 `actor`（登录账号），
 * 两个来源都缺也给空串——部署窗口「前端新、后端旧」时页面必须照旧能显示，⛔ 不许出现 `undefined`。
 * ⛔ 列定义 / 字段名 / 宽度一律不动（`userName` 是对标的字段名）。
 */
export function toActionLogRow(entry: AuditEntry): ActionLogRow {
  return {
    createTime: formatLogTime(entry.created_at),
    remark: entry.summary,
    userName: entry.actor_display ?? entry.actor ?? '',
  }
}

/**
 * 操作日志表的 3 列（字段名 / 标题 / 宽度 / 对齐 / `sort` / `unresize` 逐字照证据件，一个不改）。
 * ⚠️ **`sort: true` 保留**：本页数据一次全给 + `page:false` ⇒ layui 的本地排序就是既有行为，
 * ⛔ 不要顺手改成远程排序、也不要传 `autoSort: false`。
 */
export function buildActionLogCols(): Record<string, unknown>[] {
  return [
    { field: 'createTime', title: '时间', width: 90, align: 'center', unresize: false, sort: true },
    // ⛔ `minwidth`（**小写 w**）是逐字照证据件的原文；layui 2.13.9 只认驼峰 `minWidth`
    // ⇒ 这个属性被忽略、这一列是「自动列」。⛔ **不要**顺手 "修好" 成 `minWidth`：
    // 那会把自动列撑开、总宽超过视口（先例 `/User/userlist` 1649→1669）。
    { field: 'remark', title: '操作说明', minwidth: 100, unresize: false },
    { field: 'userName', title: '操作人', align: 'center', width: 70, sort: false },
  ]
}

/** 一个字段的显示文本。空值 → `''`（照对标「空就空着」）；金额走 `formatMoney`（`null` ⇒ `—`）。 */
export function fieldText(order: Order, field: string, dicts: NameDicts): string {
  const raw = (order as unknown as Record<string, unknown>)[field]
  if (field === 'is_urgent') return raw === true ? '是' : '否'
  if (MONEY_FIELD_SET.has(field)) {
    return formatMoney(typeof raw === 'string' || typeof raw === 'number' ? raw : null)
  }
  if (raw === null || raw === undefined || raw === '') return ''
  if (field === 'created_at' || field === 'updated_at') return formatTime(String(raw))
  const dictKey = FK_DICT_KEY[field]
  if (dictKey !== undefined && typeof raw === 'number') {
    return dicts[dictKey].get(raw) ?? String(raw)
  }
  return String(raw)
}

/** 一行 `.layui-form-item` 的「值 span」集合（对标每行都是 `label.itemTitle` + N 个 `span.left`）。 */
function rowByLabel(root: HTMLElement, label: string): HTMLElement | null {
  for (const el of Array.from(root.querySelectorAll<HTMLElement>('label.itemTitle'))) {
    if ((el.textContent ?? '').trim() !== label) continue
    return el.closest<HTMLElement>('.layui-form-item')
  }
  return null
}

/** 写第 `index` 个 `span.left`（行 / 下标不存在时**静默不写**，不抛错）。 */
function setSpan(row: HTMLElement | null, index: number, text: string): void {
  if (row === null) return
  const span = row.querySelectorAll<HTMLElement>('span.left')[index]
  if (span) span.textContent = text
}

/**
 * 「复制订单内容」要复制的那段文本：读**本页已经渲染出来的那些行**（`STANDARD_ROW_LABELS`
 * 的 14 个标签 + 该行 `span.left` 的拼接值，一行一条、`\n` 连接）
 * ⇒「复制到的」与「屏幕上看到的」**必然同源**，不引入第二份字段口径。
 * ⚠️ 只认 `span.left`：`当前接手` 的值节点是 `<a>`、`指定电话` 的 span 没有 `.left`——
 * 两行的值本来就是空，行文本停在标签本身。
 */
export function buildCopyText(root: HTMLElement): string {
  return Object.values(STANDARD_ROW_LABELS)
    .map((label) => {
      const row = rowByLabel(root, label)
      const value =
        row === null
          ? ''
          : Array.from(row.querySelectorAll<HTMLElement>('span.left'))
              .map((span) => (span.textContent ?? '').trim())
              .join('')
      return `${label}${value}`
    })
    .join('\n')
}

/**
 * 把订单详情填进对标的 16 行；本函数是**唯一**的填值出口，
 * 只碰证据件里本来就有的行、不新造任何行（「我方字段」追加卡片已删，见文件头第 15–17 条）。
 */
export function fillStandardRows(root: HTMLElement, order: Order, dicts: NameDicts): void {
  const row = (key: StandardRowKey): HTMLElement | null =>
    rowByLabel(root, STANDARD_ROW_LABELS[key])

  setSpan(row('orderNo'), 0, displayOrderNo(order))

  // 2 游戏区服：第 1 格 = 游戏名；第 2 格 = `/` + 区服名（后 3 格语义未考证 ⇒ 留空）
  setSpan(row('gameServer'), 0, fieldText(order, 'game', dicts))
  const serverName = fieldText(order, 'server', dicts)
  setSpan(row('gameServer'), 1, serverName === '' ? '' : `/${serverName}`)

  // 3 角色信息：只填第 1 格（`game_role_name`），后 3 格留空
  setSpan(row('roleInfo'), 0, order.game_role_name)

  // 4 游戏账号 / 5 子账号
  setSpan(row('gameAccount'), 0, order.game_account)
  setSpan(row('subAccount'), 0, order.game_sub_account)

  // 6 登录密码 / 7 暗号口令：我方无这些列 ⇒ **显式清空**（证据件原文就是空 span；
  //    这里仍写一遍，是为了让本函数**幂等**——被同一份 DOM 调两次也不会留下上一单的值）。
  setSpan(row('password'), 0, '')
  setSpan(row('secret'), 0, '')

  // 8 项目分类 / 9 代练项目（+ 徽章）
  setSpan(row('category'), 0, fieldText(order, 'category', dicts))
  const projectText = order.project_text !== '' ? order.project_text : fieldText(order, 'project', dicts)
  setSpan(row('project'), 0, projectText)
  const badge = row('project')?.querySelector<HTMLElement>('span.layui-badge')
  if (badge) {
    const typeText = orderTypeText(order.order_type)
    if (typeText === '') badge.remove()
    else badge.textContent = typeText
  }

  // 10 订单状态（文字取我方词表；颜色固定 `#289f16`，见「已知会不同」第 14 条）
  setSpan(row('status'), 0, statusText(order.status))

  // 11 订单计时 / 12 当前接手 / 13 指定电话：我方没有这三样数据 ⇒ 一律**不写**（见「已知会不同」6/7/8）。
  //     ⚠️ `订单计时` 那一行第 2 个 `span.left` 里有**对标原文的字面量「还剩」**——
  //     往那一格 `textContent = ''` 会**连「还剩」一起抹掉**（那是改对标的标记，不是填值；
  //     本页第一版就这么错过）。这一行本来就没有可填的字段 ⇒「整行不写」既准确、也没有幂等问题。
  const taker = row('taker')?.querySelector<HTMLElement>('a')
  if (taker) taker.textContent = ''
  // ⚠️ `指定电话` 那一行的 span 没有 `.left` ⇒ `setSpan` 找不到它，也没有可清的值。
  //    别为了「统一写法」给它补 class——那会改掉对标的标记。

  // 14 联系电话：**留空**。对标那一格装的是**虚拟电话**、我方 `customer_phone` 是**客户电话**——
  //    同名不同义，按标签映射会写错语义（E-0068）。
  setSpan(row('contactPhone'), 0, '')
}

/**
 * 把一条推送行填进「代练通」面板的各格（`PLATFORM_FIELD_SELECTORS` 的字段表）。
 * `push === null` ⇒ **清空全部**：空态与**取数失败**两个场景都靠它（失败时留旧值就是「说假话」）。
 * ⚠️ 值全部来自后端推送行，⛔ 本函数不造任何默认值；「交易状态」走 `platformDisplayStatus`
 * 的优先级，⛔ 不在这里再抄一张码表。
 */
export function fillPlatformRows(root: HTMLElement, push: DailiantongPushRow | null): void {
  const set = (key: keyof typeof PLATFORM_FIELD_SELECTORS, text: string): void => {
    const el = root.querySelector<HTMLElement>(PLATFORM_FIELD_SELECTORS[key])
    if (el !== null) el.textContent = text
  }
  if (push === null) {
    for (const key of Object.keys(PLATFORM_FIELD_SELECTORS) as Array<
      keyof typeof PLATFORM_FIELD_SELECTORS
    >) {
      set(key, '')
    }
    syncPlatformActionVisibility(root, null)
    return
  }
  set('serialNo', push.serial_no === null || push.serial_no === '' ? '—' : push.serial_no)
  set('platform', platformDisplayName(push.platform))
  set('price', formatMoney(push.price))
  set('ensure1', formatMoney(push.ensure1))
  set('ensure2', formatMoney(push.ensure2))
  set('timeLimit', `${push.time_limit_hours} 小时`)
  // 「交易状态」：`closed` ⇒「已关闭」→ 后端综合状态 → 本地按 (Status, CancelStatus) 推导
  // → `state_display` 兜底（优先级在 `platformDisplayStatus`）。
  // ⛔ 绝不能只读 `remote_status`：撤销中那一单会一直显示「撤销中」（真机 bug）。
  const stateDisplay =
    typeof push.state_display === 'string' && push.state_display.trim() !== ''
      ? push.state_display
      : platformPushStatusLabel(push)
  const display = platformDisplayStatus(push)
  set(
    'status',
    push.state === 'closed' ? '已关闭' : (display?.label ?? stateDisplay),
  )
  set('cancelStatus', remoteCancelStatusLabel(push.remote_cancel_status) ?? '—')
  // 「撤销申请结算 / 赔付」两格由本函数赋值：有值 ⇒ 两位小数；缺值 ⇒「—」（⛔ 不编 0.00）。
  set('payLevelBal', formatMoney(push.pay_level_bal))
  set('repEnsureBal', formatMoney(push.rep_ensure_bal))
  // 「已结代练费 / 已赔保证金」：后端只在 Status 17/16 给值（其它 null，键也可能缺席）
  // ⇒ 一律走 `formatMoney`：有值两位小数、null / 缺键「—」，⛔ 不编 0.00。
  set('settledPayLevel', formatMoney(push.settled_pay_level_bal))
  set('settledRepEnsure', formatMoney(push.settled_rep_ensure_bal))
  set('lastSynced', push.last_synced_at === null ? '—' : formatTime(push.last_synced_at))
  set('lastError', push.last_error)
  syncPlatformActionVisibility(root, push)
}

/**
 * 按**远端状态**决定「哪几颗动作按钮该露出来」。
 * ⚠️ 这是**可见性**、不是权限也不是业务判据：真正的状态闸在后端（点了会回中文业务错误）；
 * ⛔ 不新建第二张中文码表（码表与后端同值，只在 `platformPush.ts` 一处）。
 * 规则读 `remote_status` / `remote_cancel_status` / `cancel_initiator` / `accept_uid`（详见函数体）：
 * 加价 / 下架只在未接手（11）、上架 11 或 18（已下架）、评价 17（已结算）、锁定 12/13/14、解锁 15；
 * 撤销区按「谁发起 + 撤销到哪一步」决定组合（申请 / 同意 / 取消撤销 / 仲裁，详见函数体）。
 * ⛔ 18（已下架）时**只露「上架」**——它已不在可接列表里、再下架没有意义（后端下架闸也只放 11），
 * 两颗一起露会让客服点到一个必然被后端拒的按钮。
 * `push === null`（没有推送行 / 取数失败）⇒ 全部收起（屏幕在说「不知道」时不能摆出可点的动作）。
 */
export function syncPlatformActionVisibility(
  root: HTMLElement,
  push: DailiantongPushRow | null,
): void {
  const panel = root.querySelector<HTMLElement>(
    `.layui-tab[lay-filter="${PLATFORM_SUB_TAB_FILTER}"]`,
  )
  if (panel === null) return
  const setVisible = (selector: string, visible: boolean): void => {
    const el = panel.querySelector<HTMLElement>(selector)
    if (el !== null) el.style.display = visible ? '' : 'none'
  }
  /**
   * ⭐ g3：按后端下发的 `arbitration_available` 决定**能不能点**（不是收起来）。
   *
   * ⚠️ 只有**明确 `false`** 才禁用；字段缺席（后端未升级）/ `null` ⇒ 保持可点、点了让上游原文说话。
   * ⛔ 页面不自己算「是否满 1 小时」（时间由后端按上游规则算）。
   */
  const setDisabled = (selector: string, disabled: boolean): void => {
    const el = panel.querySelector<HTMLElement>(selector)
    if (el === null) return
    if (disabled) el.setAttribute(PLATFORM_BLOCKED_ATTR, '1')
    else el.removeAttribute(PLATFORM_BLOCKED_ATTR)
    el.classList.toggle(PLATFORM_BUSY_CLASS, disabled)
    if (disabled) el.setAttribute('aria-disabled', 'true')
    else el.removeAttribute('aria-disabled')
  }
  /**
   * **没有 `order.platform_write` ⇒ 这一页的写动作一颗都不画**（fail-closed）：撤回 / 改价 /
   * 补时 / 补款 / 加价 / 撤销三颗 / 仲裁 / 锁定解锁 / 确认完单 / 编辑 / 重发 / 截图上传 / 拉黑，
   * 以及同一页其余平台写动作（留言 / 上架 / 下架 / 评价 /「去发单」）**一并**收起；
   * 读面（10 格值 / 留言历史 / 进度图）**照常显示**。判据唯一出处 `@/layui-island/perms::canPlatformWrite`；
   * 收口在这里 = 平台面板所有按钮可见性的唯一出口（`fillPlatformRows` 每次都调本函数）。
   */
  if (!platformWriteAllowedNow()) {
    for (const selector of Object.values(PLATFORM_ACTION_SELECTORS)) {
      setVisible(selector, false)
    }
    setVisible(PLATFORM_UPLOAD_DRAG_SELECTOR, false)
    setVisible(DLT_PUSH_BTN_SELECTOR, false)
    const blockedHint = panel.querySelector<HTMLElement>(DLT_CANCEL_HINT_SELECTOR)
    if (blockedHint !== null) blockedHint.textContent = ''
    return
  }
  const status = push === null ? null : push.remote_status
  const cancel = push === null ? null : push.remote_cancel_status
  const unaccepted = status === 11
  // 18 = 已下架：只露「上架」——它已经不在可接列表里，再下架没有意义；后端下架闸也只放 11。
  const offShelf = status === 18
  const settled = status === 17
  // 「已接手」= 不是临时单、也不是未接手（10 临时 / 11 未接手都不算）。
  const accepted = status !== null && status !== 10 && !unaccepted && !offShelf
  const inArbitration = cancel === 14 || cancel === 15
  // 撤销区三档：`cancelPending` = 协商撤销中（打手 / 客服在谈）；`cancelDone` = 撤销已处理完；
  // `cancelNegotiable` = 远端「已接手」（Status 12–15）⇒ 我们还能发起协商撤销。
  const cancelPending = cancel === 11
  const cancelDone = cancel === 12 || cancel === 15 || cancel === 16
  const cancelNegotiable = status !== null && status >= 12 && status <= 15
  // 发起方：`'us'` = 我们自己发起的（不能再「同意」一次）；`'taker'` / 空串 = 打手发起或判不出。
  const cancelInitiator = push === null ? '' : (push.cancel_initiator ?? '')
  // 「同意撤销」：协商撤销中、不是我们发起的、且后端没说「现在不能同意」。
  // ⚠️ 只认 `agree_cancel_available === false`（键缺席 / null = 后端未升级 ⇒ 按发起方降级）。
  const agreeCancelVisible =
    cancelPending &&
    cancelInitiator !== 'us' &&
    push?.agree_cancel_available !== false
  // 「申请撤销」：远端可协商、且撤销还没进「协商中 / 已处理」。
  // `CancelStatus=13`（撤销已取消）与 `null`（还没回读）都算「没有进行中的撤销」⇒ 可发起。
  const applyCancelVisible =
    cancelNegotiable && !cancelPending && !cancelDone && !inArbitration
  // 「取消撤销」：只有**我们自己发起**的协商撤销能自己撤回；打手发起的那是在等我们同意 / 仲裁。
  const requestCancelVisible = cancelPending && cancelInitiator === 'us'
  // 「申请仲裁」：上游只在**协商撤销中**（`CancelStatus=11`）可能允许；`CancelStatus` 未知时
  // 沿用旧口径（已接手未结算未介入），让旧回包仍能点到、由后端原文说话。
  const arbitrationVisible =
    cancelPending || (cancel === null && accepted && !settled)
  // 确认完单：证据件只在「等待验收」(13) 露出来（g3 起已接真，后端还会再判一次状态）。
  setVisible(PLATFORM_COMPLETE_SELECTOR, status === 13)
  setVisible(PLATFORM_ACTION_SELECTORS.upPrice, unaccepted)
  setVisible(PLATFORM_ACTION_SELECTORS.offSell, unaccepted)
  setVisible(PLATFORM_ACTION_SELECTORS.onSell, unaccepted || offShelf)
  setVisible(PLATFORM_ACTION_SELECTORS.value, settled)
  // 12 / 13 / 14 都能锁（14 上锁 ⇒ 状态变 15）；15（被我们锁定）才露解锁。
  setVisible(PLATFORM_ACTION_SELECTORS.lock, status === 12 || status === 13 || status === 14)
  setVisible(PLATFORM_ACTION_SELECTORS.unlock, status === 15)
  setVisible(PLATFORM_ACTION_SELECTORS.arbitration, arbitrationVisible)
  setVisible(PLATFORM_ACTION_SELECTORS.applyCancel, applyCancelVisible)
  setVisible(PLATFORM_ACTION_SELECTORS.agreeCancel, agreeCancelVisible)
  setVisible(PLATFORM_ACTION_SELECTORS.requestCancel, requestCancelVisible)
  setDisabled(
    PLATFORM_ACTION_SELECTORS.arbitration,
    push !== null && push.arbitration_available === false,
  )
  setVisible(
    PLATFORM_ACTION_SELECTORS.blacklist,
    accepted && push !== null && (push.accept_uid ?? '') !== '',
  )
  // 撤销区那句说明；`push === null`（没有推送行 / 取数失败）⇒ 清空（屏幕在说「不知道」时不能补一句话）。
  const hint = panel.querySelector<HTMLElement>(DLT_CANCEL_HINT_SELECTOR)
  if (hint !== null) hint.textContent = platformCancelHint(push)
}

/**
 * 面板顶部状态区的三态：`hidden`（有数据）/ `empty`（没有推送行，带「去发单」）/ `error`（取数失败）。
 * `error` 的文案 = `PLATFORM_LOAD_ERROR_PREFIX` + **后端原文**；面板里这行是「⛔ 不静默空白」的保底。
 */
export type PlatformStatusState = 'hidden' | 'empty' | 'error'

export function setPlatformStatus(
  root: HTMLElement,
  state: PlatformStatusState,
  message = '',
): void {
  const box = root.querySelector<HTMLElement>(PLATFORM_STATUS_SELECTOR)
  if (box === null) return
  if (state === 'hidden') {
    box.style.display = 'none'
    return
  }
  box.style.display = ''
  const text = box.querySelector<HTMLElement>(PLATFORM_STATUS_TEXT_SELECTOR)
  if (text !== null) {
    text.textContent = message
    // 空态是中性灰、取数失败是红（可见的错误提示）
    text.style.color = state === 'error' ? '#ff0000' : '#999'
  }
  const button = box.querySelector<HTMLElement>(DLT_PUSH_BTN_SELECTOR)
  if (button !== null) button.style.display = state === 'empty' ? '' : 'none'
}

/* ── 平台留言历史 / 进度图：渲染出口 ─────────────────────────
 * ⚠️ 两块列表都**只用 DOM API + `textContent`**（⛔ 不拼 HTML）：留言正文 / 图片说明都是
 * 上游原值（第三方可写），拼串就是一条 XSS。
 */

/** 读面失败时的「重试」按钮（锚点；点击由页面自己的委托处理，见 `DLT_RETRY_BTN_SELECTOR`）。 */
function buildDltRetryButton(): HTMLElement {
  const button = document.createElement('a')
  button.className = `layui-btn layui-btn-xs ${DLT_RETRY_BTN_CLASS}`
  button.setAttribute('href', 'javascript:;')
  button.textContent = DLT_RETRY_BTN_TEXT
  return button
}

/** 留言区的提示行（加载中 / 空 / 失败）：三态都**可见**，⛔ 不静默空白。 */
export function setPlatformMessageNotice(
  root: HTMLElement,
  text: string,
  options: { error?: boolean; retry?: boolean } = {},
): void {
  const box = root.querySelector<HTMLElement>(PLATFORM_MSG_LIST_SELECTOR)
  if (box === null) return
  box.textContent = ''
  const line = document.createElement('p')
  line.className = 'linespan dltMsgNotice'
  line.setAttribute(
    'style',
    `line-height:28px;margin-left:5px;color:${options.error === true ? '#ff0000' : '#999'}`,
  )
  line.textContent = text
  box.appendChild(line)
  if (options.retry === true) box.appendChild(buildDltRetryButton())
}

/**
 * 把留言列表渲染进 `#dltlogdiv`：每条两行（第 1 行 = **发送方** + `time` + `is_system` 的
 * 「系统」徽标，第 2 行 = 正文）。
 * ⚠️ `time` / `sender` 都是上游原样串 ⇒ **不重排、不翻译**（认不出格式就原样显示，⛔ 不编时间）。
 */
export function renderPlatformMessages(
  root: HTMLElement,
  messages: PlatformPushMessage[],
): void {
  const box = root.querySelector<HTMLElement>(PLATFORM_MSG_LIST_SELECTOR)
  if (box === null) return
  box.textContent = ''
  for (const message of messages) {
    const time = (message.time ?? '').trim()
    const sender = (message.sender ?? '').trim()
    const head = document.createElement('p')
    head.className = 'linespan dltMsgTime'
    head.setAttribute('style', 'line-height:28px;margin-left:5px;color:#5FB878')
    // 系统消息的发送方常常就是「系统」——徽标已经说了一次，⛔ 不再重复一个「系统 系统」。
    if (sender !== '' && !(message.is_system && sender === PLATFORM_MSG_SYSTEM_LABEL)) {
      const who = document.createElement('span')
      who.className = PLATFORM_MSG_SENDER_CLASS
      who.textContent = `${sender} `
      head.appendChild(who)
    }
    if (time !== '') head.appendChild(document.createTextNode(`${time} `))
    if (message.is_system) {
      const tag = document.createElement('span')
      tag.className = `layui-badge layui-bg-blue ${PLATFORM_MSG_SYSTEM_CLASS}`
      tag.textContent = PLATFORM_MSG_SYSTEM_LABEL
      head.appendChild(tag)
    }
    // 发送方 / 时间 / 系统标记都缺（脏数据）⇒ 不留一行空标题
    if ((head.textContent ?? '').trim() !== '') box.appendChild(head)
    const body = document.createElement('p')
    body.className = 'linespan dltMsgText'
    body.setAttribute(
      'style',
      'line-height:28px;margin-left:5px;color:#76838F;font-size:14px',
    )
    body.textContent = message.text ?? ''
    box.appendChild(body)
  }
}

/**
 * 留言区图片（`messages` 响应里的 `images`）的提示行：加载中 / 空 / 失败都**可见**，
 * ⛔ 不静默空白。失败原因 + 「重试」与留言区同一套口径。
 */
export function setPlatformMessageImagesNotice(
  root: HTMLElement,
  text: string,
  options: { error?: boolean; retry?: boolean } = {},
): void {
  const box = root.querySelector<HTMLElement>(PLATFORM_MSG_IMAGE_LIST_SELECTOR)
  if (box === null) return
  box.textContent = ''
  const line = document.createElement('p')
  line.className = 'dltMsgImageNotice'
  line.setAttribute(
    'style',
    `width:100%;line-height:24px;margin:4px 0;color:${
      options.error === true ? '#ff0000' : '#999'
    }`,
  )
  line.textContent = text
  box.appendChild(line)
  if (options.retry === true) box.appendChild(buildDltRetryButton())
}

/**
 * 把「本单图片」渲染进留言区块下方的 `.dltMsgImageList`（缩略图墙）。
 * 有 `url` 的缩略图挂 `PLATFORM_PROGRESS_IMAGE_CLASS` ⇒ 复用页面已有的点击委托开大图；
 * `url` 为空显示「无预览」占位块（⛔ 不画 `<img src="">` 破图标）。
 */
export function renderPlatformMessageImages(
  root: HTMLElement,
  items: PlatformPushProgressItem[],
): void {
  const box = root.querySelector<HTMLElement>(PLATFORM_MSG_IMAGE_LIST_SELECTOR)
  if (box === null) return
  box.textContent = ''
  for (const item of items) {
    const url = (item.url ?? '').trim()
    if (url !== '') {
      const image = document.createElement('img')
      image.src = url
      image.className = `${PLATFORM_PROGRESS_IMAGE_CLASS} ${PLATFORM_MSG_IMAGE_CLASS}`
      image.alt = (item.caption ?? '').trim() || PLATFORM_PROGRESS_PREVIEW_TITLE
      image.setAttribute(
        'style',
        'width:80px;height:80px;object-fit:cover;border:1px solid #eee;cursor:zoom-in',
      )
      box.appendChild(image)
    } else {
      const placeholder = document.createElement('div')
      placeholder.className = 'dltNoPreview'
      placeholder.setAttribute(
        'style',
        'width:80px;height:80px;line-height:80px;color:#999;text-align:center;background:#f2f2f2;font-size:12px',
      )
      placeholder.textContent = PLATFORM_PROGRESS_NO_PREVIEW_TEXT
      box.appendChild(placeholder)
    }
  }
}

/** 进度图区的提示（加载中 / 空 / 失败）：占一个 `li`，不参与图片卡片的排版。 */
export function setPlatformProgressNotice(
  root: HTMLElement,
  text: string,
  options: { error?: boolean; retry?: boolean } = {},
): void {
  const list = root.querySelector<HTMLElement>(PLATFORM_PROGRESS_LIST_SELECTOR)
  if (list === null) return
  list.textContent = ''
  const item = document.createElement('li')
  item.className = 'dltProgressNotice'
  item.setAttribute(
    'style',
    `float:none;height:auto;border:none;margin:20px 10px;color:${
      options.error === true ? '#ff0000' : '#999'
    }`,
  )
  item.textContent = text
  if (options.retry === true) {
    item.appendChild(document.createElement('br'))
    item.appendChild(buildDltRetryButton())
  }
  list.appendChild(item)
}

/**
 * 把进度图渲染进 `.platfromOrderImgList`：每一项 = 图片 + 时间 + 说明。
 * ⚠️ `url` 为空 ⇒ 显示「无预览」占位块，**⛔ 不写 `<img src="">`**（那会画一个破图标）。
 * ⚠️ 占位块没有大图可开、⛔ 不要给它挂 `PLATFORM_PROGRESS_IMAGE_CLASS`。
 */
export function renderPlatformProgress(
  root: HTMLElement,
  items: PlatformPushProgressItem[],
): void {
  const list = root.querySelector<HTMLElement>(PLATFORM_PROGRESS_LIST_SELECTOR)
  if (list === null) return
  list.textContent = ''
  for (const item of items) {
    const card = document.createElement('li')
    card.className = 'dltProgressItem'
    const url = (item.url ?? '').trim()
    if (url !== '') {
      const image = document.createElement('img')
      image.src = url
      image.className = PLATFORM_PROGRESS_IMAGE_CLASS
      image.alt = (item.caption ?? '').trim() || PLATFORM_PROGRESS_PREVIEW_TITLE
      card.appendChild(image)
    } else {
      const placeholder = document.createElement('div')
      placeholder.className = 'dltNoPreview'
      placeholder.setAttribute(
        'style',
        'width:220px;height:200px;line-height:200px;color:#999;text-align:center;background:#f2f2f2',
      )
      placeholder.textContent = PLATFORM_PROGRESS_NO_PREVIEW_TEXT
      card.appendChild(placeholder)
    }
    const time = document.createElement('div')
    time.className = 'dltProgressTime'
    time.textContent = (item.time ?? '').trim()
    card.appendChild(time)
    const caption = document.createElement('div')
    caption.className = 'dltProgressCaption'
    caption.textContent = (item.caption ?? '').trim()
    card.appendChild(caption)
    list.appendChild(card)
  }
}

/**
 * 「去发单」打开的弹层高度（屏高分支）。
 * ⚠️ 与 `order-orderlist.ts::pushJdpLayerHeight` 是**同源两份实现**（两个页面各自是独立 island
 * 入口，不抽公共模块是为了不动那边的「开层调用点计数」网）——**改一边必须同一次改另一边**。
 */
function pushJdpLayerHeight(screenHeight: number = window.screen.height): string {
  if (screenHeight <= 1080) return '95%'
  if (screenHeight <= 1440) return '90%'
  if (screenHeight <= 1600) return '80%'
  return '70%'
}

/**
 * 打开我们自己的发单页（iframe 弹层，`layui-island/page.html?page=order-pushjdp&order_no=…`）。
 * ⚠️ 标题里用的是**展示单号**（`displayOrderNo`）、寻址用的 `order_no` 仍是 20 位系统单号
 * ——两者形状不同，⛔ 不要互相替代；`title` 是 HTML 上下文 ⇒ 进层前 `escapeHtml`。
 * `titleOverride`：「重发此单」要的标题是「重发平台订单」，此时不再拼展示单号；空串 = 默认标题。
 */
/** 发单弹层的 layer 索引（`openPushJdpPage` 记下、发单成功消息里关它）。 */
let pushJdpLayerIndex: number | null = null

export function openPushJdpPage(
  layer: unknown,
  orderNo: string,
  viewNo: string,
  titleOverride = '',
): void {
  if (orderNo === '') return
  const base = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
  const index = (layer as { open: (options: unknown) => unknown }).open({
    title:
      titleOverride !== ''
        ? titleOverride
        : `${escapeHtml(viewNo !== '' ? viewNo : orderNo)}发单到交易平台：请确认发单内容`,
    shade: [0.3, '#333'],
    shadeClose: true,
    maxmin: true,
    type: 2,
    skin: 'layui-layer-lan',
    anim: 8,
    area: ['1280px', pushJdpLayerHeight()],
    content: `${base}/layui-island/page.html?page=order-pushjdp&order_no=${encodeURIComponent(orderNo)}`,
    end: () => {
      pushJdpLayerIndex = null
    },
  })
  pushJdpLayerIndex = typeof index === 'number' ? index : null
}

/** 拉一次 4 个 FK 的字典（区服 / 分类 / 项目按本单的 `game` / `category` 过滤）。 */
async function loadNameDicts(order: Order): Promise<NameDicts> {
  const [games, servers, categories, projects] = await Promise.all([
    fetchGames(),
    fetchServers(order.game),
    fetchCategories(order.game),
    order.category === null ? Promise.resolve<ConfigRow[]>([]) : fetchProjects(order.category),
  ])
  const toMap = (rows: { id: number; name: string }[]): Map<number, string> =>
    new Map(rows.map((r) => [r.id, r.name] as const))
  return {
    games: toMap(games),
    servers: toMap(servers),
    categories: toMap(categories),
    projects: toMap(projects),
  }
}

/**
 * 受限模式挂载：无 `order.view_detail` / `order.view_content` 的账号（典型 = 代练员）走这一条。
 * 与全量模式的差异：**不取证据件**（那 16 行骨架对打手是「标签在、值全空」的静默骨架）；
 * 取数改打受限端点（`fetchMyOrderDetail`）；**订单 / 操作日志一概不请求**（`/audit/` 同样 403）；
 * 「代练通」页签、平台写按钮、拆单 / 复制等全量功能**全部不装**；失败有人话
 * （403 ⇒ 固定文案 +「关闭」，其它 ⇒ 后端 `msg` 原样 +「重试」）。
 */
async function mountRestrictedOrderViewPage(
  root: HTMLElement,
  layui: Record<string, unknown>,
): Promise<() => void> {
  const layer = layui.layer as unknown as Record<string, unknown>
  const lay = layui.lay as { clipboard: { writeText: (options: unknown) => void } }
  const errMsg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: ERROR_MSG_MS })
  }
  const errText = (e: unknown): string => (e instanceof Error ? e.message : '操作失败')
  const orderNo = orderNoFromSearch(window.location.search)
  document.title = `${orderNo}${TITLE_SUFFIX}`

  async function load(): Promise<void> {
    if (orderNo === '') {
      renderRestrictedError(root, `缺少订单号参数（${ORDER_NO_PARAM}）`, false)
      return
    }
    root.replaceChildren(restrictedNotice(RESTRICTED_LOADING_TEXT))
    try {
      const detail = await fetchMyOrderDetail(orderNo)
      document.title = `${restrictedDisplayNo(detail)}${TITLE_SUFFIX}`
      renderRestrictedDetail(root, detail)
    } catch (e) {
      if (e instanceof HttpError && e.status === 403) {
        // 403 = 不是本人的单 / 无 `my_orders` 节点：**固定人话** + 关闭键。⛔ 不再渲染空骨架。
        renderRestrictedError(root, RESTRICTED_FORBIDDEN_TEXT, true)
        return
      }
      // 其它失败：msg 原样 + 页内可见的失败块 + 重试（toast 会消失、页内块不会）。
      const message = errText(e)
      errMsg(message)
      renderRestrictedError(root, message, false)
    }
  }

  /** 行内「复制」键（单号 + 游戏账号三字段，H5 同款）：`data-copy` 带要复制的值。 */
  const onCopyClick = (event: Event): void => {
    const button = (event.target as HTMLElement).closest<HTMLElement>(`.${RESTRICTED_COPY_BTN_CLASS}`)
    if (!button) return
    event.preventDefault()
    lay.clipboard.writeText({
      text: button.dataset.copy ?? '',
      done: () => {
        ;(layer.msg as (t: string, o?: unknown) => void)(RESTRICTED_COPY_SUCCESS_MSG, { time: 1500 })
      },
      error: () => {
        ;(layer.msg as (t: string, o?: unknown) => void)(RESTRICTED_COPY_FAIL_MSG, { time: 1500 })
      },
    })
  }
  /** 非 403 失败块的「重试」：重新走一次取数。 */
  const onRetryClick = (event: Event): void => {
    if (!(event.target as HTMLElement).closest(`.${RESTRICTED_RETRY_BTN_CLASS}`)) return
    event.preventDefault()
    void load()
  }
  /** 403 失败块的「关闭」：关掉承载本页的弹层（标准 iframe 关闭法；不在弹层里则 no-op）。 */
  const onCloseClick = (event: Event): void => {
    if (!(event.target as HTMLElement).closest(`.${RESTRICTED_CLOSE_BTN_CLASS}`)) return
    event.preventDefault()
    closeIslandLayer(window)
  }
  root.addEventListener('click', onCopyClick)
  root.addEventListener('click', onRetryClick)
  root.addEventListener('click', onCloseClick)

  await load()

  return () => {
    root.removeEventListener('click', onCopyClick)
    root.removeEventListener('click', onRetryClick)
    root.removeEventListener('click', onCloseClick)
    root.replaceChildren()
  }
}

/**
 * 挂载本页。**导出**是为了让测试能挂整页并断言真实出口；`default.mount` 只是它的薄包装。
 * 挂载第一步先判「全量 / 受限」：持 `order.view_detail` ∨ `order.view_content` ⇒ 全量行为；
 * 两颗都没有（打手）⇒ 走受限挂载；身份探不到 ⇒ 按全量降级（后端 403 兜底仍在）。
 */
export async function mountOrderOrderViewPage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  /** ⚠️ 先挂样式再塞标记（E-0067），顺序写反会闪一下没样式的页面。 */
  // 剪贴板出口 `layui.lay.clipboard` 随 all-in-one 一起可用（真浏览器探针实测，见下面 `const lay`）；
  // `element` 由 `bootstrapIsland` 自己补（页签切换要用）。
  const layui = await bootstrapIsland(['form', 'layer', 'table'])
  const { fullDetail, changeTakerPrice: canChangeTakerPrice, addEditOrder: canAddEditOrder } = await resolveOrderViewPermissions()
  if (fullDetail === false) {
    return mountRestrictedOrderViewPage(
      root,
      layui as unknown as Record<string, unknown>,
    )
  }
  /**
   * 本页「代练通」页签里的**平台写按钮**一起挂 `order.platform_write` 节点：**没权限不画**
   * （fail-closed，统一在 `syncPlatformActionVisibility` 一处收口）。
   * 判据（含后端没这个节点时的降级）全在 `@/layui-island/perms::resolvePlatformWritePermission`。
   * ⚠️ 必须在切到该页签（首次渲染平台面板）之前判定。
   */
  await resolvePlatformWritePermission()
  const res = await fetch(`${import.meta.env.BASE_URL}${ARTIFACT_URL.replace(/^\//, '')}`)
  if (!res.ok) throw new Error(`证据件取不到（HTTP ${res.status}）：${ARTIFACT_URL}`)
  root.innerHTML = await res.text()
  // 「代练通」页签是**静态加**的；⚠️ 必须在 `initIslandElements` 之前追加。
  injectPlatformTab(root)
  const platformTab = root.querySelector<HTMLElement>(PLATFORM_TAB_ITEM_SELECTOR)
  const splitButton = root.querySelector<HTMLElement>('.spliteorder')
  if (splitButton !== null) splitButton.style.display = 'none'
  // 页签要等标记进来之后再绑（它绑的是标记里的 `.layui-tab`）。
  initIslandElements(layui)

  const form = layui.form as unknown as Record<string, unknown>
  const layer = layui.layer as unknown as Record<string, unknown>
  const table = layui.table as unknown as Record<string, unknown>
  /**
   * layui 的剪贴板出口挂在 **`layui.lay`** 上（不是 `layui.util`：那个模块的键里没有 `clipboard`）。
   * 签名 `{ text, done, error }`。
   */
  const lay = (layui as unknown as Record<string, unknown>).lay as {
    clipboard: { writeText: (options: unknown) => void }
  }
  /**
   * `layer.open({type:1, content: <节点>})` 的正确形态（裸 DOM 节点会让 layer 抛错、「点了没反应」）：
   *  1. 先把节点挂进 `document.body`（layer 的 jQuery 分支要能在文档里 `parents()` / 回填位置）；
   *  2. `content: layui.$(节点)`（**jQuery 形状**——裸节点没有 `.parents`，layer 在 `creat` 里抛错）；
   *  3. `end: () => 节点.remove()`——layui 关层只 `unwrap`、**不删节点**，不清就每次点都留一个。
   * ⛔ 别改回「content 直接传节点」；也别只传 HTML 字符串（表单上的监听 / 引用全会丢）。
   */
  const layerNode = (el: HTMLElement): unknown => {
    document.body.appendChild(el)
    return (layui as unknown as { $: (node: Element) => unknown }).$(el)
  }
  /**
   * 错误提示拉长到 6000ms——权限 / 业务失败必须看得清（同 `emp-myemp.ts`）。
   * 空内容提示另走对标的 `{time:1000}`，这里不包一层没人用的 `islandMsg`。
   */
  const errMsg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: ERROR_MSG_MS })
  }
  /** 可显示的错误文本。**不自造文案**：`request()` 抛出的 `message` 就是后端 `msg` 原文。 */
  const errText = (e: unknown): string => (e instanceof Error ? e.message : '操作失败')
  /**
   * 「尚未开放」的提示（1500ms，与其它复刻页的 `islandMsg` 同值）——代练通页签里的动作按钮走这里。
   * ⚠️ 与 `errMsg`（6000ms）分开：那是**失败**提示，这里是**功能未开放**的中性提示。
   */
  const notOpenMsg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 1500 })
  }
  // T8：代练通页签里的按钮点了只提示「尚未开放」（零请求）；「点我刷新当前页」保持原生 reload。
  const unbindPlatform = bindPlatformNotOpen(root, notOpenMsg)

  const orderNo = orderNoFromSearch(window.location.search)

  /** 页面标题照对标 `<单号>订单详情` 的形状；页头那一行与它是**同一个值**（见 displayOrderNo）。 */
  document.title = `${orderNo}${TITLE_SUFFIX}`

  // 隐藏域照对标保留（`#orderId` 是页面自己带的 20 位系统单号；`#viewType` / `#tid` 照原文不动）。
  const hidden = root.querySelector<HTMLInputElement>('#orderId')
  if (hidden) hidden.value = orderNo

  if (orderNo === '') {
    errMsg(`缺少订单号参数（${ORDER_NO_PARAM}）`)
    ;(form.render as () => void)()
    return () => {
      unbindPlatform()
      root.innerHTML = ''
    }
  }

  /* ── 左下三颗按钮 ──────────────────────────────────────────────────────
   *
   * `刷新本界面` **不接线**：那颗 `<a>` 自带 `href="javascript:location.reload(true);"`，
   * island iframe 里点它 = 重载本页 ⇒ 重新取数（订单详情 + 两块日志一起重取）。
   * ⛔ 不要为了「统一」把它改成 `void(0)` 再挂处理器（那会改掉对标的标记）。
   * `#copybtn` 与 `.spliteorder` 走下面两个委托。
   * ⛔ 三颗都**不改证据件的标记结构**（页内两段 `<style>` 有逐字钉子）。
   */
  const onCopyClick = (event: Event): void => {
    if (!(event.target as HTMLElement).closest('#copybtn')) return
    // `javascript:void(0)` 的默认动作没必要执行（我们自己的处理器负责全部行为）。
    event.preventDefault()
    lay.clipboard.writeText({
      // 文本**读已渲染的 DOM**（不另取一份订单对象、不引入第二份字段口径）。
      text: buildCopyText(root),
      // 两句提示逐字照对标；失败那句的第二参也是 `'error'`。
      done: () => {
        ;(layer.msg as (t: string) => void)(COPY_SUCCESS_MSG)
      },
      error: () => {
        ;(layer.msg as (t: string, o?: unknown) => void)(COPY_ERROR_MSG, 'error')
      },
    })
  }
  const onSplitClick = (event: Event): void => {
    if (!(event.target as HTMLElement).closest('.spliteorder')) return
    event.preventDefault()
    if (splitButton?.style.display === 'none') return
    void openSplitDialog()
  }
  function updateSplitButton(source: Order): boolean {
    const allowed = source.status !== 'COMPLETED' || (canAddEditOrder && source.can_continue_split === true)
    if (splitButton !== null) splitButton.style.display = allowed ? '' : 'none'
    return allowed
  }
  async function openSplitDialog(): Promise<void> {
    try {
      const source = await fetchOrder(orderNo)
      if (!updateSplitButton(source)) {
        errMsg('当前订单无法继续拆分，请刷新本界面')
        return
      }
      const members = source.split_id ? await fetchSplitMembers(source.split_id) : []
      const legacy = source.split_kind === 'LEGACY'
      const anchor = members.find((member) => member.split_is_balance_anchor) ?? { ...source, split_is_balance_anchor: true }
      const canChangePrice = canChangeTakerPrice
      const editableRow = (member: Order | (typeof members)[number], label: string, sourceRow = false): string => {
        const isMain = member.order_no === anchor.order_no
        const price = isMain
          ? `<strong>${escapeHtml(formatMoney(member.total_price))}</strong>`
          : sourceRow
          ? canChangePrice
            ? `<input inputmode="decimal" value="${escapeHtml(member.total_price)}" data-source-price />`
            : `<strong>${escapeHtml(formatMoney(member.total_price))}</strong>`
          : canChangePrice
            ? `<input inputmode="decimal" value="${escapeHtml(member.total_price)}" data-existing-price data-original-price="${escapeHtml(member.total_price)}" />`
            : `<strong>${escapeHtml(formatMoney(member.total_price))}</strong>`
        return `<div class="tt-split-member ${isMain ? 'tt-split-anchor' : sourceRow ? 'tt-split-source' : ''}" ${sourceRow ? '' : `data-existing-row data-order="${escapeHtml(member.order_no)}"`}><b>${escapeHtml(member.order_no)}</b><textarea rows="1" aria-label="${label}" ${sourceRow ? 'data-remaining-project' : `data-existing-project data-original="${escapeHtml(member.project_text)}"`}>${escapeHtml(member.project_text)}</textarea><label>接单价${price}</label><em>${isMain ? '组主单' : sourceRow ? '本次发起订单' : '已有订单'}</em></div>`
      }
      const mainRow = editableRow(anchor.order_no === source.order_no ? source : anchor, '原单项目内容', anchor.order_no === source.order_no)
      const sourceRow = anchor.order_no === source.order_no ? '' : editableRow(source, '本次发起订单内容', true)
      const memberRows = members.filter((member) => member.order_no !== source.order_no && member.order_no !== anchor.order_no).map((member) => editableRow(member, '已有子单内容')).join('')
      const body = `<style>.tt-peer-split{padding:18px;color:#333;font-size:13px}.tt-peer-split label{display:grid;gap:7px;color:#555;min-width:0}.tt-peer-split textarea{width:min(100%,620px);min-height:32px;box-sizing:border-box;padding:6px 8px;font:inherit;line-height:18px;border:1px solid #ddd;border-radius:5px;resize:vertical}.tt-peer-split input{width:110px;max-width:100%;box-sizing:border-box;padding:8px;border:1px solid #ddd;border-radius:5px}.tt-split-members{display:grid;gap:8px;padding:10px;margin-bottom:14px;background:#faf8f3;border:1px solid #e8e1d4;border-radius:7px}.tt-split-member{display:grid;grid-template-columns:170px minmax(0,1fr) 150px 120px;gap:10px;align-items:center;padding:9px;border:1px solid #e8e1d4;border-radius:6px;background:white;min-width:0}.tt-split-member em{font-style:normal;color:#837b6f}.tt-split-source{background:#f1eee7}.tt-split-anchor{background:#f1eee7}.tt-split-new-row{display:grid;grid-template-columns:170px minmax(0,1fr) 150px 120px;gap:10px;align-items:center;padding:9px;border:1px dashed #c7b89b;border-radius:6px;background:#fffdf8;min-width:0}.tt-split-total{padding:10px;background:#f4f0e8;border-radius:5px}.tt-split-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}@media(max-width:760px){.tt-split-member,.tt-split-new-row{grid-template-columns:minmax(0,1fr)}.tt-peer-split textarea{width:100%}}</style><div class="tt-peer-split" data-peer-split>
        <section class="tt-split-members"><b>订单组</b>${mainRow}${sourceRow}${memberRows}<div data-new-orders></div><button type="button" class="layui-btn layui-btn-primary" data-add-new>＋ 新增子单</button><p class="tt-split-total" data-total-summary></p></section>
        <div class="tt-split-actions"><button type="button" class="layui-btn layui-btn-normal" data-save-split>保存拆分</button><button type="button" class="layui-btn layui-btn-primary" data-close-split>关闭</button></div>
      </div>`
      const open = layer.open as (options: unknown) => number
      const index = open({
        type: 1,
        title: '拆分订单',
        shade: [0.3, '#333'],
        shadeClose: false,
        area: ['860px', legacy ? '620px' : '720px'],
        content: body,
        success: () => {
          const panel = document.querySelector<HTMLElement>('.layui-layer-content .tt-peer-split')
          if (panel === null) return
          const updateSummary = (): void => {
            const summary = panel.querySelector<HTMLElement>('[data-total-summary]')
            if (summary === null) return
            const cents = (raw: string): number => {
              const match = /^(-?)(\d+)(?:\.(\d{0,2}))?$/.exec(raw.trim())
              if (match === null) return 0
              const value = Number(match[2]) * 100 + Number((match[3] ?? '').padEnd(2, '0'))
              return match[1] === '-' ? -value : value
            }
            const total = members.length > 0 ? members.reduce((sum, member) => sum + cents(member.total_price), 0) : cents(source.total_price)
            const newCents = Array.from(panel.querySelectorAll<HTMLInputElement>('[data-new-price]')).reduce((sum, input) => sum + cents(input.value), 0)
            const sourcePrice = panel.querySelector<HTMLInputElement>('[data-source-price]')?.value ?? source.total_price
            let adjustment = source.order_no === anchor.order_no ? 0 : cents(sourcePrice) - cents(source.total_price)
            for (const row of Array.from(panel.querySelectorAll<HTMLElement>('[data-existing-row]'))) {
              const input = row.querySelector<HTMLInputElement>('[data-existing-price]')
              if (input) adjustment += cents(input.value) - cents(input.dataset.originalPrice ?? input.value)
            }
            const sourceIsAnchor = source.order_no === anchor.order_no
            const projected = cents(anchor.total_price) - adjustment - (sourceIsAnchor ? newCents : 0)
            const projectedSource = sourceIsAnchor ? projected : cents(sourcePrice) - newCents
            summary.textContent = `组总价 ${formatMoney(total / 100)} → 保存后 ${formatMoney(total / 100)}（保持不变） · 新增 ${formatMoney(newCents / 100)} · 原单预计 ${formatMoney(projected / 100)} · 发起单预计 ${formatMoney(projectedSource / 100)}`
          }
          panel.addEventListener('input', updateSummary)
          updateSummary()
          panel.querySelector<HTMLElement>('[data-close-split]')?.addEventListener('click', () => {
            ;(layer.close as (i: number) => void)(index)
          })
          panel.querySelector<HTMLElement>('[data-add-new]')?.addEventListener('click', () => {
            const list = panel.querySelector<HTMLElement>('[data-new-orders]')
            if (list === null) return
            const row = document.createElement('div')
            row.className = 'tt-split-new-row'
            row.innerHTML = '<b>新增订单</b><textarea rows="1" aria-label="新增子单项目内容" data-new-project></textarea><input inputmode="decimal" value="0.00" aria-label="新增子单接单价" data-new-price /><button type="button" class="layui-btn layui-btn-danger layui-btn-sm" data-remove-new>删除</button>'
            row.querySelector('[data-remove-new]')?.addEventListener('click', () => row.remove())
            list.appendChild(row)
          })
          panel.querySelector<HTMLElement>('[data-save-split]')?.addEventListener('click', async () => {
            const save = panel.querySelector<HTMLButtonElement>('[data-save-split]')
            if (save === null || save.disabled) return
            save.disabled = true
            try {
              const newOrders = Array.from(panel.querySelectorAll<HTMLElement>('.tt-split-new-row')).map((row) => ({
                project_text: row.querySelector<HTMLTextAreaElement>('[data-new-project]')?.value.trim() ?? '',
                total_price: row.querySelector<HTMLInputElement>('[data-new-price]')?.value.trim() ?? '',
              }))
              if (newOrders.some((item) => item.project_text === '' || item.total_price === '')) throw new Error('请填写拆出项目和接单价')
              const existingOrders = Array.from(panel.querySelectorAll<HTMLElement>('[data-existing-row]')).flatMap((row) => {
                const input = row.querySelector<HTMLTextAreaElement>('[data-existing-project]')
                const price = row.querySelector<HTMLInputElement>('[data-existing-price]')
                const projectText = input?.value ?? ''
                const totalPrice = price?.value.trim() ?? price?.dataset.originalPrice ?? ''
                const projectChanged = input !== null && input !== undefined && projectText !== input.dataset.original
                const priceChanged = price !== null && price !== undefined && totalPrice !== price.dataset.originalPrice
                return projectChanged || priceChanged
                  ? [{ order_no: row.dataset.order ?? '', ...(projectChanged ? { project_text: projectText } : {}), ...(priceChanged ? { total_price: totalPrice } : {}) }]
                  : []
              })
              const remainingProjectText = panel.querySelector<HTMLTextAreaElement>('[data-remaining-project]')?.value ?? ''
              const sourceContentChanged = source.split_id !== '' && remainingProjectText !== source.project_text
              const sourcePrice = panel.querySelector<HTMLInputElement>('[data-source-price]')?.value.trim() ?? source.total_price
              const sourcePriceChanged = source.order_no !== anchor.order_no && sourcePrice !== source.total_price
              if (newOrders.length === 0 && existingOrders.length === 0 && !sourceContentChanged && !sourcePriceChanged) {
                throw new Error('请修改发起订单内容、已有子单内容或新增拆出订单')
              }
              const payload = {
                remaining_project_text: remainingProjectText,
                new_orders: newOrders,
                ...(sourcePriceChanged ? { source_total_price: sourcePrice } : {}),
                ...(existingOrders.length > 0 ? { existing_orders: existingOrders } : {}),
              }
              const result = await splitPeerOrder(orderNo, {
                ...payload,
              })
              const createdCount = result.created.length
              ;(layer.close as (i: number) => void)(index)
              ;(layer.msg as (t: string) => void)(`${SPLIT_SUCCESS_MSG}（新建 ${createdCount} 笔）`)
              await loadAndRenderOrder()
            } catch (e) {
              errMsg(errText(e))
            } finally {
              save.disabled = false
            }
          })
        },
      })
    } catch (e) {
      errMsg(errText(e))
    }
  }
  root.addEventListener('click', onCopyClick)
  root.addEventListener('click', onSplitClick)

  /* ── 两块日志 ──────────────────────────────────────────────────────────
   *
   * **同源**：两块都用 `GET /api/business/orders/<单号>/audit/` 的那一份数据——
   * 一次取数喂两个块；「添加」成功后两块**一起**补新行（本地数据，不发第二次请求）。
   */
  /** 两块日志共用的行数据（**后端顺序 = 新在前**，本页**不 reverse**）。 */
  let auditEntries: AuditEntry[] = []
  const logDiv = root.querySelector<HTMLElement>('#logdiv')

  /** 操作日志表的本地数据出口（⛔ **不发请求**——数据已在 `auditEntries` 里）。 */
  function renderActionTable(): void {
    ;(table.reloadData as (id: string, o: unknown) => void)(ACTIONLOG_TABLE_ID, {
      data: auditEntries.map(toActionLogRow),
    })
  }

  /**
   * 「订单日志」页签的**唯一过滤出口**（过滤「查看游戏账号密码及详情」`VIEW_CREDENTIALS_ACTION`）。
   * ⚠️ 过滤**只影响渲染**、不动取数；`renderOrderLogs`（整块重绘）与 `submitNote`（就地 prepend）
   * 两个渲染出口的过滤规则**只能有这一处**——否则哪天规则变了，两个出口会各说各话。
   */
  function passesOrderLogFilter(entry: AuditEntry): boolean {
    return entry.action !== VIEW_CREDENTIALS_ACTION
  }

  /**
   * 「订单日志」整块重绘。
   * ⚠️ **只在这里过滤**（与 `submitNote` 共用同一个出口，见 `passesOrderLogFilter`）。
   */
  function renderOrderLogs(): void {
    if (logDiv === null) return
    logDiv.replaceChildren(...auditEntries.filter(passesOrderLogFilter).map(buildLogLine))
  }

  /** 取一次 `/audit/` 并**整块重绘**两块（首屏与 2 分钟定时器共用这一条路）。 */
  async function loadAudit(): Promise<void> {
    try {
      auditEntries = await fetchOrderAudit(orderNo)
    } catch (e) {
      // 与详情取数同口径：原样弹后端 msg（⛔ 不自造文案）。
      errMsg(errText(e))
      return
    }
    renderOrderLogs()
    renderActionTable()
  }

  /**
   * 「添加」成功后就地补一行：两块**同源** ⇒ 操作日志表的**本地数据**也同时补上
   * （`table.reloadData(id, {data})`，**不再发请求**），否则同一屏两个页签自相矛盾。
   */
  async function submitNote(content: string): Promise<void> {
    try {
      const row = await createOrderNote(orderNo, content)
      auditEntries = [row, ...auditEntries]
      // prepend 也走**同一个过滤出口**（`passesOrderLogFilter`），与 `renderOrderLogs` 形状一致。
      if (passesOrderLogFilter(row)) logDiv?.prepend(buildLogLine(row))
      renderActionTable()
      const input = root.querySelector<HTMLInputElement>('#remark')
      if (input) input.value = ''
    } catch (e) {
      errMsg(errText(e))
    }
  }

  // 操作日志表：**先 `table.render` 一次**（不先 render 就 `reloadData` 只会打一行 warn 然后什么都不做——E-0035）。
  // ⛔ **不带 `url`**：只要 config 里还有 `url`，`reloadData(id, {data})` 就走 ajax 分支、忽略我们传的 `data`；
  // 数据一律由页面自己取，统一信封 / CSRF / 错误映射仍只在 `@/api/` 一处。
  ;(table.render as (o: unknown) => void)({
    elem: ACTIONLOG_TABLE_ELEM,
    id: ACTIONLOG_TABLE_ID,
    data: [],
    height: 320,
    defaultToolbar: [],
    page: false,
    cols: [buildActionLogCols()],
  })

  // layui 的 `form` 模块对 `*[lay-submit]` 做了**文档级委托**：注册 `submit(addpbtn)` 就够，不需要再绑 click。
  ;(form.on as (e: string, cb: (data: { field?: Record<string, string> }) => unknown) => void)(
    'submit(addpbtn)',
    (data) => {
      const content = data.field?.remark ?? ''
      // ⚠️ 判据比对标**严一档**：纯空白也拦下来（零请求；后端对纯空白同样拦 ⇒ 只省一次往返、不改语义）。
      if (content.trim() === '') {
        // 文案与停留时长逐字照对标：`layer.msg('备注内容不能为空！', { time: 1000 })`。
        ;(layer.msg as (t: string, o?: unknown) => void)('备注内容不能为空！', { time: 1000 })
        return false
      }
      // `submitNote` 自己 catch（失败原样弹后端 msg）⇒ 这里 fire-and-forget，不 await。
      void submitNote(content)
      return false
    },
  )

  // ⚠️ 卸载函数里必须 `clearInterval`：页面挂在 iframe 里，不清理就泄漏、并让已卸载页面继续发请求。
  const refreshTimer = setInterval(() => {
    void loadAudit()
  }, LOG_REFRESH_MS)

  // 两块日志与订单详情**各取各的**；先发出去、在挂载返回前收口，调用方拿到的一定是渲染完的 DOM。
  let order: Order | null = null
  /**
   * 取这一单并把它渲染到左列（挂载时跑一次；「继续拆分订单」成功后也跑一次——
   * 拆分改掉了当前单的 `project_text`，不重取就会在页面上说假话）。
   */
  async function loadAndRenderOrder(): Promise<Order | null> {
    try {
      order = await fetchOrder(orderNo)
    } catch (e) {
      // 未知单号与形状不对都**原样**弹出来（不换文案、不吞掉）。
      errMsg(errText(e))
      return null
    }
    if (order === null) return null
    updateSplitButton(order)
    if (platformTab !== null) platformTab.style.display = order.has_dailiantong_history === true ? '' : 'none'
    document.title = `${displayOrderNo(order)}${TITLE_SUFFIX}`
    // 字典只影响 FK 的**显示名字** ⇒ 拉失败回落空字典（显示原始 id），不挡订单本身。
    let dicts = emptyNameDicts()
    try {
      dicts = await loadNameDicts(order)
    } catch {
      dicts = emptyNameDicts()
    }
    fillStandardRows(root, order, dicts)
    renderPenaltyOrderLinks(root, order)
    // `copybtn` 的 `data-id` 装系统单号（复制动作归 onCopyClick；`data-clipboard-text` 保持空串）。
    const copyBtn = root.querySelector<HTMLElement>('#copybtn')
    if (copyBtn) copyBtn.dataset.id = order.order_no
    // ⛔ 这里**不追加**「我方字段」卡片：业务方裁定「完全照对标」、运行期追加已删
    // （`data-extra-card` 全页为 0，由 `order-orderview.spec.ts` 钉住）。
    return order
  }

  /* ── 代练通页签：取数 + 动作按钮 ───────────────────────────────────────
   *
   * 一个状态灯控三件事：`platformLoading`（在途去重：连点页签只发一次）、
   * `platformActionBusy`（动作在途 ⇒ 按钮禁用 + 再点不处理）、以及页面自己那份 DOM。
   */
  let platformLoading = false
  let platformActionBusy = false
  /**
   * 面板数据的状态（四态，见 `PlatformPanelState`）：四颗动作按钮在 `ready` 以外**都不开层**，
   * 且三种「不能点」的原因各给各的下一步（loading / failed / none）。
   */
  let platformState: PlatformPanelState = 'idle'
  /** 两块读面各自的在途灯（互不拖住；重试只重取该重的那块）。 */
  let platformMessagesLoading = false
  let platformProgressLoading = false
  /**
   * 两块读面最近一次取数的**失败原因**（中文；成功时清空）。
   *
   * 用途只有一个：推送行回来是「本单还没发到平台」（`none`）时，把读面那句
   * 「不能做平台读取」换成**空态**——那种单确实没有留言 / 进度图，不是读失败。
   */
  let platformMessagesError = ''
  let platformProgressError = ''
  /**
   * 留言响应里 `images`（本单图片）最近一次的失败原因。
   *
   * ⚠️ 与 `platformMessagesError` 分开：图片读失败**不拖累留言**（后端 `images_error`），
   * 页面要把「留言读到了、图片没读到」如实画出来。
   */
  let platformMsgImagesError = ''
  /**
   * 整块面板（推送行 + 两块读面）的在途闸：**连点页签只跑一次**。
   *
   * ⚠️ 不能只用 `platformLoading`：它只管推送行那一段，推送到手之后还有两块读面要取，
   * 第二次点击会在那个窗口里再起一轮 ⇒ 留言 / 进度图各发两发。
   */
  let platformPanelLoading = false
  /**
   * 最近一次取回来的**全部**推送行（不是只有拿来做展示的那一条）。
   *
   * 唯一消费者是「全平台改价」：它要按**平台**去重后再逐个发（撤回后重抛会留下多条同平台行，
   * 只有最新一条有效 ⇒ 不去重就会对同一平台发两发）。⛔ 不留旧值：取数失败时同步清空。
   */
  let platformRows: DailiantongPushRow[] = []
  /**
   * 截图上传的**文件选择器**：页面运行期补的隐藏 `<input type="file">`（点拖拽区 / 选文件都落到它）。
   * ⛔ 不进证据件标记（那会改动对标的标记）。
   */
  const platformPanel = root.querySelector<HTMLElement>(
    `.layui-tab[lay-filter="${PLATFORM_SUB_TAB_FILTER}"]`,
  )
  let screenshotInput: HTMLInputElement | null = null
  if (platformPanel !== null) {
    screenshotInput = document.createElement('input')
    screenshotInput.type = 'file'
    screenshotInput.accept = 'image/jpeg,image/png'
    screenshotInput.className = PLATFORM_UPLOAD_INPUT_CLASS
    screenshotInput.style.display = 'none'
    platformPanel.appendChild(screenshotInput)
  }
  /** 已有确认 / 输入层开着：连点时不再叠第二层（否则第二个弹层确认后还会再发一次）。 */
  let platformDialogOpen = false
  /** 弹层被确定 / 取消 / 右上角关掉时，把「开着」的闸放回去。 */
  function closePlatformDialog(): void {
    platformDialogOpen = false
  }

  /**
   * 取这一单的**全部**推送行并重画面板（切页签 / 每个动作成功后都走这一条路）。
   * 有行 ⇒ 填格收起状态区；没有 ⇒ 清空 + 空态；失败 ⇒ **先清空**（留旧值就是假话）
   * + 面板里可见的错误 + 弹后端原文。
   * ⚠️ 在途判断放在最前面：连点页签 / 动作成功后的自动重取都只会有一次在跑。
   */
  async function loadPlatformPushes(): Promise<void> {
    if (platformLoading) return
    platformLoading = true
    platformState = 'loading'
    try {
      const pushes = await fetchOrderPlatformPushes(orderNo)
      platformRows = pushes
      const push = pushes[0] ?? null
      platformState = push === null ? 'none' : 'ready'
      fillPlatformRows(root, push)
      if (push === null) setPlatformStatus(root, 'empty', PLATFORM_EMPTY_TEXT)
      else setPlatformStatus(root, 'hidden')
    } catch (e) {
      const message = errText(e)
      platformState = 'failed'
      platformRows = []
      fillPlatformRows(root, null)
      setPlatformStatus(root, 'error', `${PLATFORM_LOAD_ERROR_PREFIX}${message}`)
      errMsg(message)
    } finally {
      platformLoading = false
    }
  }

  /**
   * 取平台留言历史并渲染（三态：加载中 / 空 / 失败）。
   *
   * ⚠️ 后端读面**失败不 500**：HTTP 200 + `{messages: [], error: 中文}` ⇒ `error` 非空
   * **也是失败**（⛔ 不许当成「暂无留言」，那就是静默空）。失败态给中文原因 + 「重试」。
   */
  async function loadPlatformMessages(): Promise<void> {
    if (platformMessagesLoading) return
    platformMessagesLoading = true
    platformMessagesError = ''
    platformMsgImagesError = ''
    setPlatformMessageNotice(root, PLATFORM_MSG_LOADING_TEXT)
    setPlatformMessageImagesNotice(root, PLATFORM_MSG_IMAGE_LOADING_TEXT)
    try {
      const result = await fetchOrderPlatformPushMessages(orderNo)
      const error = (result.error ?? '').trim()
      if (error !== '') {
        platformMessagesError = error
        platformMsgImagesError = error
        setPlatformMessageNotice(
          root,
          `${PLATFORM_MSG_LOAD_ERROR_PREFIX}${error}`,
          { error: true, retry: true },
        )
        setPlatformMessageImagesNotice(
          root,
          `${PLATFORM_MSG_IMAGE_LOAD_ERROR_PREFIX}${error}`,
          { error: true, retry: true },
        )
        return
      }
      const messages = result.messages ?? []
      if (messages.length === 0) {
        setPlatformMessageNotice(root, PLATFORM_MSG_EMPTY_TEXT)
      } else {
        renderPlatformMessages(root, messages)
      }
      // ⭐ 图片与留言同一响应（后端从进度列表取 `images`）；图片读失败**不拖累留言**。
      const imagesError = (result.images_error ?? '').trim()
      const images = result.images ?? []
      if (imagesError !== '') {
        platformMsgImagesError = imagesError
        setPlatformMessageImagesNotice(
          root,
          `${PLATFORM_MSG_IMAGE_LOAD_ERROR_PREFIX}${imagesError}`,
          { error: true, retry: true },
        )
      } else if (images.length === 0) {
        setPlatformMessageImagesNotice(root, PLATFORM_MSG_IMAGE_EMPTY_TEXT)
      } else {
        renderPlatformMessageImages(root, images)
      }
    } catch (e) {
      const message = errText(e)
      platformMessagesError = message
      platformMsgImagesError = message
      setPlatformMessageNotice(
        root,
        `${PLATFORM_MSG_LOAD_ERROR_PREFIX}${message}`,
        { error: true, retry: true },
      )
      setPlatformMessageImagesNotice(
        root,
        `${PLATFORM_MSG_IMAGE_LOAD_ERROR_PREFIX}${message}`,
        { error: true, retry: true },
      )
    } finally {
      platformMessagesLoading = false
    }
  }

  /** 取平台进度图并渲染（三态与失败语义同留言：`error` 非空也是失败）。 */
  async function loadPlatformProgress(): Promise<void> {
    if (platformProgressLoading) return
    platformProgressLoading = true
    platformProgressError = ''
    setPlatformProgressNotice(root, PLATFORM_PROGRESS_LOADING_TEXT)
    try {
      const result = await fetchOrderPlatformPushProgress(orderNo)
      const error = (result.error ?? '').trim()
      if (error !== '') {
        platformProgressError = error
        setPlatformProgressNotice(
          root,
          `${PLATFORM_PROGRESS_LOAD_ERROR_PREFIX}${error}`,
          { error: true, retry: true },
        )
        return
      }
      const items = result.progress ?? []
      if (items.length === 0) {
        setPlatformProgressNotice(root, PLATFORM_PROGRESS_EMPTY_TEXT)
        return
      }
      renderPlatformProgress(root, items)
    } catch (e) {
      const message = errText(e)
      platformProgressError = message
      setPlatformProgressNotice(
        root,
        `${PLATFORM_PROGRESS_LOAD_ERROR_PREFIX}${message}`,
        { error: true, retry: true },
      )
    } finally {
      platformProgressLoading = false
    }
  }

  /**
   * 切到「代练通」页签时的**唯一**入口：三发**并发**（推送行 + 留言 + 进度图），再按推送行
   * 的真实结果收口。`none` 时把读面的失败提示换成**空态**——没有平台单是**预期内的前提**、
   * 不是读失败（⛔ 不把前提说成错误）。⚠️ 整段在途闸在最外层：连点页签不会把三发变成六发。
   */
  async function loadPlatformPanel(): Promise<void> {
    if (platformPanelLoading) return
    platformPanelLoading = true
    // 一进来就摆「加载中」：三发可能都不快，让用户先看到在读。
    setPlatformMessageNotice(root, PLATFORM_MSG_LOADING_TEXT)
    setPlatformMessageImagesNotice(root, PLATFORM_MSG_IMAGE_LOADING_TEXT)
    setPlatformProgressNotice(root, PLATFORM_PROGRESS_LOADING_TEXT)
    try {
      await Promise.all([
        loadPlatformPushes(),
        loadPlatformMessages(),
        loadPlatformProgress(),
      ])
      if (platformState === 'none') {
        if (platformMessagesError !== '') {
          setPlatformMessageNotice(root, PLATFORM_MSG_EMPTY_TEXT)
        }
        if (platformMsgImagesError !== '') {
          setPlatformMessageImagesNotice(root, PLATFORM_MSG_IMAGE_EMPTY_TEXT)
        }
        if (platformProgressError !== '') {
          setPlatformProgressNotice(root, PLATFORM_PROGRESS_EMPTY_TEXT)
        }
        return
      }
      // `ready`：读面已按各自结果画好；`failed`：读面若碰巧成功，保留真实结果，⛔ 不硬改成错误。
      // `loading` / `idle`：推送行还在另一个入口取 ⇒ 保持「加载中」。
    } finally {
      platformPanelLoading = false
    }
  }

  /** 提交中的按钮样子（layui 的禁用类 + `aria-disabled`；点击本身由 `platformActionBusy` 挡住）。 */
  function setPlatformBusy(button: HTMLElement, busy: boolean): void {
    if (busy) {
      button.classList.add(PLATFORM_BUSY_CLASS)
      button.setAttribute('aria-disabled', 'true')
      return
    }
    // 后端说「不能点」的标记还在 ⇒ 收尾时把禁用类补回去（别把持久禁用当成一次性的提交中）。
    if (button.getAttribute(PLATFORM_BLOCKED_ATTR) === '1') {
      button.classList.add(PLATFORM_BUSY_CLASS)
      button.setAttribute('aria-disabled', 'true')
      return
    }
    button.classList.remove(PLATFORM_BUSY_CLASS)
    button.removeAttribute('aria-disabled')
  }

  /**
   * 动作按钮的**唯一**出口：在途闸 → 禁用按钮 → 发请求 → 提示 → **重新取数**。
   * ⚠️ 成功提示是页面复述（`request<T>()` 读不到信封 `msg`）；**撤回例外**：话术按**返回行**算
   * （`withdrawResultMessage()`）。失败按 `classifyPushError` 分两档（⛔ 不许把「不知道」说成「失败」）：
   * `failed` ⇒ 原样弹后端中文；`unknown` ⇒「已发出，请刷新核对」。
   * ⚠️ **两种失败默认都强制重拉面板**：`failed` 里混着「已提交但没核验到」（同一个 1002 码、
   * 前端没有可判别字段，见文件头第 40 条）——不重拉用户就对着一屏旧值判断。⛔ 不做中文文案匹配。
   * ⚠️ 成功后**必须重取**：撤回会把行删掉、改价 / 补时会改值——不重取就是把旧值画在屏幕上。
   */
  async function runPlatformAction(
    button: HTMLElement,
    send: () => Promise<DailiantongPushRow>,
    successText: string | ((row: DailiantongPushRow) => string),
    options: {
      refreshOnFailure?: boolean
      refreshOrder?: boolean
      /**
       * 失败提示的可读化（只给需要翻译的那一个动作传）。
       * 默认原样显示后端中文；传了就把「参数错误」这类上游原话翻成人话。
       */
      mapFailure?: (message: string) => string
    } = {},
  ): Promise<void> {
    if (platformActionBusy) return
    platformActionBusy = true
    setPlatformBusy(button, true)
    try {
      const row = await send()
      const text = typeof successText === 'function' ? successText(row) : successText
      if (options.refreshOrder) {
        const [refreshedOrder] = await Promise.all([loadAndRenderOrder(), loadPlatformPushes()])
        if (refreshedOrder?.status !== 'COMPLETED') {
          errMsg(`代练通已结算，${row.last_error || '本地订单状态暂未确认，请稍后刷新查看。'}`)
          return
        }
        ;(layer.msg as (t: string) => void)(text)
      } else {
        ;(layer.msg as (t: string) => void)(text)
        await loadPlatformPushes()
      }
    } catch (e) {
      if (classifyPushError(e) === 'failed') {
        const message = errText(e)
        errMsg(options.mapFailure === undefined ? message : options.mapFailure(message))
      } else {
        errMsg(PLATFORM_ACTION_UNKNOWN_MSG)
      }
      // ⚠️ 两种失败默认都重拉：`failed` 也可能是「已提交但没核验到」，屏幕必须回到服务端真实状态。
      // 撤销区例外：业务方口径「失败原样显示后端中文、不刷新」，保持用户当前所见便于对照。
      if (options.refreshOnFailure !== false) await loadPlatformPushes()
    } finally {
      platformActionBusy = false
      setPlatformBusy(button, false)
    }
  }

  /**
   * 发送一条平台留言。
   * 与 `runPlatformAction` 的区别只有**成功之后做什么**：留言不改推送行 ⇒ 成功要
   * **清空输入框 + 重取留言列表**（⛔ 不是重取推送行）；在途闸 / 禁用 / 失败分档沿用同一套，
   * ⛔ 不要在这里另写第二套状态机。
   */
  async function sendPlatformMessage(button: HTMLElement, msg: string): Promise<void> {
    if (platformActionBusy) return
    platformActionBusy = true
    setPlatformBusy(button, true)
    try {
      await sendOrderPlatformPushMessage(orderNo, msg)
      ;(layer.msg as (t: string) => void)(MESSAGE_SUCCESS_MSG)
      const input = root.querySelector<HTMLInputElement>(PLATFORM_MSG_INPUT_SELECTOR)
      if (input !== null) input.value = ''
      await loadPlatformMessages()
    } catch (e) {
      // 失败**不清空**输入框：用户改一改就能重发（清掉等于让他重敲一遍）。
      if (classifyPushError(e) === 'failed') {
        errMsg(errText(e))
      } else {
        errMsg(PLATFORM_ACTION_UNKNOWN_MSG)
      }
    } finally {
      platformActionBusy = false
      setPlatformBusy(button, false)
    }
  }

  /**
   * 非 `ready` 态点动作时该说的下一步（与各态一一对应）；`ready` ⇒ `null`（可以继续）。
   */
  function platformNotReadyMessage(): string | null {
    if (platformState === 'ready') return null
    if (platformState === 'none') return PLATFORM_NEED_PUSH_MSG
    if (platformState === 'failed') return PLATFORM_FAILED_MSG
    return PLATFORM_LOADING_MSG
  }

  /**
   * 点进度图缩略图 ⇒ 开大图（`layer.open` 的 `type:1`）。
   * 只对**有 `url`** 的缩略图生效（占位块没有可开的图）；标题用 `caption`，进层前 `escapeHtml`。
   */
  function openPlatformProgressPreview(url: string, caption: string): void {
    const box = document.createElement('div')
    box.className = 'dltProgressPreview'
    box.setAttribute('style', 'padding:10px;text-align:center')
    const image = document.createElement('img')
    image.src = url
    image.alt = caption === '' ? PLATFORM_PROGRESS_PREVIEW_TITLE : caption
    image.setAttribute('style', 'max-width:90vw;max-height:80vh')
    box.appendChild(image)
    ;(layer.open as (options: unknown) => void)({
      type: 1,
      title: escapeHtml(caption === '' ? PLATFORM_PROGRESS_PREVIEW_TITLE : caption),
      // ⚠️ 必须是 jQuery 形状 + end 清理（裸节点会让 layui 的 layer 抛错，见 `layerNode`）。
      content: layerNode(box),
      end: () => box.remove(),
      area: ['auto', 'auto'],
      shadeClose: true,
    })
  }

  /**
   * 从**所有平台**撤回：返回值是**逐平台结果** ⇒ 成功后先弹整单摘要、再弹逐平台明细
   * （部分成功如实报）；然后重取**整块面板**（推送行可能全部消失 / 变成协商撤销态）。
   */
  async function runWithdrawAll(button: HTMLElement): Promise<void> {
    if (platformActionBusy) return
    platformActionBusy = true
    setPlatformBusy(button, true)
    try {
      const result = await withdrawAllOrderPlatformPushes(orderNo)
      ;(layer.msg as (t: string) => void)(withdrawAllSummary(result))
      ;(
        layer.alert as (content: string, options?: unknown) => void
      )(withdrawAllDetailHtml(result), { title: WITHDRAW_ALL_RESULT_TITLE })
      await loadPlatformPanel()
    } catch (e) {
      if (classifyPushError(e) === 'failed') {
        errMsg(errText(e))
      } else {
        errMsg(PLATFORM_ACTION_UNKNOWN_MSG)
      }
      // 失败也重拉：`failed` 可能混着「已提交但没核验到」，屏幕必须回到服务端真实状态。
      await loadPlatformPushes()
    } finally {
      platformActionBusy = false
      setPlatformBusy(button, false)
    }
  }

  /**
   * 编辑平台订单（两颗按钮共用同一张表单，只有端点与标题不同：`edit/` 与 `modify/`）。
   * 提交时只送**填了内容**的字段（见 `readPlatformEditPayload`），一项都没填 ⇒ 页内提示 + 零请求；
   * 提交中禁用 / 成功重取 / 失败原样显示都走 `runPlatformAction`。
   */
  function openPlatformEditDialog(
    scope: 'current' | 'all',
    button: HTMLElement,
  ): void {
    if (platformDialogOpen) return
    platformDialogOpen = true
    const formEl = buildPlatformEditForm()
    ;(layer.open as (options: unknown) => void)({
      type: 1,
      title: scope === 'all' ? EDIT_ALL_TITLE : EDIT_CURRENT_TITLE,
      // ⚠️ jQuery 形状 + end 清理（裸节点会让 layer 抛错；见 `layerNode`）。
      content: layerNode(formEl),
      end: () => {
        formEl.remove()
        closePlatformDialog()
      },
      area: ['620px', 'auto'],
      shadeClose: false,
      btn: [EDIT_SUBMIT_TEXT, '取消'],
      yes: (index: number) => {
        const payload = readPlatformEditPayload(formEl)
        if (Object.keys(payload).length === 0) {
          // 空载荷**不关层**：用户可能只是还没填完；先关层等于把已输入的内容丢掉。
          errMsg(EDIT_EMPTY_MSG)
          return
        }
        closePlatformDialog()
        ;(layer.close as (i: number) => void)(index)
        void runPlatformAction(
          button,
          () =>
            scope === 'all'
              ? modifyOrderPlatformPush(orderNo, payload)
              : editOrderPlatformPush(orderNo, payload),
          EDIT_SUCCESS_MSG,
        )
      },
      btn2: (index: number) => {
        closePlatformDialog()
        ;(layer.close as (i: number) => void)(index)
      },
      cancel: closePlatformDialog,
    })
  }

  /**
   * 上传一张进度截图（multipart）。
   * 顺序 = 页内先挡一轮（四态闸 / 大小 / 说明长度 / **字节魔数**）⇒ 提交中禁用上传区
   * ⇒ 成功提示 + 重取**进度图**（⛔ 不是整块面板：截图不改推送行）。失败分档同其它写动作。
   */
  async function uploadPlatformScreenshot(file: File): Promise<void> {
    if (platformActionBusy) return
    const blocked = platformNotReadyMessage()
    if (blocked !== null) {
      notOpenMsg(blocked)
      return
    }
    if (file.size > PLATFORM_SCREENSHOT_MAX_BYTES) {
      errMsg(PLATFORM_SCREENSHOT_TOO_LARGE_MSG)
      return
    }
    const captionEl = root.querySelector<HTMLInputElement>(
      PLATFORM_SCREENSHOT_CAPTION_SELECTOR,
    )
    const caption = (captionEl?.value ?? '').trim()
    if (caption.length > PLATFORM_SCREENSHOT_CAPTION_MAX_LENGTH) {
      errMsg(PLATFORM_SCREENSHOT_CAPTION_TOO_LONG_MSG)
      return
    }
    const kind = await sniffPlatformImageKind(file)
    if (kind === null) {
      errMsg(PLATFORM_SCREENSHOT_TYPE_MSG)
      return
    }
    // 嗅探是异步的 ⇒ 回来后再看一眼在途闸（两秒内连拖两张不要叠两发）。
    if (platformActionBusy) return
    platformActionBusy = true
    const drag = platformPanel?.querySelector<HTMLElement>(PLATFORM_UPLOAD_DRAG_SELECTOR) ?? null
    if (drag !== null) setPlatformBusy(drag, true)
    try {
      await uploadOrderPlatformPushScreenshot(
        orderNo,
        file,
        caption === '' ? undefined : caption,
      )
      ;(layer.msg as (t: string) => void)(PLATFORM_SCREENSHOT_SUCCESS_MSG)
      if (captionEl !== null) captionEl.value = ''
      await loadPlatformProgress()
    } catch (e) {
      if (classifyPushError(e) === 'failed') {
        errMsg(errText(e))
      } else {
        errMsg(PLATFORM_ACTION_UNKNOWN_MSG)
      }
    } finally {
      platformActionBusy = false
      if (drag !== null) setPlatformBusy(drag, false)
    }
  }

  /** 隐藏 `<input type="file">` 的 change（选完文件立刻把 input 清空，同一个文件能再选一次）。 */
  function onScreenshotInputChange(): void {
    if (screenshotInput === null) return
    const file = screenshotInput.files?.[0]
    screenshotInput.value = ''
    if (file !== undefined) void uploadPlatformScreenshot(file)
  }

  /** 拖拽经过上传区时允许落下来（不 preventDefault 的话浏览器会打开这张图）。 */
  function onPlatformDragOver(event: Event): void {
    const target = event.target as HTMLElement | null
    if (target === null) return
    if (target.closest(`${PLATFORM_UPLOAD_DRAG_SELECTOR}, #uploadInput`) === null) return
    event.preventDefault()
  }

  /** 拖拽落下 ⇒ 取第一个文件走同一条上传路（与「选择文件」共用校验与出口）。 */
  function onPlatformDrop(event: Event): void {
    const target = event.target as HTMLElement | null
    if (target === null) return
    if (target.closest(`${PLATFORM_UPLOAD_DRAG_SELECTOR}, #uploadInput`) === null) return
    event.preventDefault()
    const file = (event as DragEvent).dataTransfer?.files?.[0]
    if (file !== undefined) void uploadPlatformScreenshot(file)
  }

  /**
   * 锁定 / 解锁的**原因输入层**（后端 `reason` 可选、上限 128 字）。
   * 确定前页内先挡超长；空 / 纯空白 ⇒ **不送 `reason` 键**；提交中禁用、成功重取、
   * 失败原样弹后端中文（`runPlatformAction` 负责）。
   */
  function openLockReasonPrompt(locked: 0 | 1, button: HTMLElement): void {
    platformDialogOpen = true
    ;(
      layer.prompt as (options: unknown, yes: (value: string, index: number) => void) => void
    )(
      {
        formType: 0,
        value: '',
        title: LOCK_REASON_PROMPT_TITLE[locked],
        btn: ['确定', '取消'],
        btn2: closePlatformDialog,
        cancel: closePlatformDialog,
        end: closePlatformDialog,
      },
      (value: string, index: number) => {
        closePlatformDialog()
        ;(layer.close as (i: number) => void)(index)
        const reason = (value ?? '').trim()
        if (reason.length > LOCK_REASON_MAX_LENGTH) {
          errMsg(LOCK_REASON_TOO_LONG_MSG)
          return
        }
        void runPlatformAction(
          button,
          () =>
            lockOrderPlatformPush(orderNo, locked, reason === '' ? undefined : reason),
          LOCK_SUCCESS_MSG[locked],
        )
      },
    )
  }

  /**
   * 全平台改价：按**平台**去重后逐个 `POST price/`（本仓没有「一次改所有平台」的批量端点）。
   * ⚠️ 今天读面只可能有代练通一档 ⇒ 实际只发一发；⛔ 不假装已经能跨平台（多平台是后端契约缺口）。
   */
  async function runPlatformBulkPrice(button: HTMLElement, price: string): Promise<void> {
    if (platformActionBusy) return
    platformActionBusy = true
    setPlatformBusy(button, true)
    try {
      const platforms: number[] = []
      for (const row of platformRows) {
        if (!platforms.includes(row.platform)) platforms.push(row.platform)
      }
      for (let i = 0; i < platforms.length; i += 1) {
        await changeOrderPlatformPushPrice(orderNo, price)
      }
      ;(layer.msg as (t: string) => void)(REPRICE_ALL_SUCCESS_MSG)
      await loadPlatformPushes()
    } catch (e) {
      if (classifyPushError(e) === 'failed') {
        errMsg(errText(e))
      } else {
        errMsg(PLATFORM_ACTION_UNKNOWN_MSG)
      }
      await loadPlatformPushes()
    } finally {
      platformActionBusy = false
      setPlatformBusy(button, false)
    }
  }

  /**
   * 点「代练通」页签 ⇒ 取一次数据（layui 的类名切换由 `initIslandElements` 负责，这里只管取数）。
   * `loadPlatformPanel` 自带整段在途闸 ⇒ 连点页签三发请求也只会各发一次。
   */
  const onPlatformTabClick = (event: Event): void => {
    const target = event.target as HTMLElement | null
    if (target === null || target.closest(PLATFORM_TAB_ITEM_SELECTOR) === null) return
    void loadPlatformPanel()
  }

  /**
   * 代练通面板里**已接真**的按钮的分发（空态「去发单」、读面「重试」、上传区、缩略图、各动作）。
   * ⚠️ 与 `bindPlatformNotOpen`（捕获阶段、只提示「尚未开放」）的分工：那颗委托按
   * `PLATFORM_WIRED_SELECTOR` 放过这几颗，事件才落到这里。
   */
  const onPlatformActionClick = (event: Event): void => {
    const target = event.target as HTMLElement

    // 空态「去发单」：打开我们自己的发单页（同一颗地球按钮的打开方式），本页零 API。
    const pushButton = target.closest<HTMLElement>(DLT_PUSH_BTN_SELECTOR)
    if (pushButton !== null) {
      event.preventDefault()
      openPushJdpPage(layer, orderNo, order === null ? '' : displayOrderNo(order))
      return
    }

    // 「重试」重取**整块面板**（推送行 + 两块读面）——读不到留言也可能是连推送行都没读到。
    // ⚠️ 必须排在下面 `platformState !== 'ready'` 闸**之前**：重试按钮正是「不在 ready 态」时才出现。
    const retryButton = target.closest<HTMLElement>(DLT_RETRY_BTN_SELECTOR)
    if (retryButton !== null) {
      event.preventDefault()
      if (platformActionBusy || platformDialogOpen) return
      void loadPlatformPanel()
      return
    }

    // 截图上传区（拖拽框）点了 ⇒ 打开文件选择器。
    // ⚠️ 必须在下面 `PLATFORM_ACTION_SELECTOR` 闸之前：上传区不是动作按钮。
    const uploadDrag = target.closest<HTMLElement>(PLATFORM_UPLOAD_DRAG_SELECTOR)
    if (uploadDrag !== null) {
      event.preventDefault()
      if (platformActionBusy) return
      const blocked = platformNotReadyMessage()
      if (blocked !== null) {
        notOpenMsg(blocked)
        return
      }
      screenshotInput?.click()
      return
    }

    // ⭐ g3：点进度图缩略图 ⇒ 开大图（只有 `url` 非空的缩略图挂了这个 class）。
    const progressThumb = target.closest<HTMLElement>(PLATFORM_PROGRESS_IMAGE_SELECTOR)
    if (progressThumb !== null) {
      const url = (progressThumb.getAttribute('src') ?? '').trim()
      if (url === '') return
      event.preventDefault()
      openPlatformProgressPreview(url, (progressThumb.getAttribute('alt') ?? '').trim())
      return
    }

    // 已接真的动作按钮：先过「有没有推送行」这道闸。没有 ⇒ 提示先发单、**零请求**
    // （⛔ 不要让用户进弹层、填完再被后端拒一次）。
    if (target.closest(PLATFORM_ACTION_SELECTOR) === null) return
    event.preventDefault()
    // 只有 `ready`（确实有推送行）才开层；其余三态各说各的下一步。
    const blocked = platformNotReadyMessage()
    if (blocked !== null) {
      notOpenMsg(blocked)
      return
    }
    // 在途 / 已有弹层：⛔ 不再开第二层（补时是增量，第二个弹层确认后会重复加时）。
    if (platformActionBusy || platformDialogOpen) return

    // ⭐ g3：确认完单（只在 `remote_status=13` 露出来；后端还会再判一次状态 + 扣余额 + 回读 17）。
    const finishButton = target.closest<HTMLElement>(PLATFORM_COMPLETE_SELECTOR)
    if (finishButton !== null) {
      platformDialogOpen = true
      ;(
        layer.confirm as (
          content: string,
          options: unknown,
          yes: (index: number) => void,
          cancel: () => void,
        ) => void
      )(
        FINISH_CONFIRM_TEXT,
        { title: FINISH_CONFIRM_TITLE, btn: ['确定', '取消'], cancel: closePlatformDialog, end: closePlatformDialog },
        (index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          void runPlatformAction(
            finishButton,
            () => finishOrderPlatformPush(orderNo),
            FINISH_SUCCESS_MSG,
            { refreshOrder: true },
          )
        },
        closePlatformDialog,
      )
      return
    }

    // ⭐ g3：从所有平台撤回（确认文案逐字照对标；成功后再逐平台回结果）。
    const withdrawAllButton = target.closest<HTMLElement>(
      PLATFORM_ACTION_SELECTORS.withdrawAll,
    )
    if (withdrawAllButton !== null) {
      platformDialogOpen = true
      ;(
        layer.confirm as (
          content: string,
          options: unknown,
          yes: (index: number) => void,
          cancel: () => void,
        ) => void
      )(
        WITHDRAW_ALL_CONFIRM_TEXT,
        {
          title: WITHDRAW_ALL_CONFIRM_TITLE,
          btn: ['确定', '取消'],
          cancel: closePlatformDialog,
          end: closePlatformDialog,
        },
        (index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          void runWithdrawAll(withdrawAllButton)
        },
        closePlatformDialog,
      )
      return
    }

    // ⭐ g3：编辑当前平台订单 / 全平台编辑订单（同一张表单，端点分别是 edit/ 与 modify/）。
    const editButton = target.closest<HTMLElement>(PLATFORM_ACTION_SELECTORS.edit)
    if (editButton !== null) {
      openPlatformEditDialog('current', editButton)
      return
    }
    const editAllButton = target.closest<HTMLElement>(PLATFORM_ACTION_SELECTORS.editAll)
    if (editAllButton !== null) {
      openPlatformEditDialog('all', editAllButton)
      return
    }

    const withdrawButton = target.closest<HTMLElement>(PLATFORM_ACTION_SELECTORS.withdraw)
    if (withdrawButton !== null) {
      // 两档语义必须在确认框里说清楚：未接手 = 删除并退款；已接手 = 协商撤销（可能被驳回）。
      platformDialogOpen = true
      ;(
        layer.confirm as (
          content: string,
          options: unknown,
          yes: (index: number) => void,
          cancel: () => void,
        ) => void
      )(
        WITHDRAW_CONFIRM_TEXT,
        { title: '从平台撤回', btn: ['确定', '取消'], cancel: closePlatformDialog, end: closePlatformDialog },
        (index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          void runPlatformAction(
            withdrawButton,
            () => withdrawOrderPlatformPush(orderNo),
            // ⛔ 不复述后端那句固定的「撤回已完成」：按返回行说真实结果。
            withdrawResultMessage,
            // 与撤销区的「申请撤销」同一个端点 ⇒ 同一个「参数错误」的人话口径。
            { mapFailure: withdrawFailureMessage },
          )
        },
        closePlatformDialog,
      )
      return
    }

    const priceButton = target.closest<HTMLElement>(PLATFORM_ACTION_SELECTORS.price)
    if (priceButton !== null) {
      platformDialogOpen = true
      ;(
        layer.prompt as (options: unknown, yes: (value: string, index: number) => void) => void
      )(
        {
          formType: 0,
          value: '',
          title: PRICE_PROMPT_TITLE,
          btn: ['确定', '取消'],
          btn2: closePlatformDialog,
          cancel: closePlatformDialog,
          end: closePlatformDialog,
        },
        (value: string, index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          const text = (value ?? '').trim()
          // 页内先挡一道（>0、两位小数）只为省一次往返；判据仍在后端 `PriceSerializer`。
          if (!isValidPlatformPrice(text)) {
            errMsg(PRICE_INVALID_MSG)
            return
          }
          void runPlatformAction(
            priceButton,
            () => changeOrderPlatformPushPrice(orderNo, text),
            PRICE_SUCCESS_MSG,
          )
        },
      )
      return
    }

    const timeButton = target.closest<HTMLElement>(PLATFORM_ACTION_SELECTORS.time)
    if (timeButton !== null) {
      platformDialogOpen = true
      ;(
        layer.prompt as (options: unknown, yes: (value: string, index: number) => void) => void
      )(
        {
          formType: 0,
          value: '',
          title: TIME_PROMPT_TITLE,
          btn: ['确定', '取消'],
          btn2: closePlatformDialog,
          cancel: closePlatformDialog,
          end: closePlatformDialog,
        },
        (value: string, index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          const text = (value ?? '').trim()
          // 正整数（后端 `IntegerField`）；>720 由后端答，前端不替它猜边界文案。
          if (!isValidPlatformHours(text)) {
            errMsg(TIME_INVALID_MSG)
            return
          }
          void runPlatformAction(
            timeButton,
            () => addOrderPlatformPushTime(orderNo, Number(text)),
            TIME_SUCCESS_MSG,
          )
        },
      )
      return
    }

    const messageButton = target.closest<HTMLElement>(PLATFORM_ACTION_SELECTORS.message)
    if (messageButton !== null) {
      // **直接读面板输入框**（对标 `platfromorderview.js::searchDltmsg` 的口径），
      // 不再开第二层输入层——否则用户在面板里敲的字只是「初值」，与「发送后清空输入框」也对不上。
      const input = root.querySelector<HTMLInputElement>(PLATFORM_MSG_INPUT_SELECTOR)
      const text = (input?.value ?? '').trim()
      if (text === '') {
        errMsg(MESSAGE_EMPTY_MSG)
        return
      }
      void sendPlatformMessage(messageButton, text)
      return
    }

    // ⭐ dlt-d：加价（增量；金额判据与改价同一套）。
    const upPriceButton = target.closest<HTMLElement>(PLATFORM_ACTION_SELECTORS.upPrice)
    if (upPriceButton !== null) {
      platformDialogOpen = true
      ;(
        layer.prompt as (options: unknown, yes: (value: string, index: number) => void) => void
      )(
        {
          formType: 0,
          value: '',
          title: UP_PRICE_PROMPT_TITLE,
          btn: ['确定', '取消'],
          btn2: closePlatformDialog,
          cancel: closePlatformDialog,
          end: closePlatformDialog,
        },
        (value: string, index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          const text = (value ?? '').trim()
          if (!isValidPlatformUpPrice(text)) {
            errMsg(UP_PRICE_INVALID_MSG)
            return
          }
          void runPlatformAction(
            upPriceButton,
            () => upPriceOrderPlatformPush(orderNo, text),
            UP_PRICE_SUCCESS_MSG,
          )
        },
      )
      return
    }

    // 补款：后端 `add_push_amount` **整数元 ≥1**、代练中 / 待验收都能做；「加价」仍走 `up-price/`。
    const appendPriceButton = target.closest<HTMLElement>(
      PLATFORM_ACTION_SELECTORS.appendPrice,
    )
    if (appendPriceButton !== null) {
      platformDialogOpen = true
      ;(
        layer.prompt as (options: unknown, yes: (value: string, index: number) => void) => void
      )(
        {
          formType: 0,
          value: '',
          title: APPEND_PRICE_PROMPT_TITLE,
          btn: ['确定', '取消'],
          btn2: closePlatformDialog,
          cancel: closePlatformDialog,
          end: closePlatformDialog,
        },
        (value: string, index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          const text = (value ?? '').trim()
          // 页内先挡一道（整数 + ≥1）只为省一次往返；判据仍在后端 `AmountSerializer`。
          if (!isValidPlatformAmount(text)) {
            errMsg(APPEND_PRICE_INVALID_MSG)
            return
          }
          void runPlatformAction(
            appendPriceButton,
            () => addOrderPlatformPushAmount(orderNo, text),
            APPEND_PRICE_SUCCESS_MSG,
          )
        },
      )
      return
    }

    // 全平台改价。
    const repriceAllButton = target.closest<HTMLElement>(
      PLATFORM_ACTION_SELECTORS.repriceAll,
    )
    if (repriceAllButton !== null) {
      platformDialogOpen = true
      ;(
        layer.prompt as (options: unknown, yes: (value: string, index: number) => void) => void
      )(
        {
          formType: 0,
          value: '',
          title: REPRICE_ALL_PROMPT_TITLE,
          btn: ['确定', '取消'],
          btn2: closePlatformDialog,
          cancel: closePlatformDialog,
          end: closePlatformDialog,
        },
        (value: string, index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          const text = (value ?? '').trim()
          if (!isValidPlatformPrice(text)) {
            errMsg(REPRICE_ALL_INVALID_MSG)
            return
          }
          void runPlatformBulkPrice(repriceAllButton, text)
        },
      )
      return
    }

    // 重发此单 ⇒ **打开发单页**。
    // ⛔ 不在这里自己拼发单载荷：价格 / 双金 / 时限 / 区服 / 门槛的唯一出处是发单页的预览接口。
    const repushButton = target.closest<HTMLElement>(PLATFORM_ACTION_SELECTORS.repush)
    if (repushButton !== null) {
      openPushJdpPage(
        layer,
        orderNo,
        order === null ? '' : displayOrderNo(order),
        REPUSH_LAYER_TITLE,
      )
      return
    }

    // 撤销区的「同意撤销」——打手发起协商撤销时，由我们同意。
    // 端点不收任何入参（空载荷）；⚠️ 失败**不刷新**面板（业务方口径：原样显示后端中文，别把现场刷掉）。
    const agreeCancelButton = target.closest<HTMLElement>(
      PLATFORM_ACTION_SELECTORS.agreeCancel,
    )
    if (agreeCancelButton !== null) {
      platformDialogOpen = true
      // 确认框正文里**必须写出打手要多少**（只读）；
      // ⛔ 不能只给一颗「同意」按钮让人不知道在同意多少钱。金额 / 撤销要求按**最新有效行**取。
      const currentPush = latestActivePushRow(platformRows)
      ;(
        layer.confirm as (
          content: string,
          options: unknown,
          yes: (index: number) => void,
          cancel: () => void,
        ) => void
      )(
        buildAgreeCancelConfirmText(currentPush),
        {
          title: AGREE_CANCEL_CONFIRM_TITLE,
          btn: ['同意', '取消'],
          cancel: closePlatformDialog,
          end: closePlatformDialog,
        },
        (index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          void runPlatformAction(
            agreeCancelButton,
            () => agreeOrderPlatformPushCancel(orderNo),
            AGREE_CANCEL_SUCCESS_MSG,
            { refreshOnFailure: false },
          )
        },
        closePlatformDialog,
      )
      return
    }

    // 撤销区的「申请撤销」——由我们发起协商撤销（与「从平台撤回」同端点、话术不同：这里写「等打手同意」）。
    // **三格表单**（两个金额用推送行现值预填、可改 + 撤销理由）；两格都空 ⇒ 仍是空载荷（后端按详情现值）。
    // 成功话术按**返回行**判（`withdrawResultMessage`，⛔ 不复述后端固定 msg）。
    const applyCancelButton = target.closest<HTMLElement>(
      PLATFORM_ACTION_SELECTORS.applyCancel,
    )
    if (applyCancelButton !== null) {
      platformDialogOpen = true
      // 预填按**最新有效行**取（与后端金额同一行）；取不到 ⇒ 两格留空、要求手填（⛔ 不编 0）。
      const formEl = buildPlatformRevokeForm(latestActivePushRow(platformRows))
      ;(layer.open as (options: unknown) => void)({
        type: 1,
        title: APPLY_CANCEL_DIALOG_TITLE,
        // ⚠️ jQuery 形状 + end 清理（裸节点会让 layer 抛错、弹层「点了没反应」；见 `layerNode`）。
        content: layerNode(formEl),
        end: () => {
          formEl.remove()
          closePlatformDialog()
        },
        area: ['520px', 'auto'],
        shadeClose: false,
        btn: [APPLY_CANCEL_SUBMIT_TEXT, '取消'],
        yes: (index: number) => {
          const payload = readPlatformRevokePayload(formEl)
          if (payload === null) {
            // 金额不合法 ⇒ 页内先挡、**弹层不关**（用户改完再提交），零请求。
            errMsg(APPLY_CANCEL_AMOUNT_INVALID_MSG)
            return
          }
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          void runPlatformAction(
            applyCancelButton,
            () => withdrawOrderPlatformPush(orderNo, payload),
            withdrawResultMessage,
            // 上游只说「参数错误」时翻成人话；其它原因仍原样透出后端中文。
            { mapFailure: withdrawFailureMessage },
          )
        },
        btn2: (index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
        },
        cancel: closePlatformDialog,
      })
      return
    }

    // 「取消撤销」——取消**我们自己发起的**协商撤销申请（端点不收任何入参 ⇒ 空载荷）。
    // ⚠️ 后端未上线时按「结果未知」降级（「已发出，请刷新核对」，⛔ 不谎报成功）；
    // 失败**不刷新**面板（与同意撤销同口径）。
    const requestCancelButton = target.closest<HTMLElement>(
      PLATFORM_ACTION_SELECTORS.requestCancel,
    )
    if (requestCancelButton !== null) {
      platformDialogOpen = true
      ;(
        layer.confirm as (
          content: string,
          options: unknown,
          yes: (index: number) => void,
          cancel: () => void,
        ) => void
      )(
        CANCEL_REQUEST_CONFIRM_TEXT,
        {
          title: CANCEL_REQUEST_CONFIRM_TITLE,
          btn: ['确定', '取消'],
          cancel: closePlatformDialog,
          end: closePlatformDialog,
        },
        (index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          void runPlatformAction(
            requestCancelButton,
            () => cancelOrderPlatformPushRequest(orderNo),
            CANCEL_REQUEST_SUCCESS_MSG,
            { refreshOnFailure: false },
          )
        },
        closePlatformDialog,
      )
      return
    }

    // 申请客服介入（只提交申请，结果靠对方 / 回读核验）。
    // 后端明确 `arbitration_available=false` ⇒ 按钮已置灰，这里再拦一道并显示后端原因（含剩余分钟）；
    // 字段缺席 / true ⇒ 照旧开确认框，失败时原样显示上游中文。
    const arbitrationButton = target.closest<HTMLElement>(
      PLATFORM_ACTION_SELECTORS.arbitration,
    )
    if (arbitrationButton !== null) {
      // 仲裁可用性也按**最新有效行**取（与金额同一行，避免「行 A 的钱 + 行 B 的闸」）。
      const currentPush = latestActivePushRow(platformRows)
      if (currentPush !== null && currentPush.arbitration_available === false) {
        const reason = (currentPush.arbitration_blocked_reason ?? '').trim()
        errMsg(reason === '' ? ARBITRATION_UNAVAILABLE_MSG : reason)
        return
      }
      platformDialogOpen = true
      ;(
        layer.confirm as (
          content: string,
          options: unknown,
          yes: (index: number) => void,
          cancel: () => void,
        ) => void
      )(
        ARBITRATION_CONFIRM_TEXT,
        { title: '申请客服介入', btn: ['确定', '取消'], cancel: closePlatformDialog, end: closePlatformDialog },
        (index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          void runPlatformAction(
            arbitrationButton,
            () => requestOrderPlatformPushArbitration(orderNo),
            ARBITRATION_SUCCESS_MSG,
          )
        },
        closePlatformDialog,
      )
      return
    }

    // 评价打手。两步入参（`EvalType` 码值 + 可留空的评价内容），
    // 后端只认 10/11/12/13；内容为空时**不送 `comment` 键**。
    const valueButton = target.closest<HTMLElement>(PLATFORM_ACTION_SELECTORS.value)
    if (valueButton !== null) {
      platformDialogOpen = true
      ;(
        layer.prompt as (options: unknown, yes: (value: string, index: number) => void) => void
      )(
        {
          formType: 0,
          value: '',
          title: VALUE_RATING_PROMPT_TITLE,
          btn: ['下一步', '取消'],
          btn2: closePlatformDialog,
          cancel: closePlatformDialog,
          end: closePlatformDialog,
        },
        (value: string, index: number) => {
          ;(layer.close as (i: number) => void)(index)
          const evalType = parsePlatformEvalType(value ?? '')
          if (evalType === null) {
            closePlatformDialog()
            errMsg(VALUE_RATING_INVALID_MSG)
            return
          }
          ;(
            layer.prompt as (
              options: unknown,
              yes: (value: string, index: number) => void,
            ) => void
          )(
            {
              formType: 2,
              value: '',
              title: VALUE_COMMENT_PROMPT_TITLE,
              btn: ['提交', '取消'],
              area: ['520px', '160px'],
              btn2: closePlatformDialog,
              cancel: closePlatformDialog,
              end: closePlatformDialog,
            },
            (comment: string, commentIndex: number) => {
              ;(layer.close as (i: number) => void)(commentIndex)
              closePlatformDialog()
              void runPlatformAction(
                valueButton,
                () =>
                  valueOrderPlatformPush(
                    orderNo,
                    evalType,
                    (comment ?? '').trim(),
                  ),
                VALUE_SUCCESS_MSG,
              )
            },
          )
        },
      )
      return
    }

    // 下架 / 上架（`flag` 0 / 1；只有未接手能操作，后端再判一次）。
    const offSellButton = target.closest<HTMLElement>(PLATFORM_ACTION_SELECTORS.offSell)
    if (offSellButton !== null) {
      platformDialogOpen = true
      ;(
        layer.confirm as (
          content: string,
          options: unknown,
          yes: (index: number) => void,
          cancel: () => void,
        ) => void
      )(
        OFF_SELL_CONFIRM_TEXT[0],
        { title: '下架', btn: ['确定', '取消'], cancel: closePlatformDialog, end: closePlatformDialog },
        (index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          void runPlatformAction(
            offSellButton,
            () => offSellOrderPlatformPush(orderNo, 0),
            OFF_SELL_SUCCESS_MSG[0],
          )
        },
        closePlatformDialog,
      )
      return
    }

    const onSellButton = target.closest<HTMLElement>(PLATFORM_ACTION_SELECTORS.onSell)
    if (onSellButton !== null) {
      platformDialogOpen = true
      ;(
        layer.confirm as (
          content: string,
          options: unknown,
          yes: (index: number) => void,
          cancel: () => void,
        ) => void
      )(
        OFF_SELL_CONFIRM_TEXT[1],
        { title: '上架', btn: ['确定', '取消'], cancel: closePlatformDialog, end: closePlatformDialog },
        (index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          void runPlatformAction(
            onSellButton,
            () => offSellOrderPlatformPush(orderNo, 1),
            OFF_SELL_SUCCESS_MSG[1],
          )
        },
        closePlatformDialog,
      )
      return
    }

    // 锁定 / 解锁（按点了哪一颗决定方向）；输入层**可填原因**——
    // 没填就不送这个键（载荷恰好），填了前后空白由页面 trim 掉。
    const lockButton = target.closest<HTMLElement>(PLATFORM_ACTION_SELECTORS.lock)
    if (lockButton !== null) {
      openLockReasonPrompt(1, lockButton)
      return
    }

    const unlockButton = target.closest<HTMLElement>(PLATFORM_ACTION_SELECTORS.unlock)
    if (unlockButton !== null) {
      openLockReasonPrompt(0, unlockButton)
      return
    }

    // 拉黑这张单的接单打手（UID 由后端从推送行的 `accept_uid` 取）。
    const blacklistButton = target.closest<HTMLElement>(
      PLATFORM_ACTION_SELECTORS.blacklist,
    )
    if (blacklistButton !== null) {
      platformDialogOpen = true
      ;(
        layer.confirm as (
          content: string,
          options: unknown,
          yes: (index: number) => void,
          cancel: () => void,
        ) => void
      )(
        BLACKLIST_CONFIRM_TEXT,
        { title: '拉黑打手', btn: ['确定', '取消'], cancel: closePlatformDialog, end: closePlatformDialog },
        (index: number) => {
          closePlatformDialog()
          ;(layer.close as (i: number) => void)(index)
          void runPlatformAction(
            blacklistButton,
            () => blacklistOrderPlatformPush(orderNo, 0),
            BLACKLIST_SUCCESS_MSG,
          )
        },
        closePlatformDialog,
      )
    }
  }

  root.addEventListener('click', onPlatformTabClick)
  root.addEventListener('click', onPlatformActionClick)
  // 截图上传的两条入口 —— 文件选择器的 change + 拖拽落下。
  if (screenshotInput !== null) {
    screenshotInput.addEventListener('change', onScreenshotInputChange)
  }
  root.addEventListener('dragover', onPlatformDragOver)
  root.addEventListener('drop', onPlatformDrop)

  /**
   * 发单页（iframe）发单成功 ⇒ 关弹窗 + 重新取本单 + 代练通页签重新取数 + 出提示。
   * 协议常量 `PUSH_JDP_SENT_MESSAGE_TYPE` 的唯一出处是 `platformPush.ts`。
   */
  const onPushJdpSentMessage = (event: MessageEvent): void => {
    if (event.origin !== window.location.origin) return
    const data = event.data as { type?: string } | undefined
    if (!data || data.type !== PUSH_JDP_SENT_MESSAGE_TYPE) return
    if (pushJdpLayerIndex !== null) {
      ;(layer.close as (i: number) => void)(pushJdpLayerIndex)
      pushJdpLayerIndex = null
    }
    void loadAndRenderOrder()
    void loadPlatformPushes()
    islandMsg(layer as unknown as Record<string, unknown>, PUSH_JDP_SENT_TEXT)
  }
  window.addEventListener('message', onPushJdpSentMessage)

  // 两块日志的首屏取数收口（`loadAudit` 自己 catch 失败并弹后端 msg ⇒ 这里不会 throw）。
  const auditLoaded = loadAudit()
  await loadAndRenderOrder()
  await auditLoaded

  ;(form.render as () => void)()

  return () => {
    // ⚠️ **必须清定时器**：页面挂在 iframe 里，卸载后那个 `setInterval` 仍在
    // ⇒ 既泄漏、又让已卸载的页面继续发请求。
    clearInterval(refreshTimer)
    // 两颗按钮的委托也要摘（`root.innerHTML = ''` 只清标记，不清挂在 `root` 上的监听器）。
    root.removeEventListener('click', onCopyClick)
    root.removeEventListener('click', onSplitClick)
    // 代练通页签的取数 / 动作按钮的委托同样要摘（否则卸载后它们还挂在旧 root 上）。
    root.removeEventListener('click', onPlatformTabClick)
    root.removeEventListener('click', onPlatformActionClick)
    // 截图上传的两条入口同样要摘（change 挂在 input 上，input 随 innerHTML 一起消失）。
    if (screenshotInput !== null) {
      screenshotInput.removeEventListener('change', onScreenshotInputChange)
    }
    root.removeEventListener('dragover', onPlatformDragOver)
    root.removeEventListener('drop', onPlatformDrop)
    window.removeEventListener('message', onPushJdpSentMessage)
    // 「尚未开放」捕获委托同样要摘。
    unbindPlatform()
    root.innerHTML = ''
  }
}

export default {
  mount: (root: HTMLElement, ctx: IslandContext) => mountOrderOrderViewPage(root, ctx),
} satisfies IslandPage
