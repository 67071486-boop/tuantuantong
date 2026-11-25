/**
 * 【发单到交易平台（2026）】页的 **layui 复刻页**（对标物 `/Order/PushJdp2026?orderId=…`）。
 *
 * 对标这一页不是 HTML 弹层，而是**列表页 `layer.open({type:2})` 装进来的独立 iframe 页**
 * （`OrderList20191212.js` L4683-4694：`title = viewId + "发单到交易平台：请确认发单内容"`、
 * `skin:'layui-layer-lan'`、`anim:8`、桌面 `area:['1280px', 屏高分支]`）。所以我们照同一形状
 * 开一个 iframe，src 指向**我们自己的岛页**（`layui-island/page.html?page=order-pushjdp&order_no=…`）；
 * 打开动作与标题拼接在 `order-orderlist.ts` 的 `openPushJdpLayer` 里。
 *
 * ## 证据与状态
 *
 * - 标记 / 页内 `<style>` → `public/replica/order-pushjdp.html`（已配置交易平台态的抓件，
 *   PII 已清空；文件头逐条登记了清空项与保留项）。
 * - **已配置态**是服务端渲的三个 `layui-hide` 开关（外发提示条显示 / 未配置提示卡隐藏 /
 *   表单块显示）。对标页面 JS **从不切这三处** ⇒ 证据件静态落地即为已配置态。
 *   侦察依据：`.superpowers/reports/pushjdp-configured-recon.md` §4 第 1–3 条（实测）。
 * - 左侧 24 项表单、右侧「本系统订单内容」只读框、模板弹层全部照证据件渲染；
 *   底部主按钮文案照对标「确定发单到第三方交易平台」。
 *
 * ## T9d/ F3：底部主按钮**接真**，其余照旧复刻
 *
 * **只提交已接真的业务字段 + `platform=10`**（后端 `PushToPlatformSerializer`）：
 *
 * | 页面控件（证据件） | 提交键 | 取值 |
 * |---|---|---|
 * | 发平台价格 `#throwFee` | `price` | 输入框原文（必填，空则拦） |
 * | 平台安全保证金 `#safeAmount` | `ensure1` | 输入框原文；空 ⇒ `0.00` |
 * | 平台效率保证金 `#effAmount` | `ensure2` | 输入框原文；空 ⇒ `0.00` |
 * | 时间要求 `select[name=day]` + `select[name=hours]` | `time_limit_hours` | `day * 24 + hours`（1–720；默认 30 天 ⇒ 720） |
 * | 游戏/平台（运行期改成可搜索） | `zone_server_id` | 从区服下拉里**选中**的那一条 |
 * | 要求最低完单数 `select[name=dlmmlimitAccept]` | `power_level` | 选中的数字（`0` = 不限制）；设置没读到真值 ⇒ **不传**，由后端用全局默认 |
 * | 拒绝新手和介入率 `select[name=limitAccept]` | `limit_accept` | 选中的数字；`0`（不设置）⇒ 传 `null`；设置没读到真值 ⇒ **不传** |
 * | 当前订单说明 `textarea[name=gaemTf]` | `memo` | **框里的当前值**（打开时由 `preview.memo` 带出，客服可改）；P9 2026-10-27 起真发 |
 * | 同发内部抢单价 `#throwFeeHome`（G4） | `hall_price_same` | 填了 **>0** 才发；`0` / 空 = 不发（大厅价沿用发单价） |
 * | 发单频道 `#pushChannel`（G4） | `groups` | 选中的频道值（`PUBLIC` / `GOODCHANNEL` / `CERTCHANNEL`）；「指定打手 / 指定群组 / 接单密码 / 丸子悬赏」本版发不了 |
 * | 标签 `#label1`（G4） | `label1` | 框里的值（trim）；空 = 不发（后端还支持 `label2`，但证据件里没有那一格） |
 *
 * 页面其余字段（账号 / 密码 / 角色 / 标题 / 玩家电话 / QQ / `ugtext`…）**保留复刻、照旧不参与提交**；
 * 按钮上方加一句如实说明（`LIVE_NOTE_TEXT`）。⛔ 不要因为它们不提交就把控件置灰或删掉。
 *
 * ## G4：延迟发平台 / 指定人·组 / 同发内部抢单价 / 频道 / 标签
 *
 * - **延迟发平台**（对标 `PJ-html:662-677`、`PJ-js:800-818`）：块照对标渲染，
 *   「发平台价 >0 **且** 同发内部抢单价 >0」时显示；但**本仓后端没有 `isDelay` / `delaySec`
 *   入参**（`PushToPlatformSerializer` 无这两个键，服务层也没有排期机制）⇒ 选「是」时页面
 *   明确说「后端尚未支持、不会延迟」，并在提交时**拦下、零请求**（⛔ 不拿「立即发平台」
 *   冒充「已延迟」）。两个字段**不提交**。
 * - **发单频道**：选「指定打手 / 指定群组」时显示「指定人或组」那格、选「接单密码」时
 *   同一格改叫「指定接单密码」，其余频道隐藏并清空（逐条照 `PJ-js:31-51`）。
 *   历史下拉（`GetPushPlatfromAssisUserLog` / `Del…`）**后端没有对应端点** ⇒ 按对标渲染，
 *   聚焦时显示一句「尚未开放」、**零请求**（⛔ 不假装有历史、⛔ 不做删除）。
 * - `ugtext`（指定对象）**不提交**：发单接口没有这个字段；选那三个频道时提交被拦下并说清原因。
 * - `hall_price_same` / `groups` / `label1` 随单提交（后端已支持），`0` / 空按「不发」处理。
 *
 * ## 区服选择（运行期注入，证据件一个字没动）
 *
 * 证据件里「游戏/平台」那一格是**只读文本框**（服务端直接渲出订单的区服名，没有可选项）。
 * 本任务把它运行期改成可搜索输入 + 下拉：
 * 输入（防抖 `ZONE_DEBOUNCE_MS`=300ms）→ `GET /api/business/platform-integrations/zones/?q=` →
 * 选一条后记住 `zone_server_id`、输入框回显 `server_name`。
 * **未选中就提交 ⇒ 中文提示 + 零请求**（后端那道 `zone_server_id` 必填闸不替页面背锅）。
 *
 * ## 提交结果三态（⛔ 不许假装成功）
 *
 * - `state=sent`：显示平台单号 +「已发单到代练通」+「关闭后可在订单列表查看」；
 * - `state=failed`：显示后端 `last_error` **原文**（前缀「发单失败：」）；
 * - `state=unknown`：**醒目**提示「已提交但未拿到平台单号，请人工核对后再操作」，
 *   并把后端 `last_error` 一起显示出来；
 * - 前置闸失败（`code=1002`，`request()` 抛 `BizError`）同样按失败展示后端 `msg` 原文；
 * - **在途**只显示「提交中…」（⛔ 不是「已提交」——此刻什么都还没回来）；
 * - **网络错误 / 超时 / 5xx / 非信封**按 **unknown** 处理（「请求已发出但没拿到结果，请到
 *   代练通后台核对后再操作」）——写动作可能已经到达上游，⛔ 既不许说「失败」（会诱导重复发单），
 *   也不许说「已提交」（会让人以为后台已有单）。只有 `BizError` / 4xx 才允许落到 `failed`。
 *   分类的唯一出处是 `classifyPushError()`。
 *
 * 提交中按钮 `disabled` + `submitting` 旗标双保险（双击发两单是真钱）。
 *
 * ## 成功 ⇒ 自动关窗 + 父页刷新（2026-10-28 真机反馈）
 *
 * 发单页跑在 `layer.open({type:2})` 的 iframe 里，**只有父窗口能关弹层 / 刷新列表**
 * ⇒ 成功（`state=sent` 且拿到平台单号）时：
 * 1. 弹窗里先写成功结果块 + `layer.msg('发单成功')`；
 * 2. **清空**可能残留的输入（价格 / 账号资料 / 标题 / 联系 QQ·电话 / 代练要求…）；
 * 3. `window.parent.postMessage({type: 'island:push-jdp-sent', order_no})` —— 父页收到后
 *    **关弹窗 + 刷新列表 + 提示「发单成功」**（协议常量唯一出处 `layui-island/platformPush.ts`；
 *    订单列表与订单详情两个宿主都接）。
 * 4. 成功后 `succeeded` 旗标让按钮保持禁用（⛔ 不发第二单）。
 *
 * ⚠️ **失败 / 状态未知一律不关窗**：表单保留、后端中文原文写在结果块里，改完可以再提交。
 * 父窗口不响应（单独打开本页）时，成功结果 + 提示仍留在页面上。
 *
 * ## F1–F5（2026-10-27，实施计划 §2.3 / §2.4）一键抛单
 *
 * | 任务 | 页面行为 |
 * |---|---|
 * | F1 | 打开即填，**唯一的默认值来源 = 后端发单预览** `GET orders/<no>/platform-push/preview/`：价格 / 两个保证金 / 时限 / 区服（含默认区服）/ 标题 / 账号资料五段 / 联系 QQ·电话 / 门槛，全是后端**将要真正发送**的值（⛔ 前端不再自己派生）。`can_push=false`（仅「弹窗里改不了」的原因：没配置 / 状态不允许 / 已抛过）⇒ 显示 `reason` 并**禁用提交**；价格 < 2 元与区服未选是**客服能改**的 ⇒ 按钮保持可点、**提交时**拦下给中文提示 |
 * | F2 | 右面板接 `GET platform-integrations/summary/`：平台 / 账号 / 可用余额 / 冻结余额；没配置、查询失败、网络失败都显示**中文原因**（⛔ 不显示「暂无数据」） |
 * | F3 | 门槛两格按全局默认（完单数默认 1 / 介入率默认不设置）；**本单可改** ⇒ 发单载荷带 `power_level` / `limit_accept`（介入率「不设置」发 `null`）；「保存介入率和完单数设置」只提交这两个键当**全局默认**，成功提示**「保存成功」**、失败显示后端原文 |
 * | F4 | 代练要求收成**一份全局模板**（`require_template`）：编辑弹窗保存即全局生效；「设为默认 / 删除选中的模板」只解释「只有一份」、零请求（⛔ 不做多模板） |
 * | F5 | 「游戏/平台」是**下拉**（按 `preview.game_id` 取该游戏全部区服，默认选预览给的那条 / 没有就选第一条）；手选后带 `zone_server_id` **重新取一次预览**（只更新区服与 `can_push`）；迟到的旧预览不覆盖用户已选 |
 *
 * ⚠️ **默认值的数据来源**（2026-10-27 改准：只认预览，⛔ 不再自己算 / 拼别的接口）：
 * - **所有表单默认值**：`GET /api/business/orders/<单号>/platform-push/preview/`
 *   （价格、两个保证金、时限、区服 + `zone_source`、标题、账号资料五段、联系 QQ / 电话、门槛、
 *   `can_push` / `reason`）——契约见 `api/dailiantongPush.ts::PlatformPushPreview`；
 * - 门槛 / 模板的**全局设置**（F3 保存按钮 / F4 编辑弹窗）仍读 `platform-integrations/settings/`；
 * - 右面板：`GET platform-integrations/summary/`；
 * - 区服手选搜索：`GET platform-integrations/zones/?q=`。
 *
 * ⚠️ **预览失败时**只显示中文原因、**不禁用**提交（后端提交时还有同一套闸；⛔ 不猜 `can_push`）。
 * ⚠️ **旧响应**：用户手选区服会再取一次预览 ⇒ 只有**最新**那一发的区服 / `can_push` 生效，
 * 且只有首次响应填非区服字段（迟到的默认值不许覆盖用户已改的内容）。
 *
 * ## ⭐ P8 / P9（2026-10-27 业务方真机 bug）：当前订单说明 = 发出去的代练内容
 *
 * 现场：客服改了「当前订单说明」（`textarea[name=gaemTf]`）里的字，打手收到的还是后端按
 * `Order.content/remark` 生成的旧内容（页面写 A、实际发 B）；而底部「将发到代练通的内容」
 * 那块显示的是 `preview.memo`，也与框无关。
 *
 * 修法（两块**同一份内容**，⛔ 不许各说各话）：
 * - 打开时把 `preview.memo`（= `Order.content` + `Order.remark`，≤2048 字）填进「当前订单说明」框；
 * - 提交载荷带 **`memo` = 该框的当前值**（用户改过就用用户的；空串也照发）；
 * - 底部预览块改为**镜像该框**（框里输入即时更新），⛔ 不再独立显示 `preview.memo`；
 *   预览接口读取失败时如实写「读取失败」，⛔ 不停在「正在读取」。
 * ⚠️ 后端 `PushToPlatformSerializer.memo` 入参「同时在加」（P9）；后端没接时该键会被 DRF 忽略。
 *
 * ## 与对标不同（如实登记，别当成抄漏）
 *
 * | # | 差异 | 原因 |
 * |---|---|---|
 * | 1 | 右栏平台表 `#tableList` **渲染成空表** | 它的数据源 `GET /OrderAjax/GetOrderPushTransfromInterfaceList` 本节没有；标准 §7「接口不存在 → 空表」。平台表里的**绿勾是行数据勾选态**，没有数据行就不会出现（同一条口径） |
 * | 2 | 「常用模板」下拉 `#tempList` **接真**：打开即 `GET /api/business/platform-integrations/order-templates/`（= M5「代练通订单要求模板」管理页同一份**上游**库），选中一条 ⇒ 模板正文填进 `requestContent`（对标语义：覆盖）；「[默认]」前缀照对标。对标另有「`isdef=Y` 默认模板预填空正文」**不做**——空正文的默认值归 F4 全局模板管，两个预填源会打架（当时建档口径见 `.tools/recon/pushjdp-20261026/assets/JS2019/Order/pushjdp.js:207-287`） | 页面建档（F1–F5）时取数端点还不存在（原差异登记），M5建好端点后本条补上；业务方 2026-10-30 报「模板已配置、这里用不了」即本缺口 |
 * | 3 | 隐藏域 `#orderID` 从岛页查询参数 `order_no` 回填（`^\d{20}$` 形状闸） | 证据件里的单号已按 PII 清空；对标是服务端直接渲进去。其余隐藏域（`tGameName` / `tGameArea` / `tGameServer` / `jdp` / `tproject`）本节留空 |
 * | 4 | 「内部保证金要求」块保持证据件里的 `style="display:none"` | 抓件实测它在已配置态隐藏，但归因未确定（可能是租户级「内部抢单」设置，侦察报告 §4 第 5 条）。按快照保留，⛔ 不猜 |
 * | 5 | 页内其余按钮/链接触发**页内 `layer.msg`**（「尚未开放」） | 对标各自有真实动作，本节不做；照标准 §5 给「尚未开放」，不置灰 |
 * | 6 | **底部主按钮已接真**（T9d + F3 + G4） | 只提交上表业务字段 + `platform=10`（含两个接单门槛、`hall_price_same` / `groups` / `label1`）；页面其余字段由后端按订单自动生成。**与对标不同**：对标把整张表单一起发出去，我们有意只发这一组（契约见 `api/dailiantongPush.ts`） |
 * | 7 | **区服搜索控件是运行期注入的**（证据件里那格是只读文本框） | 对标由服务端渲出订单区服名，没有可搜索下拉。我们是「列表页点发单、再选区服」的动线 ⇒ 必须能选。**无对标视觉证据**：下拉/提示的视觉值自拟，⛔ 证据件不动 |
 * | 8 | **时限由「时间要求」两个下拉合成** | 对标把 `day` / `hours` 原样发出去；我们的入参只有 `time_limit_hours` ⇒ 页面按 `day*24+hours` 换算（默认 30 天 = 720，正好是后端上限）。**这是推断的映射**：后端没有单独的「天」字段 |
 * | 9 | **结果区是运行期追加的**（按钮上方那句说明 + 三态结果块） | 对标没有这个块（它发完单就关层）。本版要求「三态都要真展示」，所以必须有一块**不随 layer 消失**的可见结果；视觉值自拟 |
 * | 10 | **网络错误 / 超时 / 5xx 按 `unknown` 展示** | 对标没有这层语义；本仓后端把「写动作超时」建模成 `state=unknown` 且不重试 ⇒ 页面必须让人去平台后台核对，⛔ 不许说成失败或已提交 |
 * | 11 | 右栏平台表改成**摘要表**（发往平台 / 平台发单账号 / 可用余额 / 冻结余额，4 列，无复选框） | F2：计划 §2.3 要求显示这四个值；对标那列「平台余额」是「点击查看」链接，它调的对标端点本节没有。没拿到余额显示 `—`（⛔ 不伪造 0.00），空表时显示中文原因（⛔ 不显示 layui 默认的「暂无数据」） |
 * | 12 | 「游戏/平台」那一格由只读 **input 换成 `<select>` 下拉**（运行期注入；原 input 保留、`display:none`） | F5（业务方 2026-10-27）：要「可翻的下拉」、列出该游戏全部区服、默认第一个；对标那格是服务端渲的死文本 ⇒ 形态不同，登记在此。视觉值自拟，⛔ 证据件不动 |
 * | 19 | 提交按钮上方新增**「将发到代练通的内容」只读预览块**，内容 = **「当前订单说明」框的当前值**（P9 改准：不再独立显示 `preview.memo`；框空时显示「订单没有填写内容」、预览失败时显示读取失败） | 业务方 2026-10-27：客服发之前要能看见打手会看到什么；⚠️ P9 起提交载荷的 `memo` 就取这一格 ⇒ 预览必须与框**同一份内容**，否则页面说假话 |
 * | 20 | `preview.warnings` 在按钮上方以提示块显示（价格 / 区服等客服能改的问题） | 这些是**可改问题**，⛔ 不据此禁用提交；`can_push=false`（硬原因）才禁用 |
 * | 21 | **「延迟发平台」块照对标显示/隐藏，但不提交、选「是」时明确拒** | G4：对标 `PJ-js:800-818` 的 `delayDivShow()`（两个价都 >0 才显示）照做；但本仓后端**没有** `isDelay` / `delaySec` 入参、也没有排期机制（`PushToPlatformSerializer` + 服务层逐条核过）⇒ 选「是」提交 = 立即发平台，会让人以为延迟了。页面块内常驻一句「后端尚未支持」，提交时**拦下、零请求**，⛔ 不假装延迟 |
 * | 22 | **发单频道联动「指定人或组 / 接单密码」照对标；历史下拉只显示「尚未开放」、零请求** | G4：`PJ-js:31-51` 的显隐 / 文案切换照做。历史下拉的两个端点（`GetPushPlatfromAssisUserLog` / `Del…`）本仓后端**没有**；`ugtext` 也没有对应提交字段（后端只有 `groups` = 频道本身）⇒ 聚焦时给一句「尚未开放」、不发任何请求；选「指定打手 / 指定群组 / 接单密码」时提交被拦下并说清原因（那几个频道需要指定对象，发不出去） |
 * | 23 | **同发内部抢单价 / 发单频道 / 标签真的提交** | G4：后端 `PushToPlatformSerializer` 的 `hall_price_same` / `groups` / `label1` 已支持。`hall_price_same` 只在 **>0** 时发（`0` = 不发，大厅价沿用发单价）；`groups` 发选中频道（`PUBLIC` / `GOODCHANNEL` / `CERTCHANNEL`）；`label1` 非空才发。⚠️ `label2` 后端支持但**证据件里没有那一格** ⇒ 不提交（不凭猜测补控件） |
 * | 24 | **发单成功 ⇒ 自动关窗 + 父页刷新列表 + 父页提示「发单成功」**；成功前先清空残留输入 | 业务方 2026-10-28 真机：发完单弹窗不关、看不到列表刷新。关窗/刷新只有 iframe 的父窗口做得到 ⇒ 走 `postMessage`（同 `island:order-saved` 的既有协议形状） |
 * | 13 | 门槛两格与模板的默认值来自 `platform-integrations/settings/` | F3 / F4：这是本仓的全局设置，对标是**每个人各自保存**（那颗按钮的 `lay-tips` 原话）；业务方裁定存全局默认。读取失败时两道提交闸都关着（⛔ 不让证据件默认值覆盖真配置） |
 * | 14 | 模板区**只保留一份全局模板**：「新建 / 编辑」打开同一个编辑弹层、「设为默认 / 删除选中的模板」只解释「只有一份」、模板标题那行隐藏 | F4（业务方「⛔ 不做多模板」）：`require_template` 只有正文。设为默认在单模板下没有意义；删除会静默清空全局配置 ⇒ 都只给解释、零请求 |
 * | 15 | 价格下方多一句「代练通最低 2 元，请改价」的运行期提示；<2 元**提交时**拦下 | 计划 §2.3 的备注 + 2026-10-27 业务方拍板：价格是客服能改的 ⇒ ⛔ 不拿 `can_push=false` 永久禁用按钮，改成提交时校验并提示 |
 * | 16 | 默认值取不到时在按钮上方显示一行中文原因（预览 / 设置）＋区服格下面显示原因 | ⛔ 不静默留空：否则客服会以为「系统就是没默认值」而不知道为什么「只改价格」提交不了 |
 * | 17 | 表单默认值改为**只用后端发单预览**（价格 / 时限 / 标题 / 账号资料 / QQ·电话均由后端给，前端不再派生） | 页面显示必须等于真正要发出去的值：前端自己派生一套随机账号会让客服照页面核对时核到假值。`can_push=false` 的 `reason` 直接显示并禁用提交 |
 * | 18 | 用户手选区服后**再取一次预览** | 「区服变了」之后后端对同一张单的判定（能否发、区服名）可能不同；保持同源。只更新区服与 `can_push`，⛔ 不覆盖用户已改的价格 / 标题等 |
 *
 * ## 陷阱（`AGENTS.md` §5-G，本项目踩过）
 *
 * - 先 `bootstrapIsland`（挂样式）再塞标记；`initIslandElements` 相反**必须**在塞标记之后。
 * - 点击收口用**事件委托**（capture），不逐颗绑：证据件一来就是几十个可点元素。
 * - 选择器与类名分开写；以 `(` 开头的语句一律前置 `;`。
 * - 页里有定时器（区服搜索防抖）：假定时器要**先** `vi.useFakeTimers()`，且每个用例自己收口
 *   （`mount` 返回的 cleanup 会清掉这个定时器）。
 */
import {
  DAILIANTONG_PLATFORM,
  fetchGamePlatformZones,
  fetchPlatformPushPreview,
  fetchPlatformZones,
  pushOrderToPlatform,
  type PlatformPushPreview,
  type PlatformZone,
  type PushToPlatformPayload,
} from '@/api/dailiantongPush'
import { fetchMe } from '@/api/auth'
import { BizError } from '@/api/errors'
import {
  listDailiantongOrderTemplates,
  type OrderTemplateRow,
} from '@/api/dailiantongExtra'
import {
  fetchPlatformIntegrationSummary,
  fetchPlatformPushSettings,
  savePlatformPushSettings,
  type PlatformIntegrationSummary,
  type PlatformPushSettings,
} from '@/api/platformIntegrations'
import { fetchArtifactHtml, clickLabel } from '@/layui/artifactPage'
import { escapeHtml } from '@/layui-island/escape'
import { canNode } from '@/layui-island/perms'
import {
  classifyPushError,
  PUSH_JDP_SENT_MESSAGE_TYPE,
  PUSH_JDP_SENT_TEXT,
} from '@/layui-island/platformPush'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  islandTabTarget,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** 岛页 key（= 文件名 = `layui-island/page.html?page=` 的取值）。 */
export const PAGE_KEY = 'order-pushjdp'
/** 证据件（`public/replica/` 下；用 `BASE_URL` 拼，子路径构建不会取错）。 */
export const ARTIFACT_URL = 'replica/order-pushjdp.html'
/** 页 `<title>`（对标服务端渲染值，证据件源 `<title>` 逐字）。 */
export const TITLE = '发到第三方交易平台2026'
/** 岛页查询参数：内部订单号（20 位）。对标是 `?orderId=`；island 外壳统一用 `order_no`（同其余岛页）。 */
export const ORDER_NO_PARAM = 'order_no'
/** 底部主按钮文案（对标原文，逐字）。 */
export const SUBMIT_TEXT = '确定发单到第三方交易平台'
/** 主按钮的 `lay-filter`（对标原文）。 */
export const SUBMIT_FILTER = 'savebtn'
/** 主按钮选择器（**唯一出处**：拦截点击、找按钮都用它）。 */
export const SUBMIT_SELECTOR = `button[lay-filter="${SUBMIT_FILTER}"]`

/** 右栏平台表的 id / elem（对标 `PushJdp2026.js` L111 `elem:'#tableList'`、id=`tableList`）。 */
export const TABLE_ID = 'tableList'
export const TABLE_ELEM = '#tableList'

// ── T9d：接真用的常量（页测试钉字面值）─────────────────────────────────────

/** 真实订单号的形状（与 `Order.order_no` 的 20 位纯数字一致；形状不对就不发请求）。 */
export const ORDER_NO_PATTERN = /^\d{20}$/

/**
 * 按钮上方那句如实说明。
 *
 * ⚠️ 必须**点名**列出「页面上能填、但本版不提交」的字段（2026-10-27 审计 P2-7）：
 * 上一版只说「其余由系统按订单自动生成」，把「同发内部抢单价」这一类可填格盖住了——
 * 用户填了却发不出去，还以为已经带上了。
 */
export const LIVE_NOTE_TEXT =
  '本版提交 价格 / 保证金 / 时限 / 区服 / 完单数与介入率门槛 / 代练要求 / 自动下架天数 / ' +
  '「当前订单说明」（发到平台的代练内容）/ 同发内部抢单价 / 发单频道 / 标签；' +
  '游戏账号 / 登录密码 / 角色名、项目分类、发单标题、玩家电话 / QQ、' +
  '「指定人或组 · 接单密码」、「延迟发平台」不提交（后两项后端尚未支持，页内有说明），' +
  '其余由系统按订单自动生成'

/** 区服**下拉**的 id / class（证据件里那格是 input，运行期换成 select）。 */
export const ZONE_SELECT_ID = 'ttZoneSelect'
export const ZONE_SELECT_SELECTOR = `#${ZONE_SELECT_ID}`
export const ZONE_SELECT_CLASS = 'tt-zone-select'
/** layui 的 `form.on('select(<filter>)')` 过滤器（= 下拉的 `lay-filter`）。 */
export const ZONE_SELECT_FILTER = 'ttZoneSelect'
/** 那一格的 label 文案（结构定位用；证据件原文「游戏/平台」）。 */
export const ZONE_LABEL_TEXT = '游戏/平台'
export const ZONE_REQUIRED_MESSAGE = '请先在「游戏/平台」里选择区服'
/** 该游戏在代练通没有可用区服 / 区服列表读取失败的提示。 */
export const ZONE_LIST_EMPTY_MESSAGE = '该游戏在代练通没有可用的区服，请稍后重试或联系管理员'
/**
 * ⭐ B3：按游戏名在字典里**匹配不到**时，页面改列**全部区服**让人**手选**。
 * 旧行为是下拉 0 个选项 ⇒ 客服永远发不出这张单（阻断级）。
 */
export const ZONE_NO_GAME_MATCH_MESSAGE =
  '该游戏名在代练通区服字典里匹配不到，已列出全部区服，请在「游戏/平台」里手动选择对应的区服'
export const ZONE_LIST_FAILED_PREFIX = '区服列表读取失败：'

/**
 * F1 / 业务方 2026-10-27：「将发到代练通的内容」只读预览。
 *
 * ⭐ P9（2026-10-27，业务方真机 bug）：这块**不再独立展示 `preview.memo`**——它就是
 * 「当前订单说明」框（`textarea[name=gaemTf]`）的**当前内容**，框里改了它跟着变。
 * 理由：提交载荷的 `memo` 取的是框里的值（用户改过就用用户的）；如果这块还显示 preview
 * 的旧值，页面就是「上面写 A、实际发 B」——正是本次要修的 bug 形态。
 */
export const PUSH_MEMO_CLASS = 'tt-push-memo'
export const PUSH_MEMO_SELECTOR = `.${PUSH_MEMO_CLASS}`
export const PUSH_MEMO_TITLE = '将发到代练通的内容（取自「当前订单说明」）：'
export const PUSH_MEMO_EMPTY_TEXT = '订单没有填写内容'
export const PUSH_MEMO_LOADING_TEXT = '正在读取订单内容…'
/** 预览读取失败时的如实说明（⛔ 不停在「正在读取」、⛔ 不谎称「订单没有填写内容」）。 */
export const PUSH_MEMO_FAILED_TEXT =
  '订单内容读取失败；发出去的内容以「当前订单说明」里填的为准'
/** preview.warnings（客服能改的提示）的 class：只提示、⛔ 不据此禁用提交。 */
export const PREVIEW_WARNINGS_CLASS = 'tt-preview-warnings'
export const PREVIEW_WARNINGS_SELECTOR = `.${PREVIEW_WARNINGS_CLASS}`

/** 结果块（运行期追加）的 class / 状态属性。 */
export const RESULT_CLASS = 'tt-push-result'
export const RESULT_SELECTOR = `.${RESULT_CLASS}`
export const RESULT_STATE_ATTR = 'data-push-state'
/** 按钮上方说明的 class。 */
export const NOTE_CLASS = 'tt-push-note'
export const NOTE_SELECTOR = `.${NOTE_CLASS}`
/** 「延迟发平台」块内那句「后端尚未支持」说明（class 定义在样式表用到它之前，G4）。 */
export const DELAY_NOTE_CLASS = 'tt-delay-note'
export const DELAY_NOTE_SELECTOR = `.${DELAY_NOTE_CLASS}`

/** 三态 / 兜底文案。 */
export const SENT_MESSAGE = '已发单到代练通'
export const CLOSE_HINT = '关闭后可在订单列表查看'
export const FAILED_PREFIX = '发单失败：'
export const UNKNOWN_STATE_MESSAGE = '已提交但未拿到平台单号，请人工核对后再操作'
export const PENDING_MESSAGE = '已提交，平台处理中，请稍后刷新核对（状态：pending）'
/** 在途文案（⛔ 不是「已提交」：此刻请求还没回来，什么都还不知道）。 */
export const IN_FLIGHT_MESSAGE = '提交中…'
/**
 * 后端回了本页没有专门分支的状态时的文案（⛔ 不许兜底成「已提交，平台处理中」）。
 *
 * ⚠️ `closed` 是**已知**状态（推送已在平台删除）⇒ 不能说成「未识别」；
 * 只有真正没见过的码才叫「未预期」（评审 M-5）。
 */
export function unexpectedStateMessage(state: string): string {
  if (state === 'closed') return '推送已关闭（closed），请核对'
  return `未预期的状态：${state}，请核对`
}
/**
 * 网络错误 / 超时 / 5xx / 非信封的文案。
 *
 * ⚠️ 写动作**不知道成没成**：我们的后端也是这么建模的（超时 ⇒ `state=unknown`，永不重试）
 * ⇒ 页面⛔ 不许把它说成「发单失败」（会诱导人重复发单），也不许说成「已提交」（会让人以为
 * 后台已经有单）。只能让人去代练通后台核对。
 */
export const REQUEST_RESULT_UNKNOWN_MESSAGE = '请求已发出但没拿到结果，请到代练通后台核对后再操作'
export const ORDER_NO_INVALID_MESSAGE = '订单号缺失或格式不正确，无法发单'
export const PRICE_REQUIRED_MESSAGE = '请填写发平台价格（最低 2 元）'
export const ACCEPT_INVALID_MESSAGE = '「要求最低完单数 / 拒绝新手和介入率」没有选中有效值，请刷新页面后重试'
export const TIME_LIMIT_INVALID_MESSAGE = '时限必须在 1–720 小时之间，请调整「时间要求」'

/** 时限上下限（后端 `PushToPlatformSerializer` 的 `min_value` / `max_value`）。 */
export const TIME_LIMIT_MIN_HOURS = 1
export const TIME_LIMIT_MAX_HOURS = 720

/** 三种结果的 class 后缀（`data-push-state` 取值，CSS 用它上色）。 */
export type PushResultState = 'pending' | 'sent' | 'failed' | 'unknown'

/**
 * 右栏平台摘要表的列（**F2，2026-10-27**：接 `platform-integrations/summary/`）。
 *
 * 与对标 `PushJdp2026.js` L111-150 的差异（如实登记，见文件头「与对标不同」#11）：
 * 对标那三列是「发往平台 / 平台发单账号 / 平台余额（点击查看）」+ 两个隐藏列 + 复选框；
 * 本版按实施计划 §2.3 的「代练通 / 平台账号 / 可用余额 / 冻结余额」渲染，⛔ 不做「点击查看」
 * （那个链接指向的 `GetBalance` 端点本节没有）⇒ 余额直接显示数字，没拿到显示 `—`。
 *
 * ⚠️ 余额来自接口（可能为 `null`）⇒ 用 `balanceCell` 统一格式化，⛔ 不伪造 `0.00`。
 */
export function balanceCell(value: string | null | undefined): string {
  const raw = String(value ?? '').trim()
  if (raw === '') return '—'
  const num = Number(raw)
  if (!Number.isFinite(num)) return escapeHtml(raw)
  return num.toFixed(2)
}

export const PLATFORM_COLS: Record<string, unknown>[] = [
  { field: 'platformName', title: '发往平台', width: 80 },
  { field: 'platformAcc', title: '平台发单账号' },
  {
    field: 'availableBalance',
    title: '可用余额',
    width: 90,
    align: 'center',
    templet: (row: { availableBalance?: string | null }) => balanceCell(row.availableBalance),
  },
  {
    field: 'frozenBalance',
    title: '冻结余额',
    width: 90,
    align: 'center',
    templet: (row: { frozenBalance?: string | null }) => balanceCell(row.frozenBalance),
  },
]

/* ═══════════════ F1–F5：一键抛单（2026-10-27 实施计划 §2.3 / §2.4）═══════════════ */

/* ── F1：默认填充 ─────────────────────────────────────────────────────────── */

/**
 * 代练通最低发单价（元，计划 §2.3 的「<2 元提示改价」）。
 *
 * 2026-10-27 业务方拍板：价格 <2 元是**客服能改**的问题 ⇒ 按钮保持可点，
 * **提交时**拦下并提示这句（⛔ 不拿 `can_push=false` 把按钮永久禁用）。
 */
export const PRICE_WARN_THRESHOLD = 2
export const PRICE_LOW_MESSAGE = '代练通最低 2 元，请改价'
/**
 * 自动下架天数的本地上界（与后端 `dailiantong_push.OFF_SELL_DAYS_MAX` 同值；
 * 证据件 `#offSellDays` 的下拉是 0..30）。⭐ I3。
 */
export const OFF_SELL_DAYS_MAX = 30
export const PRICE_HINT_CLASS = 'tt-price-hint'
export const PRICE_HINT_SELECTOR = `.${PRICE_HINT_CLASS}`

/** 发单预览读取失败时的中文前缀（⛔ 不是「暂无数据」）。 */
export const PREVIEW_FAILED_PREFIX = '发单预览读取失败：'
/** 后端回 `can_push=false` 却没给 `reason` 时的兜底文案（⛔ 不编原因）。 */
export const PREVIEW_BLOCKED_FALLBACK = '后端判定当前订单暂时不能发单，但没有返回具体原因，请稍后重试或联系管理员'
/** 默认门槛 / 模板（settings 接口）读取失败时的中文前缀。 */
export const DEFAULTS_SETTINGS_FAILED_PREFIX = '默认门槛 / 模板读取失败：'
/** 预览判定「不能发单」时那句可见原因的 class（运行期注入，紧贴提交按钮）。 */
export const PREVIEW_BLOCK_CLASS = 'tt-preview-block'
export const PREVIEW_BLOCK_SELECTOR = `.${PREVIEW_BLOCK_CLASS}`
export const DEFAULTS_WARNING_CLASS = 'tt-defaults-warning'
export const DEFAULTS_WARNING_SELECTOR = `.${DEFAULTS_WARNING_CLASS}`

/** 默认区服（F5）的两种提示：匹配不到要求手选、查询失败。 */
export const ZONE_DEFAULT_NONE_MESSAGE =
  '该游戏在代练通区服字典里匹配不到默认区服，请在「游戏/平台」里手动搜索选择'
/** 映射还在、但字典取不到区服名（后端 `source=map` 且 `server_name=""`）时的如实提示。 */
export const ZONE_DEFAULT_NAME_MISSING_MESSAGE =
  '已按上次记录的区服编码选中，但区服名暂时取不到；如需核对请在「游戏/平台」里手动搜索选择'
export const ZONE_TIP_CLASS = 'tt-zone-tip'
export const ZONE_TIP_SELECTOR = `.${ZONE_TIP_CLASS}`

/**
 * ⚠️ F1 改准：页面默认值**全部来自后端发单预览**
 * （`GET orders/<no>/platform-push/preview/`），⛔ 前端不再自己派生随机账号 / 电话 / QQ。
 * 理由：前端自算一套会让页面显示的值与后端真正发出去的值不一致（客服照页面核对=核错）。
 */

/** 只填空（用户已经打过字就不覆盖）——默认值回填的唯一口径。 */
function fillIfEmpty(root: HTMLElement, name: string, value: string): void {
  const input = root.querySelector<HTMLInputElement>(`input[name="${name}"]`)
  if (!input || value === '') return
  if (input.value.trim() !== '') return
  input.value = value
}

/**
 * 价格默认回填：只覆盖证据件的 `0` / 空（用户已经改过就不动）。
 *
 * ⚠️ 取不到订单价格时**会把证据件的 `0` 清成空串**：`0` 是证据件里对标服务端的占位值，
 * 照它提交就是发一张 0 元单（上游必拒）⇒ 宁可空着让客服手填，也⛔ 不拿占位值冒充默认价。
 */
function fillPriceIfDefault(root: HTMLElement, value: string): void {
  const input = root.querySelector<HTMLInputElement>('input[name="throwFee"]')
  if (!input) return
  const current = input.value.trim()
  if (current !== '' && current !== '0') return
  input.value = value
  // ⭐ G4：价格是「延迟发平台」块的显示条件之一，而预览是**异步**填的 ⇒ 通知监听者重算，
  // 否则「预览带出 12.50 + 客服填了内部抢单价」时那一块会一直不显示。
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

/** 只在「用户没改过」时填（默认值是证据件里的 `0` / `0.00`）——回填的双保险。 */
function fillIfDefault(root: HTMLElement, name: string, value: string, defaults: string[]): void {
  const input = root.querySelector<HTMLInputElement>(`input[name="${name}"]`)
  if (!input || value === '') return
  const current = input.value.trim()
  if (!defaults.includes(current)) return
  input.value = value
}

/* ── F1：价格提示（<2 元提示改价，⛔ 不拦提交）───────────────────────────── */

export function mountPriceHint(root: HTMLElement): { update: () => void; cleanup: () => void } {
  const input = root.querySelector<HTMLInputElement>('input[name="throwFee"]')
  if (!input) return { update: () => undefined, cleanup: () => undefined }
  const holder = input.parentElement
  const hint = document.createElement('div')
  hint.className = PRICE_HINT_CLASS
  hint.style.display = 'none'
  if (holder) holder.appendChild(hint)
  const update = (): void => {
    const raw = input.value.trim()
    const num = Number(raw)
    // 空 / 证据件的占位 `0` = 还没定价 ⇒ 提示填写（⛔ 不把 0 当默认价）。
    if (raw === '' || raw === '0') {
      hint.textContent = PRICE_REQUIRED_MESSAGE
      hint.style.display = ''
      return
    }
    const low = Number.isFinite(num) && num < PRICE_WARN_THRESHOLD
    hint.textContent = low ? PRICE_LOW_MESSAGE : ''
    hint.style.display = low ? '' : 'none'
  }
  input.addEventListener('input', update)
  update()
  return {
    update,
    cleanup: () => {
      input.removeEventListener('input', update)
      hint.remove()
    },
  }
}

/* ── F2：右面板（platform-integrations/summary/）────────────────────────── */

export const SUMMARY_NOTE_CLASS = 'tt-platform-summary'
export const SUMMARY_NOTE_SELECTOR = `.${SUMMARY_NOTE_CLASS}`
export const SUMMARY_BOX_SELECTOR = '.designateBox'
export const SUMMARY_LOADING_MESSAGE = '正在读取平台账户…'
/** 后端按 200 回了 `error` 但没给账号时的兜底（⛔ 不是「暂无数据」）。 */
export const SUMMARY_NO_CONFIG_MESSAGE =
  '还没有可用的代练通账号：请到「系统 → 发平台设置 → 交易平台对接设置」新增并启用一个代练通账号'
/** 请求被拒（BizError = 业务前提不满足 / 上游失败）时的前缀。 */
export const SUMMARY_UNAVAILABLE_PREFIX = '平台账户不可用：'
/** 网络 / 超时 / 5xx 时的前缀。 */
export const SUMMARY_FAILED_PREFIX = '平台账户查询失败：'

export type SummaryPanelState = 'ok' | 'no_config' | 'failed'

/** 成功载荷里该显示「原因」还是「数据」：空串 = 正常。 */
export function summaryReason(summary: PlatformIntegrationSummary): string {
  const error = String(summary.error ?? '').trim()
  if (error !== '') return error
  // 后端 `configured=false` 时一定带 `error`；这里兜的是「接口少回了 error」的畸形载荷。
  if (!summary.configured) return SUMMARY_NO_CONFIG_MESSAGE
  return ''
}

/** 面板三态（F7）：正常 / 没配账号 / 查询失败。判据只用后端回的 `configured` + `error`。 */
export function summaryPanelState(summary: PlatformIntegrationSummary): SummaryPanelState {
  if (summaryReason(summary) === '') return 'ok'
  return summary.configured ? 'failed' : 'no_config'
}

/** 请求被拒 / 网络失败 → 中文原因（⛔ 不出现「暂无数据」）。 */
export function summaryErrorReason(error: unknown): string {
  const message = errorMessage(error)
  if (error instanceof BizError) return `${SUMMARY_UNAVAILABLE_PREFIX}${message}`
  return `${SUMMARY_FAILED_PREFIX}${message}`
}

/**
 * 右面板：成功渲染一行（平台 / 账号 / 可用余额 / 冻结余额）；没配置或查询失败显示中文原因。
 *
 * ⚠️ 初始就把表格藏起来 + 显示「正在读取平台账户…」——layui 空表默认会渲染「暂无数据」，
 * 计划明确 ⛔ 不显示那句。
 */
export function mountSummaryPanel(
  root: HTMLElement,
  table: LayuiModule,
  _layer: LayuiModule,
): { load: () => void; cleanup: () => void } {
  let disposed = false
  const box = root.querySelector<HTMLElement>(SUMMARY_BOX_SELECTOR)
  const note = document.createElement('div')
  note.className = SUMMARY_NOTE_CLASS
  note.setAttribute('role', 'status')
  if (box?.parentElement) box.parentElement.insertBefore(note, box)
  else root.appendChild(note)

  const showNote = (text: string): void => {
    note.textContent = text
    note.style.display = ''
    if (box) box.style.display = 'none'
  }
  const showRow = (summary: PlatformIntegrationSummary): void => {
    note.textContent = ''
    note.style.display = 'none'
    if (box) box.style.display = ''
    ;(table.reloadData as (id: string, options: unknown) => void)(TABLE_ID, {
      data: [
        {
          platformName: (summary.platform_label ?? '').trim() || '—',
          platformAcc: (summary.account ?? '').trim(),
          availableBalance: summary.available_balance,
          frozenBalance: summary.frozen_balance,
        },
      ],
    })
  }

  showNote(SUMMARY_LOADING_MESSAGE)

  const load = (): void => {
    void fetchPlatformIntegrationSummary().then(
      (summary) => {
        if (disposed) return
        const reason = summaryReason(summary)
        if (reason === '') showRow(summary)
        else showNote(reason)
      },
      (error: unknown) => {
        if (disposed) return
        showNote(summaryErrorReason(error))
      },
    )
  }

  return {
    load,
    cleanup: () => {
      disposed = true
      note.remove()
    },
  }
}

/* ── F3：门槛两格（完单数 / 介入率）+「保存介入率和完单数设置」────────────── */

export const ACCEPT_SAVE_SELECTOR = '.saveAcceptSettingBtn'
export const ACCEPT_POWER_SELECT = 'select[name="dlmmlimitAccept"]'
export const ACCEPT_LIMIT_SELECT = 'select[name="limitAccept"]'
export const SETTINGS_NOT_READY_MESSAGE = '设置还没读取完成，请稍后再试（或刷新页面）'
/** F3 的验收文案：保存成功必须是这四个字。 */
export const SAVE_SUCCESS_TEXT = '保存成功'

/** 门槛 / 模板共用的「设置是否已经读到真值」闸（未读到 ⇒ ⛔ 不许提交，防默认值覆盖真配置）。 */
export type SettingsGate = { loaded: boolean }

/** 后端值 → 下拉该选哪一项（`null` 的介入率 = 不设置 = `value="0"`）。 */
export function powerLevelOption(settings: PlatformPushSettings): string {
  return String(settings.default_power_level)
}

export function limitAcceptOption(
  settings: Pick<PlatformPushSettings, 'default_limit_accept'>,
): string {
  return settings.default_limit_accept === null ? '0' : String(settings.default_limit_accept)
}

export type AcceptSettings = {
  apply: (settings: PlatformPushSettings) => void
  handleClick: (target: EventTarget | null) => boolean
  cleanup: () => void
}

/**
 * F3：两个门槛下拉的默认值 + 那颗「保存介入率和完单数设置」。
 *
 * ⚠️ 保存发的是 **部分设置**（`PUT …/settings/`，后端 `partial=True`）：只带
 * `default_power_level` / `default_limit_accept` 两个键，⛔ 不动另外 9 组。
 * 计划正文写的是「PATCH settings」——本仓该端点只开 `GET` / `PUT`（`partial=True`），
 * 用 PUT 发部分键语义等价，且不会 405（缺口已写进交付报告）。
 */
export function mountAcceptSettings(
  root: HTMLElement,
  form: LayuiModule,
  layer: LayuiModule,
  gate: SettingsGate,
): AcceptSettings {
  const powerSelect = root.querySelector<HTMLSelectElement>(ACCEPT_POWER_SELECT)
  const limitSelect = root.querySelector<HTMLSelectElement>(ACCEPT_LIMIT_SELECT)
  let saving = false

  const apply = (settings: PlatformPushSettings): void => {
    if (limitSelect) limitSelect.value = limitAcceptOption(settings)
    if (powerSelect) powerSelect.value = powerLevelOption(settings)
    ;(form.render as (type?: string) => void)('select')
  }

  const saveButton = root.querySelector<HTMLElement>(ACCEPT_SAVE_SELECTOR)
  const setBusy = (busy: boolean): void => {
    if (!saveButton) return
    saveButton.classList.toggle('layui-btn-disabled', busy)
    if (busy) saveButton.setAttribute('aria-disabled', 'true')
    else saveButton.removeAttribute('aria-disabled')
  }

  function save(): void {
    if (saving) return
    if (!gate.loaded) {
      islandMsg(layer, SETTINGS_NOT_READY_MESSAGE)
      return
    }
    const powerRaw = powerSelect?.value ?? ''
    const limitRaw = limitSelect?.value ?? '0'
    const power = Number(powerRaw)
    const limit = Number(limitRaw)
    if (powerRaw === '' || !Number.isInteger(power) || power < 0) {
      islandMsg(layer, '「要求最低完单数」没有选中有效值，请刷新页面后重试')
      return
    }
    if (limitRaw === '' || !Number.isInteger(limit) || limit < 0) {
      islandMsg(layer, '「拒绝新手和介入率」没有选中有效值，请刷新页面后重试')
      return
    }
    saving = true
    setBusy(true)
    const finish = (): void => {
      saving = false
      setBusy(false)
    }
    void savePlatformPushSettings({
      default_power_level: power,
      // `0` = 不设置 ⇒ 发 `null`（后端列默认 `null`；⛔ 不发一个「0%」的假阈值）。
      default_limit_accept: limit === 0 ? null : limit,
    }).then(
      (saved) => {
        finish()
        apply(saved)
        islandMsg(layer, SAVE_SUCCESS_TEXT)
      },
      (error: unknown) => {
        finish()
        // 失败显示后端原文（`request()` 抛出的 `message` 就是 msg）。
        islandMsg(layer, errorMessage(error))
      },
    )
  }

  return {
    apply,
    handleClick: (target: EventTarget | null): boolean => {
      const el = target as HTMLElement | null
      if (!el || typeof el.closest !== 'function') return false
      if (!el.closest(ACCEPT_SAVE_SELECTOR)) return false
      save()
      return true
    },
    cleanup: () => {
      saving = false
    },
  }
}

/* ── F4：代练要求单模板（require_template，保存即全局生效）────────────────── */

export const TEMPLATE_PANEL_CLASS = 'newTempDiv'
export const TEMPLATE_PANEL_SELECTOR = `.${TEMPLATE_PANEL_CLASS}`
export const TEMPLATE_CONTENT_NAME = 'template_content'
export const TEMPLATE_TITLE_NAME = 'template_title'
export const TEMPLATE_SAVE_SELECTOR = '.saveTempBtn'
export const NEW_TEMPLATE_SELECTOR = 'a.newtemp'
export const EDIT_TEMPLATE_SELECTOR = 'a.edittemp'
export const DEFAULT_TEMPLATE_SELECTOR = 'a.defualttemp'
export const DELETE_TEMPLATE_SELECTOR = 'a.deltemp'
export const TEMPLATE_DIALOG_TITLE = '编辑代练/陪玩要求模板（全局只有一份）'
export const TEMPLATE_DIALOG_AREA: [string, string] = ['640px', '460px']
export const TEMPLATE_SAVE_SUCCESS_TEXT = '保存成功，之后发单会默认带上这份代练/陪玩要求'
export const TEMPLATE_NOT_READY_MESSAGE = '模板还没读取完成，请稍后再试（或刷新页面）'
export const TEMPLATE_PANEL_MISSING_MESSAGE = '模板弹层内容缺失，请联系管理员'
/** 「设为默认模板」/「删除选中的模板」是多模板语义 ⇒ 本版只有一份，给解释、零请求。 */
export const TEMPLATE_SET_DEFAULT_MESSAGE =
  '本版只有一份全局模板，编辑并保存后即全局生效，无需单独设为默认'
export const TEMPLATE_DELETE_MESSAGE =
  '本版只有一份全局模板，如需清空请在编辑弹窗里删掉内容后保存'

export type RequireTemplate = {
  apply: (settings: PlatformPushSettings) => void
  handleClick: (target: EventTarget | null) => boolean
  cleanup: () => void
}

/**
 * F4：把证据件里那套「多模板」控件收成**一份全局模板**。
 *
 * - 「新建模板」/「编辑选中的模板」→ 打开证据件自带的模板弹层（**模板标题那行隐藏**，
 *   因为 `require_template` 只有正文，没有标题）；
 * - 「保存模板」→ `PUT …/settings/` 只带 `require_template`；保存后写回本页的
 *   「代练/陪玩要求」（仅当用户没改过那一格）；
 * - 「设为默认模板」/「删除选中的模板」→ 解释「只有一份」并**零请求**（⛔ 不做多模板）。
 */
export function mountRequireTemplate(
  root: HTMLElement,
  layui: LayuiNamespace,
  layer: LayuiModule,
  gate: SettingsGate,
): RequireTemplate {
  let currentTemplate = ''
  let layerIndex: number | null = null
  let saving = false

  const panel = root.querySelector<HTMLElement>(TEMPLATE_PANEL_SELECTOR)
  const host = root.querySelector<HTMLElement>('form.layui-form') ?? root
  const content = panel?.querySelector<HTMLTextAreaElement>(
    `textarea[name="${TEMPLATE_CONTENT_NAME}"]`,
  )
  const contentField = root.querySelector<HTMLTextAreaElement>('textarea[name="requestContent"]')
  // 「模板标题」在多模板里才有意义（本版只存正文）⇒ 隐藏那一行（标记不删，可回退）。
  const titleInput = panel?.querySelector<HTMLInputElement>(`input[name="${TEMPLATE_TITLE_NAME}"]`)
  const titleItem = titleInput?.closest<HTMLElement>('.layui-form-item') ?? null
  if (titleItem) titleItem.style.display = 'none'

  const apply = (settings: PlatformPushSettings): void => {
    currentTemplate = settings.require_template ?? ''
    if (contentField && contentField.value.trim() === '' && currentTemplate !== '') {
      contentField.value = currentTemplate
    }
  }

  function closeDialog(): void {
    if (layerIndex === null) return
    ;(layer.close as (index: number) => void)(layerIndex)
    layerIndex = null
  }

  function openDialog(): void {
    if (layerIndex !== null) return
    if (!panel || !content) {
      islandMsg(layer, TEMPLATE_PANEL_MISSING_MESSAGE)
      return
    }
    // 单模板：打开时永远是那一份全局模板（「新建」也不会另开一份）。
    content.value = currentTemplate
    const jq = (layui as unknown as { $: (el: Element) => unknown }).$(panel)
    layerIndex = (layer.open as (options: unknown) => number)({
      type: 1,
      title: TEMPLATE_DIALOG_TITLE,
      content: jq,
      area: TEMPLATE_DIALOG_AREA,
      shade: [0.3, '#333'],
      shadeClose: false,
      skin: 'layui-layer-lan',
      anim: 8,
      end: () => {
        layerIndex = null
        if (panel.parentElement !== host) host.appendChild(panel)
        panel.style.display = 'none'
      },
    })
  }

  function save(): void {
    if (saving || !panel || !content) return
    if (!gate.loaded) {
      islandMsg(layer, TEMPLATE_NOT_READY_MESSAGE)
      return
    }
    const next = content.value
    saving = true
    const saveButton = panel.querySelector<HTMLElement>(TEMPLATE_SAVE_SELECTOR)
    if (saveButton) {
      saveButton.classList.add('layui-btn-disabled')
      saveButton.setAttribute('aria-disabled', 'true')
    }
    const finish = (): void => {
      saving = false
      if (saveButton) {
        saveButton.classList.remove('layui-btn-disabled')
        saveButton.removeAttribute('aria-disabled')
      }
    }
    const previous = currentTemplate
    void savePlatformPushSettings({ require_template: next }).then(
      (saved) => {
        finish()
        currentTemplate = saved.require_template ?? next
        // 只覆盖「没被用户改过」的那一格（用户按本单改过的内容不能被模板冲掉）。
        if (contentField && (contentField.value.trim() === '' || contentField.value === previous)) {
          contentField.value = currentTemplate
        }
        closeDialog()
        islandMsg(layer, TEMPLATE_SAVE_SUCCESS_TEXT)
      },
      (error: unknown) => {
        finish()
        islandMsg(layer, errorMessage(error))
      },
    )
  }

  const onPanelClick = (event: Event): void => {
    const target = event.target as HTMLElement | null
    if (!target?.closest?.(TEMPLATE_SAVE_SELECTOR)) return
    event.preventDefault()
    save()
  }
  panel?.addEventListener('click', onPanelClick)

  return {
    apply,
    handleClick: (target: EventTarget | null): boolean => {
      const el = target as HTMLElement | null
      if (!el || typeof el.closest !== 'function') return false
      if (el.closest(NEW_TEMPLATE_SELECTOR) || el.closest(EDIT_TEMPLATE_SELECTOR)) {
        openDialog()
        return true
      }
      if (el.closest(DEFAULT_TEMPLATE_SELECTOR)) {
        islandMsg(layer, TEMPLATE_SET_DEFAULT_MESSAGE)
        return true
      }
      if (el.closest(DELETE_TEMPLATE_SELECTOR)) {
        islandMsg(layer, TEMPLATE_DELETE_MESSAGE)
        return true
      }
      return false
    },
    cleanup: () => {
      closeDialog()
      panel?.removeEventListener('click', onPanelClick)
    },
  }
}

/* ── 「常用模板」下拉（#tempList）：代练通侧订单要求模板（2026-10-30 接真）────── */

export const TEMPLATE_LIST_SELECTOR = '#tempList'
/** layui 事件过滤值（证据件 `lay-filter="tempList"`；`form.on('select(tempList)')` 认它）。 */
export const TEMPLATE_LIST_FILTER = 'tempList'
/** 「不需要」那一项的 value（证据件原样；选中它 = 不填模板）。 */
export const TEMPLATE_LIST_EMPTY_VALUE = '0'
/** 默认模板的标题前缀（对标 `pushjdp.js:224`：`isdef=="Y"` ⇒ `[默认]`）。 */
export const TEMPLATE_LIST_DEFAULT_PREFIX = '[默认]'
export const TEMPLATE_LIST_FAILED_PREFIX = '常用模板读取失败：'

export type TemplateList = {
  cleanup: () => void
}

/**
 * 「可以选择常用模板」那个下拉（证据件 `#tempList`）**接真**。
 *
 * - 数据源 = 代练通**上游侧**订单要求模板（`GET /api/business/platform-integrations/order-templates/`，
 *   M5「代练通订单要求模板」管理页同一份库）——⚠️ 与 F4 那份本地全局模板（`require_template`）
 *   是**两回事**：这份在上游代练通账号上（每账号最多 10 条），那份只管「空正文时的默认值」。
 * - 选中一条 ⇒ 该模板正文填进「代练/陪玩要求」框（对标语义：**覆盖**，`pushjdp.js:277` 最后
 *   一次 `.val()` 是无条件覆盖）；选回「不需要」⇒ 不动框里已有的内容。
 * - 对标的「`isdef=Y` 默认模板预填空正文」（`pushjdp.js:228-233`）**有意不做**：空正文的
 *   预填已有 F4 全局模板一个来源，再加一个会互相打架。
 * - 读取失败 ⇒ `layer.msg` 给中文原因（照对标 `pushjdp.js:219` 的失败提示位）、下拉保持
 *   「不需要」——⛔ 不静默装作「没有配置模板」（2026-10-30 业务方报障正是这条被当成没配置）。
 */
export function mountTemplateList(
  root: HTMLElement,
  form: LayuiModule,
  layer: LayuiModule,
): TemplateList {
  const listSelect = root.querySelector<HTMLSelectElement>(TEMPLATE_LIST_SELECTOR)
  const contentField = root.querySelector<HTMLTextAreaElement>(
    'textarea[name="requestContent"]',
  )
  let templates: OrderTemplateRow[] = []

  const onSelect = (data: unknown): void => {
    const value = String((data as { value?: unknown } | null)?.value ?? '')
    if (!listSelect || value === '' || value === TEMPLATE_LIST_EMPTY_VALUE) return
    const hit = templates.find((item) => String(item.id) === value)
    if (!hit || contentField === null) return
    contentField.value = hit.temp
  }
  ;(
    form.on as (
      event: string,
      handler: (data: unknown) => void,
    ) => unknown
  )(`select(${TEMPLATE_LIST_FILTER})`, onSelect)

  void listDailiantongOrderTemplates().then(
    (list) => {
      templates = list.items
      if (!listSelect) return
      for (const item of list.items) {
        if (item.id === null) continue
        const option = document.createElement('option')
        option.value = String(item.id)
        option.textContent = `${item.is_default ? TEMPLATE_LIST_DEFAULT_PREFIX : ''}${item.title}`
        listSelect.appendChild(option)
      }
      ;(form.render as (type?: string) => void)('select')
    },
    (error: unknown) => {
      islandMsg(layer, `${TEMPLATE_LIST_FAILED_PREFIX}${errorMessage(error)}`)
    },
  )

  return {
    // `form.on` 没有 off；本页每次打开都是全新 iframe 文档 ⇒ 无需（也无法）反注册。
    cleanup: () => {},
  }
}

/** 运行期注入的样式（无对标证据的自有控件；⛔ 证据件不动）。 */
export const LIVE_STYLE_ID = 'order-pushjdp-live-style'
const LIVE_STYLE = `
.${NOTE_CLASS}{margin:0 0 8px;color:#ff6a00;font-size:12px;line-height:20px}
.${RESULT_CLASS}{margin:0 0 8px;padding:8px 12px;border:1px solid transparent;border-radius:2px;font-size:12px;line-height:20px}
.${RESULT_CLASS}[${RESULT_STATE_ATTR}="sent"]{color:#009688;border-color:#009688;background:#f0faf8}
.${RESULT_CLASS}[${RESULT_STATE_ATTR}="failed"]{color:#e03636;border-color:#e03636;background:#fff2f0}
.${RESULT_CLASS}[${RESULT_STATE_ATTR}="unknown"]{color:#d4380d;border-color:#ff6a00;background:#fff7e6;font-weight:700}
.${RESULT_CLASS}[${RESULT_STATE_ATTR}="pending"]{color:#666;border-color:#d2d2d2;background:#fafafa}
.${ZONE_SELECT_CLASS}{width:100%;height:38px;line-height:38px;padding:0 8px;border:1px solid #e2e2e2;border-radius:2px;background:#fff;font-size:12px;color:#333}
.${ZONE_TIP_CLASS}{margin-top:4px;color:#ff6a00;font-size:12px;line-height:18px}
.${PRICE_HINT_CLASS}{margin-top:4px;color:#ff6a00;font-size:12px;line-height:18px}
.${DEFAULTS_WARNING_CLASS}{margin:0 0 8px;padding:6px 10px;color:#d4380d;background:#fff7e6;border:1px solid #ffd591;border-radius:2px;font-size:12px;line-height:20px}
.${SUMMARY_NOTE_CLASS}{padding:8px 10px;color:#d4380d;background:#fff7e6;border:1px solid #ffd591;border-radius:2px;font-size:12px;line-height:20px}
.${PREVIEW_BLOCK_CLASS}{margin:0 0 8px;padding:8px 10px;color:#cf1322;background:#fff1f0;border:1px solid #ffa39e;border-radius:2px;font-size:12px;line-height:20px;font-weight:600}
.${PUSH_MEMO_CLASS}{margin:0 0 8px;padding:8px 10px;border:1px solid #e2e2e2;background:#fafafa;font-size:12px;line-height:20px}
.${PUSH_MEMO_CLASS} .tt-push-memo-title{font-weight:600;color:#333;margin-bottom:4px}
.${PUSH_MEMO_CLASS} .tt-push-memo-body{white-space:pre-wrap;word-break:break-all;color:#555;max-height:160px;overflow-y:auto}
.${PREVIEW_WARNINGS_CLASS}{margin:0 0 8px;padding:6px 10px;color:#d4380d;background:#fff7e6;border:1px solid #ffd591;border-radius:2px;font-size:12px;line-height:20px;white-space:pre-line}
.${DELAY_NOTE_CLASS}{margin-top:6px;color:#d4380d;font-size:12px;line-height:18px}
`

/**
 * 「尚未开放」总闸认的可点控件选择器（**唯一出处**：判定与取名字都用它，⛔ 不手抄第二份）。
 *
 * ⚠️ **必须是 `.layui-btn` 而不是 `a.layui-btn`**：证据件里「生成虚拟电话」/「保存上号联系方式」/
 * 「保存介入率和完单数设置」三颗是对标的 **`<div class="layui-btn …">`**（不是 `<a>`）——
 * 只认 `a.layui-btn` 会让它们点了**完全没反应**（2026-10-26 评审核对抓到的真 bug）。
 * `[lay-filter]` 同时会命中 `<select lay-filter=…>`，由 `isNotOpenTarget` 里的表单控件排除项兜住。
 */
export const NOT_OPEN_SELECTOR =
  'button, .layui-btn, .layui-form-switch, [lay-event], [lay-filter]'

/**
 * 一次点击要不要被「尚未开放」总闸接管。
 *
 * 只认**可点控件**：`button` / `.layui-btn`（含 `div` 形态）/ layui 开关 / `[lay-event]` / `[lay-filter]`。
 * ⛔ 表单控件（`select` / `input` / `textarea`）与 layui 渲染出来的下拉 / 单选 / 复选件
 * 不接管——否则「点一下发单频道下拉」也会弹提示、选择器打不开（`pushChannel` / `tempList`
 * 两个 `<select>` 自带 `lay-filter`，会被裸 `[lay-filter]` 误伤）。
 *
 * ⚠️ T9d：**主按钮不再走这里**——它由 `mountLiveSubmit` 的 `handleClick` 先截走（真提交）。
 */
export function isNotOpenTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  if (!el || typeof el.closest !== 'function') return false
  if (islandTabTarget(target) !== null) return false
  const hit = el.closest<HTMLElement>(NOT_OPEN_SELECTOR)
  if (!hit) return false
  if (hit.matches('select, input, textarea, option')) return false
  if (hit.hasAttribute('disabled')) return false
  return true
}

/**
 * 读「要求最低完单数 / 拒绝新手和介入率」两格 → 发单载荷里的两个键（F3 的「本单可改」）。
 *
 * - 完单数：选中 `0`（不设置）⇒ 发 `0`（上游语义 = 不限制）；
 * - 介入率：选中 `0`（不设置）⇒ 发 **`null`**（⛔ 不是 `0`——`0%` 是「拒绝任何有过介入的人」，
 *   与「不设置」是两件事，后端序列化器的注释是权威）。
 *
 * 返回 `null` = 页面上这两格没有合法选中值（正常不会发生）⇒ 调用方拦下、零请求。
 */
export function readAcceptPayload(
  root: HTMLElement,
): { power_level: number; limit_accept: number | null } | null {
  const powerRaw = root.querySelector<HTMLSelectElement>(ACCEPT_POWER_SELECT)?.value ?? ''
  const limitRaw = root.querySelector<HTMLSelectElement>(ACCEPT_LIMIT_SELECT)?.value ?? ''
  const power = Number(powerRaw)
  const limit = Number(limitRaw)
  if (powerRaw === '' || !Number.isInteger(power) || power < 0) return null
  if (limitRaw === '' || !Number.isInteger(limit) || limit < 0) return null
  return { power_level: power, limit_accept: limit === 0 ? null : limit }
}

/** 把一条错误（BizError / HttpError / NetworkError / 其它）变成人话。 */
function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim() !== '') return error.message.trim()
  return '未知错误，请稍后重试'
}

/** 网络 / 5xx 那种「不知道成没成」的结果文案（保留后端/传输层的原始消息）。 */
function requestUnknownText(detail: string): string {
  const reason = detail.trim()
  return reason === '' ? REQUEST_RESULT_UNKNOWN_MESSAGE : `${REQUEST_RESULT_UNKNOWN_MESSAGE}（${reason}）`
}

/**
 * 门槛两格是否被用户**手动改过**（`mountPushPreview` 的 change 监听会打 `data-touched="1"`）。
 *
 * ⚠️ 为什么需要：默认设置读失败时页面不该把证据件里的 5 / 0 当真实值提交（I1），
 * 但用户明确选过的值**必须**发得出去。
 */
function acceptTouched(root: HTMLElement): boolean {
  const power = root.querySelector<HTMLSelectElement>(ACCEPT_POWER_SELECT)
  const limit = root.querySelector<HTMLSelectElement>(ACCEPT_LIMIT_SELECT)
  return power?.dataset.touched === '1' || limit?.dataset.touched === '1'
}

/** 读一个文本输入框的当前值（去首尾空白；找不到 = 空串）。 */
function readInputValue(root: HTMLElement, name: string): string {
  return root.querySelector<HTMLInputElement>(`input[name="${name}"]`)?.value.trim() ?? ''
}

/**
 * 读一个多行文本域（`textarea`）的当前值；找不到 = 空串。
 *
 * ⭐ I3：发单页的「代练/陪练要求」是 `textarea[name="requestContent"]`，
 * 之前**只预填不提交** ⇒ 客服改了要求也发不出去。
 */
function readTextareaValue(root: HTMLElement, name: string): string {
  return root.querySelector<HTMLTextAreaElement>(`textarea[name="${name}"]`)?.value.trim() ?? ''
}

/**
 * 读「自动下架天数」下拉；`0`（不设置）/ 找不到 / 非整数 ⇒ `null`（**不上送这个键**）。
 *
 * ⭐ I3：证据件里那一格是 `select[name="offSellDays"]`（选项 0..30），
 * 之前页面完全不读它 ⇒ 客服选了也不生效。上游 `OffSellDays` 的默认是 7 天，
 * 所以「不设置」的正确形状是**不上送**（⛔ 不发 0）。
 */
export function readOffSellDays(root: HTMLElement): number | null {
  const el = root.querySelector<HTMLSelectElement>('select[name="offSellDays"]')
  if (!el) return null
  const value = Number.parseInt(el.value, 10)
  if (!Number.isFinite(value) || value <= 0 || value > OFF_SELL_DAYS_MAX) return null
  return value
}

/**
 * 从「时间要求」两个下拉算 `time_limit_hours`。
 *
 * ⚠️ **这是推断的映射**（证据件里只有 `day` + `hours` 两个下拉、后端只收总小时数）：
 * 默认 30 天 + 0 小时 = 720，正好是后端上限。返回 `null` 时页面拦下、不发请求。
 */
export function readTimeLimitHours(root: HTMLElement): number | null {
  const dayEl = root.querySelector<HTMLSelectElement>('select[name="day"]')
  const hoursEl = root.querySelector<HTMLSelectElement>('select[name="hours"]')
  if (!dayEl || !hoursEl) return null
  const day = Number.parseInt(dayEl.value, 10)
  const hours = Number.parseInt(hoursEl.value, 10)
  if (!Number.isFinite(day) || !Number.isFinite(hours)) return null
  const total = day * 24 + hours
  if (total < TIME_LIMIT_MIN_HOURS || total > TIME_LIMIT_MAX_HOURS) return null
  return total
}

/* ── G4（2026-10-28）：延迟发平台 / 发单频道 / 指定人·组 ─────────────────── */

/** 「延迟发平台」块（证据件 `.delayDiv`，对标 `PJ-js:800-818` 的 `delayDivShow`）。 */
export const DELAY_DIV_SELECTOR = '.delayDiv'
export const DELAY_HALL_NAME = 'throwFeeHome'
export const DELAY_RADIO_NAME = 'isDelay'
/**
 * 「后端尚未支持」的常驻说明（放在块里）。`DELAY_NOTE_CLASS` 定义在样式表用到它之前。
 * ⚠️ 本仓 `PushToPlatformSerializer` **没有** `isDelay` / `delaySec`，服务层也没有排期机制
 *    ⇒ 选「是」不可能延迟；这句话必须让人在勾选之前就看到。
 */
export const DELAY_UNSUPPORTED_NOTE =
  '⚠️ 本版后端尚未支持「延迟发平台」：选「是」不能提交（不会延迟，订单仍会立即发到交易平台）'
/** 选「是」后提交被拦下时的文案（⛔ 不拿「立即发平台」冒充「已延迟」）。 */
export const DELAY_UNSUPPORTED_MESSAGE =
  '「延迟发平台」后端尚未支持：本版选「是」不会延迟，订单仍会立即发到交易平台。' +
  '请改选「否」后提交，或到代练通后台手动延后'

/** 发单频道（证据件 `<select name="pushChannel" lay-filter="pushChannel">`）。 */
export const GROUPS_CHANNEL_SELECT = 'select[name="pushChannel"]'
/** layui 的 `form.on('select(<filter>)')` 过滤器（证据件原文）。 */
export const GROUPS_CHANNEL_FILTER = 'pushChannel'
/** 「指定人或组 / 接单密码」那一格 + 它的 label + 历史下拉（逐项对应证据件的一个节点）。 */
export const UGDIV_SELECTOR = '.ugdiv'
export const UGTEXT_NAME = 'ugtext'
export const UG_LABEL_SELECTOR = '.hcolor_Channel'
export const UG_HISTORY_SELECTOR = '.seleurtext'
export const UG_DEFAULT_LABEL = '指定人或组'
export const UG_PASSWORD_LABEL = '指定接单密码'
/**
 * 历史下拉的空态（⛔ 零请求：后端没有 `GetPushPlatfromAssisUserLog` / `Del…` 的对应端点）。
 * 对标有「点一条填入 / 点 X 删除」两个动作，本版都不做，所以这句要把「缺什么」说清。
 */
export const UG_HISTORY_EMPTY_TEXT =
  '「指定人或组」历史记录尚未开放：本版没有历史记录接口，也不能删除记录'
/** 本版能真发到代练通的频道（`PUBLIC` / `GOODCHANNEL` / `CERTCHANNEL`）。 */
export const PUSH_CHANNEL_SUPPORTED: readonly string[] = [
  'PUBLIC',
  'GOODCHANNEL',
  'CERTCHANNEL',
]
/** 需要「指定对象」才能成立的频道：后端没有那个字段 ⇒ 明确拒（⛔ 静默按公共频道发）。 */
export const PUSH_CHANNEL_NEEDS_TARGET: readonly string[] = ['USER', 'GROUP', 'RecPwd']
/** 本版只发代练通 ⇒ 这些频道的统一前缀。 */
export const PUSH_CHANNEL_UNSUPPORTED_PREFIX = '本版只发代练通，'
/** 找不到发单频道下拉时的兜底（正常不会发生）。 */
export const PUSH_CHANNEL_MISSING_MESSAGE = '找不到「发单频道」，请刷新页面后重试'

/** 「发平台价 >0 且 同发内部抢单价 >0」才显示延迟块（对标 `delayDivShow` 的两个条件）。 */
export function shouldShowDelayBlock(price: string, hallPrice: string): boolean {
  const pushPrice = Number.parseFloat(price)
  const internalPrice = Number.parseFloat(hallPrice)
  return Number.isFinite(pushPrice) && pushPrice > 0 && Number.isFinite(internalPrice) && internalPrice > 0
}

/** 「延迟发平台」当前选的是不是「是」。 */
export function delayEnabled(root: HTMLElement): boolean {
  const checked = root.querySelector<HTMLInputElement>(
    `input[name="${DELAY_RADIO_NAME}"]:checked`,
  )
  return (checked?.value ?? 'N') === 'Y'
}

/**
 * 挂「延迟发平台」块：照对标显示 / 隐藏 + 常驻「后端尚未支持」说明。
 *
 * ⚠️ 只在提交时**拦下**（`delayEnabled()`），不把控件置灰：标§5 要求「照对标渲染 +
 * 点了给人话」，而这里连「点了会生效」都不是 ⇒ 必须说清它不生效。
 */
export function mountDelayBlock(root: HTMLElement): { cleanup: () => void } {
  const block = root.querySelector<HTMLElement>(DELAY_DIV_SELECTOR)
  const priceInput = root.querySelector<HTMLInputElement>('input[name="throwFee"]')
  const hallInput = root.querySelector<HTMLInputElement>(`input[name="${DELAY_HALL_NAME}"]`)
  if (!block) return { cleanup: () => undefined }

  const note = document.createElement('div')
  note.className = DELAY_NOTE_CLASS
  note.setAttribute('role', 'note')
  note.textContent = DELAY_UNSUPPORTED_NOTE
  block.appendChild(note)

  const refresh = (): void => {
    const visible = shouldShowDelayBlock(priceInput?.value ?? '', hallInput?.value ?? '')
    block.style.display = visible ? '' : 'none'
  }
  // 对标绑的是 `change`；浏览器里逐字输入更早冒 `input` ⇒ 两个都接。
  priceInput?.addEventListener('input', refresh)
  priceInput?.addEventListener('change', refresh)
  hallInput?.addEventListener('input', refresh)
  hallInput?.addEventListener('change', refresh)
  refresh()

  return {
    cleanup: () => {
      priceInput?.removeEventListener('input', refresh)
      priceInput?.removeEventListener('change', refresh)
      hallInput?.removeEventListener('input', refresh)
      hallInput?.removeEventListener('change', refresh)
      note.remove()
    },
  }
}

/** 读发单频道：`{value,label}`（`label` = 选中项的可见文案，用于说人话的报错）。 */
export function readPushChannel(root: HTMLElement): { value: string; label: string } | null {
  const selectEl = root.querySelector<HTMLSelectElement>(GROUPS_CHANNEL_SELECT)
  if (!selectEl) return null
  const option =
    Array.from(selectEl.options).find((item) => item.value === selectEl.value) ?? null
  const label = (option?.textContent ?? '').trim() || selectEl.value
  return { value: selectEl.value, label }
}

/** 频道发不出去时的中文原因（说清「缺哪个字段」）。 */
export function channelUnsupportedMessage(value: string, label: string): string {
  if (PUSH_CHANNEL_NEEDS_TARGET.includes(value)) {
    const target = value === 'RecPwd' ? '接单密码' : '指定的人或组'
    return (
      `${PUSH_CHANNEL_UNSUPPORTED_PREFIX}「${label}」需要另外指定${target}，` +
      '而本版发单接口还没有这个字段，尚未开放；请改选「公共频道 / 优质频道 / 认证订单」'
    )
  }
  return `${PUSH_CHANNEL_UNSUPPORTED_PREFIX}「${label}」不适用于代练通，请改选「公共频道 / 优质频道 / 认证订单」`
}

/**
 * 挂发单频道联动（逐条照对标 `PJ-js:31-51`）+ 历史下拉的空态。
 *
 * ⚠️ 历史记录**零请求**：对标那两发 `GetPushPlatfromAssisUserLog` / `Del…` 在本仓没有
 * 对应端点（后端只有 `groups` = 频道本身，没有「指定对象」/ 历史记录）⇒ 只按对标渲染
 * 那个下拉、聚焦时给一句「尚未开放」。
 */
export function mountPushChannel(root: HTMLElement, form: LayuiModule): { cleanup: () => void } {
  const selectEl = root.querySelector<HTMLSelectElement>(GROUPS_CHANNEL_SELECT)
  const ugdiv = root.querySelector<HTMLElement>(UGDIV_SELECTOR)
  const ugtext = root.querySelector<HTMLInputElement>(`input[name="${UGTEXT_NAME}"]`)
  const label = root.querySelector<HTMLElement>(UG_LABEL_SELECTOR)
  const history = root.querySelector<HTMLElement>(UG_HISTORY_SELECTOR)

  const hideHistory = (): void => {
    if (history) history.style.display = 'none'
  }

  const apply = (value: string): void => {
    const needsTarget = PUSH_CHANNEL_NEEDS_TARGET.includes(value)
    const isPassword = value === 'RecPwd'
    if (needsTarget) {
      if (ugdiv) ugdiv.style.display = ''
      if (label) label.textContent = isPassword ? UG_PASSWORD_LABEL : UG_DEFAULT_LABEL
      if (ugtext) {
        ugtext.placeholder = isPassword ? UG_PASSWORD_LABEL : UG_DEFAULT_LABEL
        ugtext.focus()
      }
      return
    }
    if (ugdiv) ugdiv.style.display = 'none'
    if (ugtext) {
      ugtext.value = ''
      ugtext.placeholder = UG_DEFAULT_LABEL
    }
    if (label) label.textContent = UG_DEFAULT_LABEL
    hideHistory()
  }

  ;(form.on as (event: string, cb: (obj: unknown) => void) => void)(
    `select(${GROUPS_CHANNEL_FILTER})`,
    (obj: unknown) => {
      const value = String((obj as { value?: string }).value ?? selectEl?.value ?? '')
      apply(value)
    },
  )
  apply(selectEl?.value ?? '')

  // 历史下拉：聚焦时显示空态（**零请求**）；点 `.ugtextarea` 之外收起（同对标 `$(document).bind`）。
  const onFocus = (): void => {
    if (!history) return
    history.textContent = ''
    const item = document.createElement('div')
    item.className = 'item'
    item.textContent = UG_HISTORY_EMPTY_TEXT
    history.appendChild(item)
    history.style.display = ''
  }
  const onDocumentClick = (event: Event): void => {
    const target = event.target as HTMLElement | null
    if (!target?.closest?.('.ugtextarea')) hideHistory()
  }
  ugtext?.addEventListener('focus', onFocus)
  document.addEventListener('click', onDocumentClick)

  return {
    cleanup: () => {
      ugtext?.removeEventListener('focus', onFocus)
      document.removeEventListener('click', onDocumentClick)
    },
  }
}

/**
 * 按「游戏/平台」的 label 文案**结构定位**证据件里那个只读输入框。
 *
 * 证据件形状：`.layui-inline > label.layui-form-label` + `.layui-input-block > input.layui-input`。
 * ⛔ 不按「第几个 input」取：左侧 24 项表单，顺序一变就取错。
 */
export function findZoneInput(root: HTMLElement): HTMLInputElement | null {  for (const label of Array.from(root.querySelectorAll<HTMLLabelElement>('label'))) {
    if ((label.textContent ?? '').trim() !== ZONE_LABEL_TEXT) continue
    const item = label.parentElement
    const block = item?.querySelector<HTMLElement>('.layui-input-block') ?? null
    const input = block?.querySelector<HTMLInputElement>('input.layui-input') ?? null
    if (input) return input
  }
  return null
}

/** `mountLiveSubmit` 的出口：主按钮拦截 + 预览回填 + 「不能发单」闸 + 卸载。 */
export type LiveSubmit = {
  /** 这次点击是不是主按钮（是则已接管，调用方不要再走「尚未开放」）。 */
  handleClick: (target: EventTarget | null) => boolean
  /** F5：把「该游戏的全部区服」填进下拉（保留当前选中项；不在列表里的补一条）。 */
  setZoneOptions: (zones: PlatformZone[], opts?: { autoSelect?: boolean }) => void
  /** 当前有没有选中区服（= 下拉有值）。 */
  hasZone: () => boolean
  /**
   * 把预览/用户选中的区服选中。
   *
   * `force=false`（默认）时**不覆盖**用户已经手选/手输过的区服；`force=true` 用于
   * 「用户手选之后重新取的预览」——那次响应就是按他选的区服算出来的，应当以它为准。
   */
  setZone: (zone: PlatformZone, force?: boolean) => void
  /**
   * 预览的 `can_push` 闸：`reason` 非空 ⇒ 页面显示这句中文原因并**禁用提交**；
   * 空串 ⇒ 解锁。
   */
  setBlocked: (reason: string) => void
  cleanup: () => void
}

/**
 * 给底部主按钮接上真提交：区服搜索、说明 / 结果块、三态展示、按钮防连点。
 *
 * ⚠️ 调用方只在**找得到主按钮**时调用（`mountPushJdpPage` 里 `null` 那一支直接跳过）；
 * 找不到就保持原样，⛔ 不假装能提交。
 */
export function mountLiveSubmit(
  root: HTMLElement,
  submitButton: HTMLButtonElement,
  layui: LayuiNamespace,
  layer: LayuiModule,
  orderNo: string,
  gate: SettingsGate,
  onZoneChosen?: (zone: PlatformZone) => void,
): LiveSubmit {
  const form = layui.form as LayuiModule
  let disposed = false
  let submitting = false
  let selectedZone: PlatformZone | null = null
  /** 用户是否动过区服那一格（动过 ⇒ 迟到的默认区服不许覆盖用户的选择）。 */
  let zoneTouched = false
  /** 预览判定「不能发单」的原因（非空 ⇒ 禁用提交）。 */
  let blockedReason = ''
  /**
   * 这一发**已经成功**（后端回了 sent + 平台单号）。
   *
   * ⇒ 按钮保持禁用（防重复发）；表单清空；父窗口收到消息后关弹窗 + 刷新列表。
   * ⛔ 失败 / 状态未知**不置这个旗标**（保留表单，允许改完再提交）。
   */
  let succeeded = false

  // ── 运行期样式（自有控件，无对标证据）──────────────────────────────────
  document.getElementById(LIVE_STYLE_ID)?.remove()
  const styleEl = document.createElement('style')
  styleEl.id = LIVE_STYLE_ID
  styleEl.textContent = LIVE_STYLE
  document.head.appendChild(styleEl)

  // ── 按钮上方的说明 + 三态结果块 ────────────────────────────────────────
  const note = document.createElement('div')
  note.className = NOTE_CLASS
  note.textContent = root.querySelector('#throwFeeHome')
    ? LIVE_NOTE_TEXT
    : LIVE_NOTE_TEXT.replace('同发内部抢单价 / ', '')

  const result = document.createElement('div')
  result.className = RESULT_CLASS
  result.setAttribute('role', 'status')
  result.setAttribute('aria-live', 'polite')
  result.style.display = 'none'

  // F1：预览判定「不能发单」时的可见原因（紧贴说明上方，⛔ 不静默禁用按钮）。
  const blockedNote = document.createElement('div')
  blockedNote.className = PREVIEW_BLOCK_CLASS
  blockedNote.setAttribute('role', 'alert')
  blockedNote.style.display = 'none'

  const block = submitButton.parentElement
  if (block) {
    block.insertBefore(note, submitButton)
    // 结果块放在说明**上面**：说明紧贴按钮，结果一眼可见又不挡按钮。
    block.insertBefore(result, note)
    block.insertBefore(blockedNote, note)
  } else {
    submitButton.insertAdjacentElement('beforebegin', note)
    submitButton.insertAdjacentElement('beforebegin', result)
    submitButton.insertAdjacentElement('beforebegin', blockedNote)
  }

  function setResult(state: PushResultState, text: string): void {
    result.setAttribute(RESULT_STATE_ATTR, state)
    result.textContent = text
    result.style.display = ''
  }

  function setBusy(busy: boolean): void {
    // 「预览说不能发」「提交在途」「已经发成功」任一成立 ⇒ 禁用（互相不许把按钮解锁）。
    const disabled = busy || blockedReason !== '' || succeeded
    submitButton.disabled = disabled
    submitButton.classList.toggle('layui-btn-disabled', disabled)
  }

  // ── 区服下拉（F5：证据件那格是 input，运行期换成 select）─────────────
  const zoneInput = findZoneInput(root)
  let zoneSelect: HTMLSelectElement | null = null

  /** 把当前选中的区服写进下拉（列表里没有这条时补一条，保证显示不丢）。 */
  function syncZoneSelect(): void {
    if (!zoneSelect || selectedZone === null) return
    const id = selectedZone.zone_server_id
    let option = Array.from(zoneSelect.options).find((item) => item.value === id) ?? null
    if (!option) {
      option = document.createElement('option')
      option.value = id
      option.textContent = selectedZone.server_name
      zoneSelect.appendChild(option)
    }
    zoneSelect.value = id
    ;(form.render as (type?: string) => void)('select')
  }

  /**
   * 用户换了一条区服（layui 把原生 select 藏起来、只在 `form.on('select')` 里回值——
   * ⛔ 不能只挂原生 `change`，那样生产点不动）。
   *
   * ⚠️ 同一条重复触发（layui 事件 + 同步值）只算一次：值没变就不重新取预览。
   */
  function applyZoneChoice(id: string, name: string): void {
    const zoneServerId = id.trim()
    if (zoneServerId === '') return
    if (selectedZone !== null && selectedZone.zone_server_id === zoneServerId) return
    selectedZone = { zone_server_id: zoneServerId, server_name: name }
    zoneTouched = true
    onZoneChosen?.(selectedZone)
  }

  if (zoneInput) {
    // 证据件里那一格是只读 input；业务方要求改成**可翻的下拉**。
    // 原 input 保留在标记里（`display:none`）——不删证据件节点，只运行期加 select。
    zoneInput.style.display = 'none'
    zoneSelect = document.createElement('select')
    zoneSelect.id = ZONE_SELECT_ID
    zoneSelect.className = ZONE_SELECT_CLASS
    zoneSelect.setAttribute('lay-filter', ZONE_SELECT_FILTER)
    zoneInput.insertAdjacentElement('afterend', zoneSelect)
    ;(form.on as (event: string, cb: (obj: unknown) => void) => void)(
      `select(${ZONE_SELECT_FILTER})`,
      (obj: unknown) => {
        const id = String((obj as { value?: string }).value ?? '')
        const name =
          Array.from(zoneSelect?.options ?? []).find((option) => option.value === id)?.textContent ??
          ''
        applyZoneChoice(id, name)
      },
    )
  }

  /**
   * F5：把该游戏的全部区服填进下拉（保留当前选中；没有选中就选第一条）。
   *
   * ⭐ B3：`autoSelect=false` 用于「游戏名匹配不到、列全部区服让人手选」那一档
   * ——那一条**不是**我们确认过的默认值，⛔ 不许替用户选。
   */
  function setZoneOptions(
    zones: PlatformZone[],
    options: { autoSelect?: boolean } = {},
  ): void {
    const autoSelect = options.autoSelect !== false
    if (!zoneSelect) return
    const current = selectedZone?.zone_server_id ?? zoneSelect.value
    zoneSelect.textContent = ''
    for (const zone of zones) {
      const id = (zone.zone_server_id ?? '').trim()
      if (id === '') continue
      const option = document.createElement('option')
      option.value = id
      option.textContent = zone.server_name
      zoneSelect.appendChild(option)
    }
    // 当前选中的那条不在列表里（例如映射记的是另一条）⇒ 补一条，别把显示弄丢。
    if (current !== '' && !Array.from(zoneSelect.options).some((o) => o.value === current)) {
      const picked = selectedZone
      const option = document.createElement('option')
      option.value = current
      option.textContent = picked?.server_name ?? ''
      zoneSelect.appendChild(option)
    }
    if (current !== '') zoneSelect.value = current
    else if (autoSelect && zoneSelect.options.length > 0) {
      // 业务方要求：列出全部区服、**默认选第一个**（只对「按游戏名匹配到」的那一档）。
      const first = zoneSelect.options[0]!
      zoneSelect.value = first.value
      selectedZone = { zone_server_id: first.value, server_name: first.textContent ?? '' }
    }
    ;(form.render as (type?: string) => void)('select')
  }

  function hasZone(): boolean {
    return selectedZone !== null || (zoneSelect?.value ?? '') !== ''
  }

  /**
   * F5：把预览给的默认区服选中（`zone_server_id` 为空 ⇒ 什么都不做）。
   *
   * ⚠️ 默认区服是**异步**来的；用户在它回来之前就手选过 ⇒ `force=false` 时**不许覆盖**
   * 用户的选择（`force=true` 只用于「用户手选之后重新取的预览」那一发）。
   */
  function setZone(zone: PlatformZone, force = false): void {
    if (zoneTouched && !force) return
    const id = (zone.zone_server_id ?? '').trim()
    if (id === '') return
    selectedZone = { zone_server_id: id, server_name: zone.server_name ?? '' }
    syncZoneSelect()
  }

  /** 预览的 `can_push` 闸：原因非空 ⇒ 显示原因 + 禁用提交；空串 ⇒ 解锁。 */
  function setBlocked(reason: string): void {
    blockedReason = reason.trim()
    if (blockedReason === '') {
      blockedNote.textContent = ''
      blockedNote.style.display = 'none'
    } else {
      blockedNote.textContent = blockedReason
      blockedNote.style.display = ''
    }
    setBusy(submitting)
  }

  // ── 提交 ───────────────────────────────────────────────────────────────
  function reject(message: string): void {
    setResult('failed', message)
    islandMsg(layer, message)
  }

  /**
   * 发单成功后的清场：把可能残留的输入清空（下次打开是干净的默认值；iframe 本来就重建，
   * 这一步是为「父窗口没关掉 / 单独打开本页」那两种情况兜底）。
   */
  function clearSubmittedForm(): void {
    for (const name of [
      'throwFee',
      'safeAmount',
      'effAmount',
      'gameAcc',
      'gamePwd',
      'gameRole',
      'orderTitle',
      'moblie2',
      'qq',
      'label1',
      'ugtext',
      'remark',
    ]) {
      const input = root.querySelector<HTMLInputElement>(`input[name="${name}"]`)
      if (input) input.value = ''
    }
    for (const name of ['gaemTf', 'requestContent']) {
      const area = root.querySelector<HTMLTextAreaElement>(`textarea[name="${name}"]`)
      if (area) area.value = ''
    }
  }

  /**
   * 发单成功 ⇒ 通知**父窗口**（订单列表 / 订单详情）关弹窗 + 刷新 + 出提示。
   *
   * ⚠️ 走 `postMessage` 而不是直接摸 `parent.layer`：本页也会被单独打开
   * （那时 `window.parent === window`），直接摸父窗口的 layui 会炸（同 `notifyOrderSaved`）。
   */
  function notifyParentPushSent(): void {
    window.parent.postMessage(
      { type: PUSH_JDP_SENT_MESSAGE_TYPE, order_no: orderNo },
      window.location.origin,
    )
  }

  async function submit(): Promise<void> {
    if (submitting) return
    // 已经发成功过 ⇒ ⛔ 不再发第二单（父窗口通常已关窗，这里是单独打开时的兜底）。
    if (succeeded) return
    if (blockedReason !== '') {
      // 按钮已禁用；键盘/脚本触发也⛔ 不发请求，只把后端算出来的原因再说一遍。
      islandMsg(layer, blockedReason)
      return
    }
    if (!ORDER_NO_PATTERN.test(orderNo)) {
      reject(ORDER_NO_INVALID_MESSAGE)
      return
    }
    if (selectedZone === null) {
      reject(ZONE_REQUIRED_MESSAGE)
      return
    }
    const price = readInputValue(root, 'throwFee')
    if (price === '') {
      reject(PRICE_REQUIRED_MESSAGE)
      return
    }
    // 价格 <2 元是客服能改的问题 ⇒ 提交时拦、给明确文案（按钮本身不清禁用，见 F1 的拍板）。
    const priceNumber = Number(price)
    if (Number.isFinite(priceNumber) && priceNumber < PRICE_WARN_THRESHOLD) {
      reject(PRICE_LOW_MESSAGE)
      return
    }
    const timeLimitHours = readTimeLimitHours(root)
    if (timeLimitHours === null) {
      reject(TIME_LIMIT_INVALID_MESSAGE)
      return
    }

    // ⭐ G4：「发单频道」与「延迟发平台」两道**页面侧**闸。
    // 频道：后端只有 `groups`（频道本身），没有「指定对象」字段 ⇒ 那三个频道发不出去，
    //       明确拒（⛔ 静默按公共频道发 = 页面说 A、实际发 B）。
    // 延迟：后端没有 `isDelay` / `delaySec` 入参 ⇒ 选「是」时提交 = 立即发平台，
    //       会让人以为延迟了 ⇒ 拦下、零请求。
    const channel = readPushChannel(root)
    if (channel === null) {
      reject(PUSH_CHANNEL_MISSING_MESSAGE)
      return
    }
    if (!PUSH_CHANNEL_SUPPORTED.includes(channel.value)) {
      reject(channelUnsupportedMessage(channel.value, channel.label))
      return
    }
    if (delayEnabled(root)) {
      reject(DELAY_UNSUPPORTED_MESSAGE)
      return
    }

    // 页面所见 = 本单所发：设置读到真值 ⇒ 两个门槛都带上（显式 null = 本单不设置）；
    // 设置没读到真值 ⇒ 两个键都不带，由后端用全局默认（⛔ 不拿证据件里的 5 / 0 冒充）。
    // ⭐ I1：用户**手动改过**门槛时照发（不然「读取失败 ⇒ 只能不带」会让
    // 想指定的人没法指定）；页面上的「读取失败」提示已经说清默认值没被当真实值提交。
    let acceptPayload: { power_level: number; limit_accept: number | null } | null = null
    if (gate.loaded || acceptTouched(root)) {
      acceptPayload = readAcceptPayload(root)
      if (acceptPayload === null) {
        reject(ACCEPT_INVALID_MESSAGE)
        return
      }
    }
    // ⭐ I3：代练要求（页面填的值优先，空则交给后端用全局模板）与
    // 自动下架天数（`0`/非法 = 不上送，由上游用它的默认 7 天）。
    const requirement = readTextareaValue(root, 'requestContent')
    const offSellDays = readOffSellDays(root)
    // ⭐ P9（2026-10-27，业务方真机 bug）：发出去的代练内容 = **「当前订单说明」框的当前值**。
    // 框在打开时由 `preview.memo` 带出，用户改过就用用户的；空串也照发（后端决定空串语义）。
    // ⛔ 不再让后端按 `Order.content/remark` 重新生成一份 —— 那样页面写的和打手收到的会不一致。
    const memo = readTextareaValue(root, 'gaemTf')
    // ⭐ G4：同发内部抢单价 / 标签也随单提交（后端 `hall_price_same` / `label1` 已支持）。
    // 大厅价 `0` / 空 = **不发**（大厅价沿用发单价；⛔ 不发 0 把大厅价改成免费）。
    const hallRaw = readInputValue(root, DELAY_HALL_NAME)
    const hallNumber = Number(hallRaw)
    const hallPrice =
      hallRaw !== '' && Number.isFinite(hallNumber) && hallNumber > 0 ? hallRaw : ''
    const label1 = readInputValue(root, 'label1')
    const payload: PushToPlatformPayload = {
      platform: DAILIANTONG_PLATFORM,
      price,
      ensure1: readInputValue(root, 'safeAmount') || '0.00',
      ensure2: readInputValue(root, 'effAmount') || '0.00',
      time_limit_hours: timeLimitHours,
      zone_server_id: selectedZone.zone_server_id,
      // ⭐ G4：发单频道（`groups`）。只发本版真支持的三档（上面那道闸已排除其余）。
      groups: channel.value,
      ...(hallPrice === '' ? {} : { hall_price_same: hallPrice }),
      ...(label1 === '' ? {} : { label1 }),
      ...(requirement === '' ? {} : { requirement }),
      ...(offSellDays === null ? {} : { off_sell_days: offSellDays }),
      ...(acceptPayload ?? {}),
      memo,
    }

    submitting = true
    setBusy(true)
    // ⚠️ 在途 ≠「已提交」：此刻请求还没回来，只能给中性的进度文案。
    setResult('pending', IN_FLIGHT_MESSAGE)
    try {
      const row = await pushOrderToPlatform(orderNo, payload)
      if (disposed) return
      if (row.state === 'sent' && (row.serial_no ?? '') !== '') {
        // 成功：先显示成功（含平台单号）→ 清残留输入 → 通知父窗口关窗 + 刷新列表。
        succeeded = true
        setResult('sent', `${SENT_MESSAGE}，平台单号：${row.serial_no}。${CLOSE_HINT}`)
        islandMsg(layer, PUSH_JDP_SENT_TEXT)
        clearSubmittedForm()
        notifyParentPushSent()
        setBusy(false)
        return
      }
      if (row.state === 'failed') {
        const reason = (row.last_error ?? '').trim()
        setResult('failed', `${FAILED_PREFIX}${reason === '' ? '平台未返回失败原因，请人工核对' : reason}`)
        return
      }
      if (row.state === 'unknown' || row.state === 'sent') {
        // `sent` 却没单号（后端实测没出现过）也**不能**当成功——那会让对账永远找不到单。
        const reason = (row.last_error ?? '').trim()
        setResult(
          'unknown',
          reason === '' ? UNKNOWN_STATE_MESSAGE : `${UNKNOWN_STATE_MESSAGE}。后台返回：${reason}`,
        )
        return
      }
      if (row.state === 'pending') {
        setResult('pending', PENDING_MESSAGE)
        return
      }
      // ⛔ 兜底不再说「已提交，平台处理中」：`closed` 或将来新增的码都不是那个意思，
      // 说错会让人以为单还在平台上跑。如实给出状态码、让人核对。
      setResult('unknown', unexpectedStateMessage(String(row.state)))
    } catch (error: unknown) {
      if (disposed) return
      const message = errorMessage(error)
      if (classifyPushError(error) === 'failed') {
        // 后端**明确**说没成（业务码 / 4xx）⇒ 才允许写「发单失败」。
        setResult('failed', `${FAILED_PREFIX}${message}`)
        islandMsg(layer, message)
      } else {
        // 写动作可能已经到达上游：⛔ 不许说「失败」，也不许说「已提交」。
        setResult('unknown', requestUnknownText(message))
        islandMsg(layer, REQUEST_RESULT_UNKNOWN_MESSAGE)
      }
    } finally {
      submitting = false
      if (!disposed) setBusy(false)
    }
  }

  function handleClick(target: EventTarget | null): boolean {
    const el = target as HTMLElement | null
    if (!el || typeof el.closest !== 'function') return false
    const hit = el.closest<HTMLElement>(SUBMIT_SELECTOR)
    if (!hit || !submitButton.contains(hit)) return false
    if (succeeded) return true
    if (blockedReason !== '') {
      islandMsg(layer, blockedReason)
      return true
    }
    void submit()
    return true
  }

  return {
    handleClick,
    setZoneOptions,
    hasZone,
    setZone,
    setBlocked,
    cleanup: () => {
      disposed = true
      styleEl.remove()
    },
  }
}

/* ── F1 / F5：页面挂载时的默认值装载（订单 + 设置 + 默认区服）────────────── */

/** 一条「默认值没取到」的可见说明（⛔ 不静默留空）。 */
export function mountDefaultsWarning(root: HTMLElement): {
  show: (text: string) => void
  cleanup: () => void
} {
  const submitButton = root.querySelector<HTMLElement>(SUBMIT_SELECTOR)
  const block = submitButton?.parentElement ?? null
  const note = document.createElement('div')
  note.className = DEFAULTS_WARNING_CLASS
  note.style.display = 'none'
  note.setAttribute('role', 'status')
  if (block && submitButton) block.insertBefore(note, submitButton)
  else root.appendChild(note)
  return {
    show: (text: string) => {
      note.textContent = text
      note.style.display = ''
    },
    cleanup: () => note.remove(),
  }
}

/** 默认区服那一格下面的提示（匹配不到 / 查询失败）。 */
export function mountZoneTip(root: HTMLElement): {
  show: (text: string) => void
  clear: () => void
  cleanup: () => void
} {
  const zoneInput = findZoneInput(root)
  const holder = zoneInput?.parentElement ?? null
  const tip = document.createElement('div')
  tip.className = ZONE_TIP_CLASS
  tip.style.display = 'none'
  tip.setAttribute('role', 'status')
  if (holder) holder.appendChild(tip)
  else root.appendChild(tip)
  return {
    show: (text: string) => {
      tip.textContent = text
      tip.style.display = ''
    },
    clear: () => {
      tip.textContent = ''
      tip.style.display = 'none'
    },
    cleanup: () => tip.remove(),
  }
}

/**
 * F1（业务方 2026-10-27）：「将发到代练通的内容」只读预览块。
 *
 * ⭐ P9：内容**只来自「当前订单说明」框的当前值**（`textarea[name=gaemTf]`），
 * 框里输入即时同步 —— ⛔ 不再单独展示 `preview.memo`（那会让「改了框、这块还显示旧值」，
 * 而提交的是框里的值 ⇒ 页面说假话）。`apply()` 只负责把「预览已读到」这个状态记下来并
 * 在框里为空时补上 `preview.memo`（正常路径由 `applyPreviewDefaults` 先填好，这里兜底）。
 */
export function mountPushMemo(root: HTMLElement): {
  apply: (memo?: string) => void
  /** 预览读取失败：如实说明，⛔ 不停在「正在读取」。 */
  fail: () => void
  cleanup: () => void
} {
  const submitButton = root.querySelector<HTMLElement>(SUBMIT_SELECTOR)
  const block = submitButton?.parentElement ?? null
  const area = root.querySelector<HTMLTextAreaElement>('textarea[name="gaemTf"]')
  const box = document.createElement('div')
  box.className = PUSH_MEMO_CLASS
  const title = document.createElement('div')
  title.className = 'tt-push-memo-title'
  title.textContent = PUSH_MEMO_TITLE
  const body = document.createElement('div')
  body.className = 'tt-push-memo-body'
  body.textContent = PUSH_MEMO_LOADING_TEXT
  box.appendChild(title)
  box.appendChild(body)
  if (block && submitButton) block.insertBefore(box, submitButton)
  else root.appendChild(box)

  let loaded = false
  let failed = false
  /** 预览值只允许**第一次**回填；之后的重取（换区服）⛔ 不许把用户清空 / 改过的框填回去。 */
  let applied = false
  const sync = (): void => {
    const text = (area?.value ?? '').trim()
    if (text !== '') {
      body.textContent = text
      return
    }
    if (failed) body.textContent = PUSH_MEMO_FAILED_TEXT
    else body.textContent = loaded ? PUSH_MEMO_EMPTY_TEXT : PUSH_MEMO_LOADING_TEXT
  }
  const onInput = (): void => sync()
  area?.addEventListener('input', onInput)

  return {
    apply: (memo?: string) => {
      loaded = true
      // 兜底：**只在第一发预览**、框还空着而预览有内容时填（正常路径 `applyPreviewDefaults`
      // 已填过）。⚠️ 换区服会再取一次预览——那一发若也回填，会把用户**有意清空**的框填回去
      // （= 用户删掉的内容又发出去）。
      const text = String(memo ?? '').trim()
      if (!applied && area && area.value.trim() === '' && text !== '') area.value = text
      applied = true
      sync()
    },
    fail: () => {
      failed = true
      loaded = true
      sync()
    },
    cleanup: () => {
      area?.removeEventListener('input', onInput)
      box.remove()
    },
  }
}

/** preview.warnings：客服能在弹窗里改的提示（价格 / 区服）——只显示、⛔ 不禁用提交。 */
export function mountPreviewWarnings(root: HTMLElement): {
  apply: (warnings: string[] | undefined) => void
  cleanup: () => void
} {
  const submitButton = root.querySelector<HTMLElement>(SUBMIT_SELECTOR)
  const block = submitButton?.parentElement ?? null
  const note = document.createElement('div')
  note.className = PREVIEW_WARNINGS_CLASS
  note.setAttribute('role', 'status')
  note.style.display = 'none'
  if (block && submitButton) block.insertBefore(note, submitButton)
  else root.appendChild(note)
  return {
    apply: (warnings?: string[]) => {
      const items = (warnings ?? []).map((item) => String(item).trim()).filter((item) => item !== '')
      if (items.length === 0) {
        note.textContent = ''
        note.style.display = 'none'
        return
      }
      note.textContent = items.join('\n')
      note.style.display = ''
    },
    cleanup: () => note.remove(),
  }
}

function selectOption(select: HTMLSelectElement | null, value: string): boolean {
  if (!select) return false
  const found = Array.from(select.options).some((option) => option.value === value)
  if (!found) return false
  select.value = value
  return true
}

/** textarea 版的「只填空」（`gaemTf` / `requestContent`）。 */
function fillTextareaIfEmpty(root: HTMLElement, name: string, value: string): void {
  const area = root.querySelector<HTMLTextAreaElement>(`textarea[name="${name}"]`)
  if (!area || value === '') return
  if (area.value.trim() !== '') return
  area.value = value
}

/**
 * 预览的 `time_limit_hours`（总小时）→「时间要求」两个下拉（`day` + `hours`）。
 *
 * ⚠️ 只在「还是证据件默认的 720h」时调用：预览是异步的，用户可能已经改过时限，
 * ⛔ 不许把人家改好的值盖回去。
 */
function applyTimeLimitFromHours(
  root: HTMLElement,
  form: LayuiModule,
  totalHours: number,
): void {
  const dayEl = root.querySelector<HTMLSelectElement>('select[name="day"]')
  const hoursEl = root.querySelector<HTMLSelectElement>('select[name="hours"]')
  if (!dayEl || !hoursEl) return
  const total = Math.trunc(totalHours)
  if (!Number.isFinite(total) || total < TIME_LIMIT_MIN_HOURS || total > TIME_LIMIT_MAX_HOURS) return
  const day = Math.floor(total / 24)
  const rest = total % 24
  if (!selectOption(dayEl, String(day)) || !selectOption(hoursEl, String(rest))) return
  ;(form.render as (type?: string) => void)('select')
}

/**
 * F1：把预览里的**非区服**默认值填进页面（都只填「用户没改过」的格子）。
 *
 * 映射：价格 → `throwFee`；两个保证金 → `safeAmount` / `effAmount`；时限 → `day`+`hours`；
 * 标题 → `orderTitle`；账号资料五段 → `gameAcc` / `gamePwd` / `gameRole` / `gaemTf` /
 * `requestContent`；联系 QQ / 电话 → `qq` / `moblie2`；门槛 → 两个下拉。
 */
export function applyPreviewDefaults(
  root: HTMLElement,
  form: LayuiModule,
  preview: PlatformPushPreview,
): void {
  // `price` 可能是 `null`（订单没定价）⇒ 清空、要求手填（⛔ 不显示 0.00）。
  fillPriceIfDefault(root, preview.price ?? '')
  // 保证金：证据件默认 `0.00` / 空 ⇒ 可覆盖；用户改过就保留。
  fillIfDefault(root, 'safeAmount', preview.ensure1, ['', '0.00'])
  fillIfDefault(root, 'effAmount', preview.ensure2, ['', '0.00'])
  // 时限：只在还是证据件默认（720h）时按预览填。
  if (readTimeLimitHours(root) === TIME_LIMIT_MAX_HOURS) {
    applyTimeLimitFromHours(root, form, preview.time_limit_hours)
  }
  fillIfEmpty(root, 'orderTitle', preview.title)
  fillIfEmpty(root, 'gameAcc', preview.game_account)
  fillIfEmpty(root, 'gamePwd', preview.game_password)
  fillIfEmpty(root, 'gameRole', preview.game_role)
  // ⭐ P8（2026-10-27，业务方真机 bug）：「当前订单说明」框打开时自动带出**代练内容**
  // （`preview.memo` = `Order.content` + `Order.remark`）。
  // ⛔ 旧实现填的是 `preview.current_info`（Actors 第三段 = 常量占位「其它:无」），
  // 客服看到的是一句假内容、打手收到的也是那句。
  fillTextareaIfEmpty(root, 'gaemTf', preview.memo ?? '')
  fillTextareaIfEmpty(root, 'requestContent', preview.requirement)
  fillIfEmpty(root, 'moblie2', preview.contact_phone)
  fillIfEmpty(root, 'qq', preview.contact_qq)

  // 门槛：用户没动过这两格才按预览填（设置接口也会填同一份值，两边一致）。
  const powerEl = root.querySelector<HTMLSelectElement>(ACCEPT_POWER_SELECT)
  const limitEl = root.querySelector<HTMLSelectElement>(ACCEPT_LIMIT_SELECT)
  let changed = false
  if (powerEl && powerEl.dataset.touched !== '1') {
    powerEl.value = String(preview.power_level)
    changed = true
  }
  if (limitEl && limitEl.dataset.touched !== '1') {
    limitEl.value = limitAcceptOption({ default_limit_accept: preview.limit_accept })
    changed = true
  }
  if (changed) (form.render as (type?: string) => void)('select')
}

/** 预览的区服段 → 页面选中态（`force` 见 `LiveSubmit.setZone` 的注释）。 */
function applyPreviewZone(
  preview: PlatformPushPreview,
  opts: { live: LiveSubmit | null; zoneTip: { show: (t: string) => void; clear: () => void } },
  force: boolean,
): void {
  const zoneServerId = (preview.zone_server_id ?? '').trim()
  if (zoneServerId === '') {
    if (preview.zone_source === 'none') opts.zoneTip.show(ZONE_DEFAULT_NONE_MESSAGE)
    return
  }
  opts.live?.setZone(
    { zone_server_id: zoneServerId, server_name: preview.server_name ?? '' },
    force,
  )
  if ((preview.server_name ?? '').trim() === '') {
    opts.zoneTip.show(ZONE_DEFAULT_NAME_MISSING_MESSAGE)
  } else {
    opts.zoneTip.clear()
  }
}

export type PushPreviewOptions = {
  live: LiveSubmit | null
  form: LayuiModule
  priceHint: { update: () => void }
  warn: (text: string) => void
  zoneTip: { show: (text: string) => void; clear: () => void }
  /** preview.warnings 的展示位（只提示、不禁用提交）。 */
  warnings: { apply: (warnings?: string[]) => void }
  /** 「将发到代练通的内容」只读预览块（=「当前订单说明」框的当前值）。 */
  memo: { apply: (memo?: string) => void; fail: () => void }
}

export type PushPreview = {
  /** 重新取预览；`zoneServerId` 省略 = 用后端默认区服（首次打开）。 */
  load: (zoneServerId?: string) => void
  cleanup: () => void
}

/**
 * F1 / F5：发单预览（`platform-push/preview/`）的唯一消费者。
 *
 * - 首次 `load()` 不带区服：把**后端真正将要发送的**价格 / 保证金 / 时限 / 区服 / 标题 /
 *   账号资料五段 / 联系 QQ·电话 / 门槛一次性填进页面；`can_push=false` ⇒ 显示 `reason` 并禁用提交；
 * - 用户手选区服后 `load(zone_server_id)`：后端按这个区服重算 ⇒ **只更新区服与 `can_push`**，
 *   不覆盖用户已改的其它字段；
 * - 序号防串台：只有**最新**那一发的区服 / `can_push` 生效（迟到的旧响应不许把区服换回去）。
 */
export function mountPushPreview(
  root: HTMLElement,
  orderNo: string,
  opts: PushPreviewOptions,
): PushPreview {
  let disposed = false
  let latestSeq = 0
  let defaultsApplied = false
  /** 区服列表只按 game_id 取一次（同一张单游戏不会变）。 */
  let zonesLoadedFor: number | null = null

  const powerEl = root.querySelector<HTMLSelectElement>(ACCEPT_POWER_SELECT)
  const limitEl = root.querySelector<HTMLSelectElement>(ACCEPT_LIMIT_SELECT)
  const onPowerChange = (): void => {
    if (powerEl) powerEl.dataset.touched = '1'
  }
  const onLimitChange = (): void => {
    if (limitEl) limitEl.dataset.touched = '1'
  }
  powerEl?.addEventListener('change', onPowerChange)
  limitEl?.addEventListener('change', onLimitChange)

  const load = (zoneServerId?: string): void => {
    const seq = (latestSeq += 1)
    void fetchPlatformPushPreview(orderNo, zoneServerId).then(
      (preview) => {
        if (disposed) return
        const isLatest = seq === latestSeq
        if (!defaultsApplied) {
          applyPreviewDefaults(root, opts.form, preview)
          defaultsApplied = true
          opts.priceHint.update()
        }
        if (!isLatest) return
        applyPreviewZone(preview, opts, defaultsApplied ? zoneServerId !== undefined : false)
        // 客服能改的问题（价格 / 区服）只提示、⛔ 不参与禁用；内容预览只读。
        opts.warnings.apply(preview.warnings)
        opts.memo.apply(preview.memo)
        /**
         * `can_push` 只用于「弹窗里改不了」的原因（没配置 / 状态不允许 / 已抛过）。
         *
         * ⭐ I2：**先看硬件原因、再给价格提示** —— 旧实现是
         * `priceBelowMinimum || preview.can_push ⇒ 解锁`，于是「价格 <2」会把
         * 「状态不允许 / 已抛过 / 没配置」这些硬原因**遮住**（页面只提示改价，
         * 客服改完价仍然发不出去）。现在只认 `can_push`：硬原因一律显示；
         * 价格 <2 只提示、不参与解锁（提交时另拦，文案见 `PRICE_LOW_MESSAGE`）。
         * ⚠️ 只对**最新一发**预览生效（上面的 `isLatest` 闸）⇒ 迟到的旧响应不会把
         * 刚算出来的硬原因解锁。
         */
        if (preview.can_push) {
          opts.live?.setBlocked('')
        } else {
          opts.live?.setBlocked((preview.reason ?? '').trim() || PREVIEW_BLOCKED_FALLBACK)
        }

        // F5：按 preview.game_id 拉「该游戏的全部区服」填下拉（只拉一次）。
        const gameId = Number(preview.game_id)
        if (Number.isInteger(gameId) && gameId > 0 && zonesLoadedFor !== gameId) {
          zonesLoadedFor = gameId
          const hadZone = opts.live?.hasZone() ?? false
          void fetchGamePlatformZones(gameId).then(
            (zones) => {
              if (disposed) return
              if (zones.length === 0) {
                // 预览已经给了一条（映射记的）时不算「没有区服」——只有一条都没有时才回退。
                if (hadZone) return
                // ⭐ B3：按游戏名匹配不到 ⇒ 列**全部区服**让客服手选（⛔ 不许猜、⛔ 不许自动选）。
                void fetchPlatformZones('').then(
                  (allZones) => {
                    if (disposed) return
                    opts.live?.setZoneOptions(allZones, { autoSelect: false })
                    opts.zoneTip.show(ZONE_NO_GAME_MATCH_MESSAGE)
                  },
                  (error: unknown) => {
                    if (disposed) return
                    opts.zoneTip.show(`${ZONE_LIST_FAILED_PREFIX}${errorMessage(error)}`)
                  },
                )
                return
              }
              opts.live?.setZoneOptions(zones)
              if (!hadZone) {
                // 业务方要求「默认选第一个」：下拉自己选了第一条后，带它再取一次预览保持同源。
                load(zones[0]!.zone_server_id)
              }
            },
            (error: unknown) => {
              if (disposed) return
              opts.zoneTip.show(`${ZONE_LIST_FAILED_PREFIX}${errorMessage(error)}`)
            },
          )
        }
      },
      (error: unknown) => {
        if (disposed || seq !== latestSeq) return
        // 预览取不到：如实说，但**不猜** `can_push`（后端提交时还有同一套闸）。
        opts.warn(`${PREVIEW_FAILED_PREFIX}${errorMessage(error)}`)
        // ⭐ P9：底部「将发到代练通的内容」块也必须如实说读取失败（⛔ 停在「正在读取」）。
        opts.memo.fail()
      },
    )
  }

  return {
    load,
    cleanup: () => {
      disposed = true
      latestSeq += 1
      powerEl?.removeEventListener('change', onPowerChange)
      limitEl?.removeEventListener('change', onLimitChange)
    },
  }
}

/** F3 / F4：门槛与模板的全局默认值（读到真值之前两道提交闸都关着）。 */
export async function loadSettingsDefaults(opts: {
  gate: SettingsGate
  accept: Pick<AcceptSettings, 'apply'>
  template: Pick<RequireTemplate, 'apply'>
  warn: (text: string) => void
}): Promise<void> {
  try {
    const settings = await fetchPlatformPushSettings()
    opts.accept.apply(settings)
    opts.template.apply(settings)
    opts.gate.loaded = true
  } catch (error: unknown) {
    opts.gate.loaded = false
    // ⭐ I1：读失败必须**如实**说清两件事：
    // ① 页面上的门槛值不是读到真值的默认值；② 本次提交不会把它们当真实值带上
    // （除非用户手动改过 —— 那一档由 `acceptTouched` 放行）。
    opts.warn(
      `${DEFAULTS_SETTINGS_FAILED_PREFIX}${errorMessage(error)}。` +
        '门槛读取失败：本次提交默认不会带上「要求最低完单数 / 拒绝新手和介入率」，' +
        '也不会自动填入代练要求模板；如需指定请在页面里手动填写后再提交。',
    )
  }
}

export async function mountPushJdpPage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  // ⚠️ 先挂样式再塞标记（FOUC，E-0067）；`initIslandElements` 相反必须留在塞标记之后。
  const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule

  document.title = TITLE
  const [artifact, canPublishToHall] = await Promise.all([
    fetchArtifactHtml(ARTIFACT_URL),
    fetchMe().then((viewer) => canNode(viewer, 'order.send_to_grab_hall')).catch(() => false),
  ])
  root.innerHTML = artifact
  if (!canPublishToHall) root.querySelector('#throwFeeHome')?.closest('.layui-form-item')?.remove()

  // 隐藏域 `#orderID`：证据件里已清空（PII），运行期按查询参数回填。
  // `/^\d{20}$/` 形状闸与列表侧开层前那道同口径：形状不对就**不回填**（不把不可信输入写进表单）。
  const orderNo = new URLSearchParams(window.location.search).get(ORDER_NO_PARAM) ?? ''
  const orderIdInput = root.querySelector<HTMLInputElement>('#orderID')
  if (orderIdInput && ORDER_NO_PATTERN.test(orderNo)) orderIdInput.value = orderNo

  // 右栏平台摘要表：先渲染一次空表（layui 的实例要先注册），摘要到了再 `reloadData`。
  ;(table.render as (opts: unknown) => void)({
    elem: TABLE_ELEM,
    id: TABLE_ID,
    data: [],
    maxHeight: 390,
    page: false,
    limit: 100,
    cols: [PLATFORM_COLS],
  })
  // 证据件里的 select / radio / checkbox 由 layui 换成渲染件。
  ;(form.render as () => void)()
  initIslandElements(layui)

  // G4：延迟发平台块（显示条件照对标）+ 发单频道 ↔「指定人或组」联动。
  // 提交闸在 `mountLiveSubmit::submit()` 里（读这两处的**当前**状态）。
  const delayBlock = mountDelayBlock(root)
  const channelControl = mountPushChannel(root, form)

  // 底部主按钮接真（T9d + F3）：价格 / 两个保证金 / 时限 / 区服 / 两个门槛 + platform。
  const submitButton = root.querySelector<HTMLButtonElement>(SUBMIT_SELECTOR)
  // F3：门槛 / 模板共用的「设置读到真值了吗」闸（提交前必须就位才带本单门槛）。
  const gate: SettingsGate = { loaded: false }
  // F1 / F5：预览控制器（闭包先声明，用户手选区服时回调它）。
  let preview: PushPreview | null = null
  const live =
    submitButton === null
      ? null
      : mountLiveSubmit(root, submitButton, layui, layer, orderNo, gate, (zone) => {
          // F5：用户手选区服 ⇒ 带 zone_server_id 重新取一次预览（保持与后端同源）。
          preview?.load(zone.zone_server_id)
        })

  // F1–F5 的页面级装载。
  const priceHint = mountPriceHint(root)
  const defaultsWarning = mountDefaultsWarning(root)
  const zoneTip = mountZoneTip(root)
  const accept = mountAcceptSettings(root, form, layer, gate)
  const template = mountRequireTemplate(root, layui, layer, gate)
  // 「常用模板」下拉（代练通侧上游模板，与 F4 本地全局模板是两回事）。
  const templateList = mountTemplateList(root, form, layer)
  const summary = mountSummaryPanel(root, table, layer)
  const previewWarnings = mountPreviewWarnings(root)
  const memo = mountPushMemo(root)
  const warn = (text: string): void => defaultsWarning.show(text)
  preview = mountPushPreview(root, orderNo, {
    live,
    form,
    priceHint,
    warn,
    zoneTip,
    warnings: previewWarnings,
    memo,
  })

  // F2：右面板与设置无关，页一开就取（后端 30s 缓存，失败/未配置都显示中文原因）。
  summary.load()
  void loadSettingsDefaults({ gate, accept, template, warn })
  // F1 / F5：发单预览是页面默认值的唯一来源；订单号形状不对就不取（⛔ 拿不可信输入拼 URL）。
  if (ORDER_NO_PATTERN.test(orderNo)) {
    // 证据件里 `#throwFee` 的 `0` 是占位值，⛔ 不能当默认价发出去（0 元单上游必拒）。
    // 先清空，等预览填；预览失败时价格保持为空 ⇒ 客户端必填闸会拦住提交、让客服手填。
    const priceInput = root.querySelector<HTMLInputElement>('input[name="throwFee"]')
    if (priceInput && priceInput.value.trim() === '0') priceInput.value = ''
    preview.load()
  }

  // 点击收口（capture）：主按钮 / 门槛保存 / 模板入口先接管，其余可点控件仍走「尚未开放」。
  const onClick = (event: Event): void => {
    const target = event.target
    const handled = live?.handleClick(target) || accept.handleClick(target) || template.handleClick(target)
    if (handled) {
      event.preventDefault()
      event.stopPropagation()
      return
    }
    if (!isNotOpenTarget(event.target)) return
    event.preventDefault()
    event.stopPropagation()
    const el = event.target as HTMLElement
    const hit = el.closest<HTMLElement>(NOT_OPEN_SELECTOR)
    const label = hit?.getAttribute('lay-filter') === SUBMIT_FILTER
      ? SUBMIT_TEXT
      : clickLabel(hit ?? el) || '该功能'
    islandMsg(layer, `「${label}」尚未开放`)
  }
  root.addEventListener('click', onClick, true)

  return () => {
    root.removeEventListener('click', onClick, true)
    live?.cleanup()
    preview?.cleanup()
    accept.cleanup()
    template.cleanup()
    templateList.cleanup()
    summary.cleanup()
    priceHint.cleanup()
    defaultsWarning.cleanup()
    zoneTip.cleanup()
    previewWarnings.cleanup()
    memo.cleanup()
    delayBlock.cleanup()
    channelControl.cleanup()
    // 只清我们自己注入的标记；`layer` 的弹层挂在 `document.body`，由打开方负责关。
    root.innerHTML = ''
  }
}

export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountPushJdpPage(root, ctx)
}

export default { mount } satisfies IslandPage
