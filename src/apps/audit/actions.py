"""动作登记表：spec §5 标「做」的动作逐条照抄/新拟（来源与对标证据见 `docs/RECON-operation-log.md` §8）；`summary` 由结构化列渲染（spec §5.9/§7.1），调用方不得直传文案。 `ACTION_TEMPLATES` 是文案唯一出处（键集与 `ACTION_LOG_TYPES` 一致、权威条数 = `tests/test_operation_log_model.py` 的 `len` 断言）；
占位符 = 行内列名（spec §5.9：名字类→`target_name`、原/新值→`from_value`/`to_value`、自由文本→`reason` 纯值），⛔ 中文占位符静默渲染成空串。有意偏离与边界（R9 一行一个字段、`order.claim`=10 / `performance.assign`=20 分类照抄对标、一个 code 恰好一条模板、`user.rate_stat` / `order.change_price_no_assign` 不做）见 spec §5.2/§5.9/§11-1 —— ⛔ 不要"顺手修正"。
"""
from apps.audit.models import LogType

# --- §5.1 10 订单相关 ------------------------------------------------------
ORDER_VIEW_CREDENTIALS = "查看订单详情：{order_no}"
ORDER_OPEN_EDIT = "进入编辑订单：{order_no}"
ORDER_CHANGE_TAKER_PRICE = (
    "修改了订单接单价格（原订单价格：{from_value}元，修改后价格：{to_value}元）"
)
# ⭐ 改收款状态（`POST …/payment-status/`）。**新拟**（对标语料无对应 `logType=10` 模板，对标的
# 收款标记走它自己的弹窗），措辞照同组 `order.transition` 句式；`{from_value}` / `{to_value}` 是
# `PaymentStatus` 的**中文 label**（spec §5.1 / §8 第 4 条）。
ORDER_CHANGE_PAYMENT_STATUS = "修改了订单收款状态（{from_value} → {to_value}）"
# ⭐ 改发单价（`POST …/hall-price/`）。**新拟**照同组句式（spec §5.2 未指定文案；语料里的发单价
# 原文没有 from/to 句式，本模板区分性字样在 `.tools/recon/` 零命中）。`{from_value}` / `{to_value}`
# 是**金额**而非中文 label——与 `order.change_payment_status` 刻意不同源；「价是折算的」由 `reason` 承载。
ORDER_CHANGE_OFFER_PRICE = (
    "修改了订单发单价（原发单价：{from_value}元，修改后发单价：{to_value}元）"
)
# ⭐ 占位符是 `{actor_name}` 不是 `{target_name}`：这里的「接单人」就是操作人本人（两条写路径都是
# `target_name=request.user.username`），写 `{target_name}` 会与 `log.py` §3.3 的前缀规则各加一次名字
#（渲染成双前缀）；`target_name` 列照旧填。⛔ 别改用「渲染值==操作人名就不加前缀」的抑制规则（修不到有昵称的人，反例见 spec §3.3）。
ORDER_CLAIM = "{actor_name}从{reason}接手了订单 {order_no}，接手价 {to_value} 元"
ORDER_CLAIM_FROM_DOWNSTREAM = "{target_name}接手了订单 {order_no}，接手价 {to_value} 元"
ORDER_CLAIM_SYNCED_DOWNSTREAM = ORDER_CLAIM_FROM_DOWNSTREAM + "，已同步到下家 {reason} 的系统"
ORDER_CLAIM_TAKER_API = "{target_name}从打手对接接口接手了订单 {order_no}，接手价 {to_value} 元"
ORDER_CREATE = "{actor_name}录入了订单 {order_no}，来源单号：{source_order_no}，发单价 {to_value} 元"
ORDER_UPDATE = "{actor_name}编辑了订单 {order_no}，{field_label}由{from_value}改为{to_value}"
# ⭐ D-0337：「（原因：{reason}）」尾段让弹窗选问题单 / 已暂停 / 已退单时填的原因进日志正文（业务方要求原因自然可见）。
# 空值形态靠渲染器：不带原因的流转 `reason` 是空串，`apps/audit/log.py::render_summary` 在 format 前整段剔除
# ⇒ 与不带尾段的旧句逐字相同（判据：`tests/test_operation_log_model.py` 的模板渲染用例）。
ORDER_TRANSITION = "{actor_name}将订单 {order_no} 的状态从{from_value}改为{to_value}（原因：{reason}）"
ORDER_THROW_TO_HALL = "将订单 {order_no} 抛入接单大厅，发单价：{to_value}"
ORDER_WITHDRAW = "将订单 {order_no} 从接单大厅撤回"
ORDER_CLEAR_TAKER = "清除了订单 {order_no} 的接单人（原接单人：{target_name}）"
# ⭐ 换销售客服归属（业务方拍板「换销售客服 = 提成也换人」）。**新拟**——对标只有「重算提成」按钮（不改归属人），
# 措辞照 `order.clear_taker` 同组句式，`from_value` / `to_value` 是显示名。「原本没有归属人」由写入侧兜底成「（无）」，
# ⛔ 模板层不做这个兜底（渲染输入只能来自行内列；写「未设置」会与「有一个叫『未设置』的人」撞名）。
ORDER_CHANGE_SALES_AGENT = (
    "将订单 {order_no} 的接单销售客服从{from_value}改为{to_value}"
)
# ⭐ 退款（部分退款 / 全额退款 / 退单回池的累计额复位）。**新拟**——对标语料没有退款动作
#（两路否定证据见 `docs/RECON-operation-log.md` §8）；句式照 `order.transition`，
# `from_value`/`to_value` 是 `Order.refund_fee` 的旧 / 新**累计额**。
ORDER_REFUND = "将订单 {order_no} 的累计退款额从 {from_value} 元更新为 {to_value} 元（原因：{reason}）"
# ⭐ 复制成新订单（`POST /api/business/orders/<no>/copy`）。**新拟**——对标没有这个动作；
# spec §8.1 的「例如『由订单 {reason} 复制生成』」是**举例**不是照抄依据。
# 句式照同组的 `order.create`（动词 + 单号），{reason} 装原单号（spec §8.1 明写）。
ORDER_COPY = "复制订单 {reason} 生成新单：{order_no}"
# ⭐ `POST /orders/<单号>/notes/`「添加订单日志」。**新拟**（读码 + 勘察两路证据，见
# `docs/RECON-operation-log.md` §8）：对标的「订单日志」是另一条业务事件流（自由文本 Note），
# 不在操作日志表里，我方也没有那条流的模型 ⇒ 落成新动作；`{内容}` 按 §5.9 落 `reason`（纯值，R10）。
ORDER_NOTE = "添加订单 {order_no} 的日志：{reason}"
ORDER_WECOM_CHAT_LINK = "为订单 {order_no} 确认关联了企微群聊"
ORDER_PENALTY_SUBMIT = "发起罚款审批"
ORDER_PENALTY_APPROVE = "通过罚款审批"

#: ⭐ 自动发货线（spec `2026-10-03-xgj-auto-ship-design.md` §3.6）：闲管家「无需物流发货」的结果写进**订单日志**（业务方 D5：显示位置就是订单日志区）。 **新拟**（证据等级同 `order.note`）；正文 `【{actor_name}】` 照抄对标 `Note` 自带的形状，`{reason}` 只装随结果变化的半截（R10）。
#: ⚠️ 该动作在「系统操作日志」页同时有「操作人=系统」列与正文「【系统】」（对标同形状）；⛔ 不把「系统」写死进模板（重试路径 actor 是登录客服）、 ⛔ 别为去重删正文半截（订单详情页只渲染正文）。
ORDER_SHIP_RESULT = "店铺订单 {order_no} 发货{reason}"

#: ⭐ D-0357：发货闸放行前的「暂不发货」留痕（**新拟**：对标没有拆单组）。正文逐字口径来自
#: spec G6；`X/N`（已完成数/成员总数）经 `{reason}` 进正文（R10 纯值）；操作人**恒**是自动触发的
#: `系统`（闸只挂自动发货口，「重试发货」手工口不经它）。
ORDER_SPLIT_SHIP_HOLD = "【{actor_name}】拆单组未全部完成（{reason}），暂不发货"

#: ⭐ D-0357（G15）：组齐时刻对**主单**的跨单重算遇 `CommissionError` 时吞掉并留的**响亮**日志
#:（**新拟**，对标没有拆单组）。正文逐字口径来自 spec G15；`{order_no}` = **主单号**（日志落主单
#: 名下），`{reason}` = 异常原文（「请到结算页核查」在模板里，不进 `reason` 列）。
ORDER_SPLIT_MAIN_COMMISSION_ERROR = (
    "【{actor_name}】主单客服提成计算失败（主单号 {order_no}）：{reason}，请到结算页核查"
)

ORDER_DELETE = "已将订单 {order_no} 移入回收站（原因：{reason}）"
ORDER_DELETE_WITH_PERFORMANCE = "已将订单 {order_no} 移入回收站，并删除关联绩效记录（原因：{reason}）"
ORDER_DELETE_KEEP_PERFORMANCE = "已将订单 {order_no} 移入回收站，保留绩效记录（原因：{reason}）"

#: 图片存储线：删掉订单上的一张图（**行 + 对象都删、不可恢复**）。
#: ⚠️ 上传 / 认领 / 改头像 / 改收款码**不记**（spec §9.2 的裁定：只给不可恢复的动作留痕）。
ORDER_DELETE_SCREENSHOT = "删除订单截图：{order_no}"

#: ⭐ D-0144：从回收站恢复（对标 `systemlog:10`）。**整句就是模板，没有变量**。
ORDER_RESTORE = "已从回收站恢复订单 {order_no}"

#: ⭐ 订单导出：文案**逐字照抄对标**，登记在 `docs/specs/2026-09-21-system-operation-log-design.md` 的 `order.export` 那一行（对标记在 `10`、`target_kind=operator`、`target_id` = 操作人 IP）。
#: ⚠️ **本仓两个结构化列都不写**（照 `performance.export` 先例）⇒ 与对标口径不同，是 spec §9-11 登记的**有意差异**（理由与代价见 spec §11-Q2 的裁定块）。
ORDER_EXPORT = "从订单管理列表导出了订单数据"

#: ⭐ 交易平台抛单线：把一张本地订单**真发到**代练通。**新拟**——对标没有这个动作可抄（它对交易平台发单是它对上游的事，不进自己的操作日志）。
#: 句式照同组 `order.throw_to_hall`，但**结果**要能一眼看出：`reason` 装结果摘要（成功 / 上游原文 / 状态未知），`to_value` 装代练费
#:（⛔ `reason` 的来源只有 `DailiantongError.message` 或本线中文说明，不装凭据）。
ORDER_DAILIANTONG_PUSH = "将订单 {order_no} 抛到代练通，价格：{to_value}（{reason}）"

#: ⭐ 在代练通上**撤回**一张已经抛出去的单。**新拟**（对标没有代练通的「撤回」动作可抄）；
#: 撤回的两条分支（未接手删除 / 已接手协商）与结果摘要都落 `reason`
#:（⛔ 不装凭据：来源只有上游 `Err` 原文与本线中文说明）。
ORDER_DAILIANTONG_WITHDRAW = "订单 {order_no} 的代练通撤回结果：{reason}"

#: ⭐ 代练通平台动作线：改代练通订单的**代练价格**（只有未接手可改）。**新拟**。
#: `from_value` / `to_value` 装新旧价；`reason` 装回读结果 / 上游原文。
ORDER_DAILIANTONG_PRICE = "修改订单 {order_no} 的代练通价格：{from_value} → {to_value}（{reason}）"

#: ⭐ 代练通平台动作线：给代练通订单**补时**（增量）。**新拟**。
#: `from_value` / `to_value` 装原时限与补后时限（小时）；`reason` 装回读结果。
ORDER_DAILIANTONG_TIME = "给订单 {order_no} 的代练通订单补时：{from_value} → {to_value} 小时（{reason}）"

#: ⭐ 代练通平台动作线：给代练通订单发一条**留言**。**新拟**。
#: 留言正文不进审计（可能很长 / 含业务细节），`reason` 只装结果摘要。
ORDER_DAILIANTONG_MESSAGE = "给订单 {order_no} 的代练通订单发留言（{reason}）"

#: ⭐ 接手后自动把**一张图**（本单群码 / 规则图片）+ **规则话术**发到代练通。**新拟**——对标有「交易平台自动回复打手」，
#: 但没有这条动作的日志原文可照抄。`reason` 只装结果摘要（本单群码 / 规则图片 / 规则话术 / 失败原因）；
#: ⛔ 不装规则正文（用户自由文本）、OSS 对象名（内部标识）、STS 凭据。
ORDER_DAILIANTONG_QR = "订单 {order_no} 的代练通接单说明发送结果：{reason}"

#: ⭐ 按 `PlatformAutoReplyRule` 给打手发话术（可选带图）。**新拟**——对标有「交易平台自动回复」，
#: 但没有这条动作的日志原文可照抄。`reason` 装「哪一档（接单后 / 申请验收后）+ 结果摘要 / 失败原因」；
#: ⛔ 不装规则正文（用户自由文本）、OSS 对象名（内部标识）、任何凭据。
ORDER_DAILIANTONG_AUTO_REPLY = (
    "给订单 {order_no} 的代练通订单自动回复（{reason}）"
)

#: ⭐ 在代练通**确认完单**（`LevelOrderOver`）。**新拟**——对标工具栏有「批量验收」
#:（打 `/OrderAjax/ConfigmTransctionOrder`），但没有可照抄的日志模板原文。
#: 这是**花钱**的动作（完单按订单价扣平台余额），必须留痕；`reason` 只装回读到的远端状态，⛔ 不装凭据。
ORDER_DAILIANTONG_ACCEPT = "订单 {order_no} 的代练通手动验收结果：{reason}"

#: ⭐ 第三方平台订单管理页：**手动同步**一张单的交易状态。**新拟**（对标「同步交易状态」
#: 打 `/OrderAjax/SynchTransctionOrderStatus`，无可照抄模板）。
#: `from_value` / `to_value` 装同步前后的远端状态中文（⛔ 不装凭据 / 上游详情）。
ORDER_DAILIANTONG_SYNC = "同步订单 {order_no} 的代练通交易状态：{from_value} → {to_value}（{reason}）"
#: ⭐ 代练通「有、我们没用上」的动作线：**未接手单加价**。**新拟**。
#: `from_value` / `to_value` 装加价前后的代练价格；`reason` 装回读结果 / 上游原文。
ORDER_DAILIANTONG_UP_PRICE = (
    "给订单 {order_no} 的代练通订单加价：{from_value} → {to_value}（{reason}）"
)

#: ⭐ 代练通「有、我们没用上」的动作线：在代练通上**申请客服介入**（仲裁）。**新拟**。
#: `reason` 装回读到的撤销状态 / 上游原文（⛔ 不装工单正文）。
ORDER_DAILIANTONG_ARBITRATION = "为订单 {order_no} 的代练通订单申请客服介入（{reason}）"

#: ⭐ 代练通「有、我们没用上」的动作线：**评价打手**（`LevelOrderValue`）。**新拟**。
#: `to_value` 装满意度中文；评价正文不进审计。
ORDER_DAILIANTONG_VALUE = "评价订单 {order_no} 的代练通打手：{to_value}（{reason}）"

#: ⭐ 代练通「有、我们没用上」的动作线：代练通订单**上架 / 下架**。**新拟**。
#: `from_value` / `to_value` 装「已上架 / 已下架」。
ORDER_DAILIANTONG_OFF_SELL = (
    "订单 {order_no} 的代练通上架状态由 {from_value} 变为 {to_value}（结果：{reason}）"
)

#: ⭐ 代练通「有、我们没用上」的动作线：订单**锁定 / 解锁**（只有正在代练能锁）。**新拟**。
#: `from_value` / `to_value` 装锁定状态。
ORDER_DAILIANTONG_LOCK = (
    "订单 {order_no} 的代练通锁定状态由 {from_value} 变为 {to_value}（结果：{reason}）"
)

#: ⭐ 代练通「有、我们没用上」的动作线：**拉黑 / 解拉黑打手**（`AddBlackList`）。**新拟**。
#: `target_name` 装打手 UID；⛔ 不装任何联系方式。
ORDER_DAILIANTONG_BLACKLIST = "订单 {order_no} 的代练通打手 {target_name} 黑名单操作结果：{reason}"

#: ⭐ 代练通「有、我们没用上」的动作线：**代练通侧订单要求模板**的增 / 改 / 删 / 设默认。
#: **新拟**。⚠️ 这是**代练通侧的模板**，与本仓自己的「代练要求模板」是两回事。
#: `reason` 装操作与结果（⛔ 不装模板正文：那是用户自由文本）。
ORDER_DAILIANTONG_TEMPLATE = "代练通订单要求模板：{reason}"

#: ⭐ 订单详情页补充动作：把一条**失败 / 已关闭**的推送重发出去。**新拟**——同一次发单
#: 会另写一条 `order.dailiantong_push`（那是「真发单」），本条记的是「谁把哪条旧链路
#: 重发了」：`from_value` / `to_value` 装新旧链路行的状态。
ORDER_DAILIANTONG_REPUSH = (
    "重发订单 {order_no} 的代练通推送：{from_value} → {to_value}（{reason}）"
)

#: ⭐ 订单详情页：给已在代练通的单**补款**（`LevelOrderModify_Special` 的 Kind=0）。**新拟**。
#: `from_value` / `to_value` 装补款前后的代练价格（补款会真的加冻结）。
ORDER_DAILIANTONG_AMOUNT = (
    "给订单 {order_no} 的代练通订单补款：{from_value} → {to_value}（{reason}）"
)

#: ⭐ 订单详情页：编辑代练通订单的**非金额字段**（标题 / 说明 / 要求 / 区服 / 频道 / 标签）。
#: **新拟**。`reason` 装结果摘要，`from_value` / `to_value` 装标题摘要（⛔ 不装长篇正文）。
ORDER_DAILIANTONG_EDIT = "编辑订单 {order_no} 的代练通订单信息（{reason}）"

#: ⭐ 订单详情页：给已在代练通的单**追加一张进度截图**。**新拟**。
#: `reason` 只装结果摘要：⛔ 不装 OSS 对象名（内部标识）、⛔ 不装 STS 凭据。
ORDER_DAILIANTONG_PROGRESS = (
    "给订单 {order_no} 的代练通订单追加进度截图（{reason}）"
)

#: ⭐ **同意撤销**（上游 `LevelOrderCancel(Flag=2)`）——打手在代练通申请撤销后，我方同意。
#: **新拟**：对标没有这条可抄。`reason` 装回读到的撤销状态 + 实际交给上游的两个金额
#: （详情为空按 0 传时写明）；⛔ 不装凭据 / 支付密码（`PayPass` 由客户端现算）。
ORDER_DAILIANTONG_AGREE_CANCEL = "同意订单 {order_no} 的代练通协商撤销（{reason}）"

#: ⭐ 代练通已无活跃订单（全部推送结束）⇒ 清掉占位接手人「代练通」。**新拟**（对标没有这一
#: 动作）。机器行（`SYNC_ACTOR`，`actor_id=None` + `system=True`）；`target_name` 装被清掉的
#: 占位账号显示名，`reason` 固定为「代练通已无活跃订单（全部结束）⇒ 清除占位接手人」。
ORDER_DAILIANTONG_CLEAR_TAKER = (
    "订单 {order_no} 的代练通跟单已结束，已清除系统占位接手人 {target_name}"
)

#: ⭐ D-0348：**订单群码自动拉取**（对接「马上来 self-app」查码接口）。**新拟**（对标没有「自动查码落库」这件事）。机器行（`order_qr_fetch._record_ledger`，`actor_id=None` + `system=True`，actor=「马上来查码」）。⚠️ `reason` 只装**结果摘要与 host**：⛔ 不装查码密钥、
#: 不装 `qr_url` 全文（临时地址，落台账只会误导人去拼它）；成功与失败（含 401 闩沿 / 网络 / 对端 500）都走本码，静默态**不**产生本行（口径见 `order_qr_fetch`）。
ORDER_MALAILAI_QR_FETCH = "自动拉取订单 {order_no} 的群码{to_value}（原因：{reason}）"
QR_FETCH_SUCCESS_REASON = "自动拉取了本单群二维码"

#: ⭐ 我方打手接下**镜像单**（鸡蛋铺挂出来的单）。**新拟**（对标没有这个动作）。⚠️ 它记的是 **我方**这一次接手（转正后的那笔业务），与鸡蛋铺侧那笔占住是**两件事**（后者不进我方日志）；
#: 占住走**抢单**（`RecOrderByInternal`，⛔ 不发任何金额），远端由**登录账号**的会话身份持单（真机实测：单记在登录账号名下、发单价原样保留）。
ORDER_JIDANPU_REMOTE_CLAIM = "接下了鸡蛋铺大厅的订单：{order_no}（远端单 {reason}）"

#: ⭐ 鸡蛋铺打手接了我方抛出的单 ⇒ **只按远端标识记账、不算钱**。**新拟**（对标的「接单」是它
#: 自己大厅里的事）。⚠️ `{target_name}` 是「远端显示名（远端 uid）」，`{reason}` 是**远端标识**
#:（⛔ 不含凭据、不含远端整行）。
ORDER_JIDANPU_REMOTE_TAKEN = "鸡蛋铺打手接了订单：{order_no}（{target_name}）"
#: ⭐ 我方打手接了自家抛出去的单 ⇒ 去鸡蛋铺**撤下 / 退单 + 原因**。**新拟**（对标没有「把单
#: 推给别人又撤下」这件事）；`{reason}` 是退单原因（纯值）。
ORDER_JIDANPU_WITHDRAW = "订单 {order_no} 已从其他下家撤下"

#: ⭐ 下家鸡蛋铺打手在他家大厅接单 ⇒ 我方**自动接手**给持有绑定的代练员（spec §6.1 / §8.2 A6）。 **新拟**。⚠️ 与 `order.jidanpu_remote_taken` 刻意不同名不同义：那条「只记账、不算钱」（钱在
#: 鸡蛋铺发），本条「真接手、钱在我方按绩效算」，共用一个 code 会把两件事混掉。机器动作：调用方把 actor 写成机器名、`actor_id` 留空；`{reason}` 只装接手出处的人话短句（纯值）。
ORDER_JIDANPU_MEMBER_AUTO_CLAIM = (
    "由下家鸡蛋铺接单自动接手订单：{order_no}，接单人：{target_name}，"
    "接手价：{to_value}（远端接单人 {reason}）"
)

#: ⭐ 下家收到上家撤单事件，把本地那张单改成已退单并留痕（spec §16.2 实测补充）。模板**逐字** 取自 spec §16.2（`{actor_name}` = 事件级显示名、`{to_value}` = 已退单、`{reason}` =
#: `WITHDRAW_REASON_TEXT` 的逐字中文）。机器来源 ⇒ 调用方 `actor_id` 留空； `record()` 没有 `summary` 形参，⛔ 调用方不许拼串。
ORDER_PEER_WITHDRAW = "【{actor_name}】将订单状态改为：{to_value}，原因：{reason}"

#: ⭐ 抢单转正把订单发单价**覆盖**为本层展示价、且差值大于 1 分时留的那行账（落点 `apps/business/peer_inbound.py::_promote_claiming_mirror`；**新拟**——对标没有「跨租户大厅转正」，句式照 `order.change_offer_price` 同族）。⚠️ **单独一条码**：这是「同步
#: 机器动作**否决**了上家带来的价格」的那一档，要与人工改价、上家改价留痕（`UpstreamPriceChangeLog`）分开筛——价差可能是钓鱼价差 / claim→assign 窗口改价。机器动作 ⇒ `actor_id` 留空、`system=True`；值只装纯值（R10）。
ORDER_PEER_PROMOTE_PRICE_OVERRIDE = (
    "订单 {order_no} 接手后，发单价由上家价格 {from_value} 元调整为本店大厅展示价 {to_value} 元"
)

#: ⭐⭐ 团团通↔团团通同步落库的 9 条机器行：前缀 `peer.`、`log_type=40`（非 `order.` 前缀不得登记成 10，结构网钉着；本组裁定**不进**
#: 「交易订单操作记录」页，订单详情页照常可见）。模板**自带** `{actor_name}` ⇒ 渲染器不再加 `【】` 前缀；机器来源 ⇒ `actor_id` 留空 + `system=True`。
#: ⚠️ **一条事件一行**（跳过与幂等重投⛔ 不写，判据在 `apps/business/peer_inbound.py`）；撤单**沿用** `order.peer_withdraw`，⛔ 不新开同名动作。
PEER_INBOUND_ORDER = "从上家「{actor_name}」同步：已录单（{order_no}）"
PEER_INBOUND_ASSIGN = "上家「{actor_name}」同步指派订单：{order_no}"
PEER_INBOUND_GRAB = "上家「{actor_name}」同步接单：{order_no}"
#: ⭐ D-0364：扩成带状态段——让订单详情能看出「状态从什么被改成什么」。`from_value` 由更新支传 **本地旧状态** label，状态没被这条事件改时（R3 / 池内豁免）传空 ⇒ 渲染器整段剔除
#: （`audit/log.py` 同款空值剔除），⛔ 不留「（状态： → 代练中）」空壳；「状态保持」走 **reason 列** （取值见 `peer_inbound.PEER_INBOUND_STATUS_HELD_NOTE`），空值同理整段剔除。
PEER_INBOUND_UPDATE = (
    "上家「{actor_name}」更新了订单：{order_no}"
    "（状态：{from_value} → {to_value}）（原因：{reason}）"
)
#: `{to_value}` = `新增` / `删除`（两个 kind 共用一条码：它们都只是「订单截图变了」）。
PEER_INBOUND_SCREENSHOT = "上家「{actor_name}」{to_value}了订单截图：{order_no}"
#: 收单（`hall.take` 带 `leave_reason="assigned"` 把已转正、仍挂本方大厅的正式单收回归池）成功后的
#: 一行（落点 `peer_inbound._collect_assigned_hall_orders`；对标没有这一档，句式照同族）。
PEER_INBOUND_HALL_COLLECT = "上家「{actor_name}」把单指派给内部打手，转正单已收回归池：{order_no}"
#: 上行：下家在我方大厅接单（`hall.claim` 被受理、订单真被机器接手）。
PEER_HALL_CLAIM = "下家「{actor_name}」在我方大厅接单：{order_no}"
#: 上行：下家回传订单状态（只收 `UPSTREAM_STATUS_MAP` 的值域）。
PEER_UPSTREAM_STATUS = "下家「{actor_name}」回传订单状态：{order_no}（{to_value}）"
PEER_UPSTREAM_SCREENSHOT = "下家「{actor_name}」{to_value}了订单截图：{order_no}"
#: ⭐⭐ D-0364：出站同步「**已同步给谁**」的机器行——事件真投递成功时订单时间线此前没有行 ⇒ 本行补上；写点在 `peer_worker._attempt` 落 `done` 处（仅 `done` 且 `reason==""`：有意作废与 failed / 重试中间态不写，防退避梯子刷屏）。
#: `{to_value}`=方向词（不是状态）、`{target_name}`=对方可读名（`_peer_counterparty_name` 口径，⛔ 不新造名字）、`{reason}`=事件短句（`peer_worker._OUTBOUND_SYNC_SENTENCES`）；模板不带 `{actor_name}` ⇒ 渲染器照常加 `【系统】` 前缀；前缀/分类同族裁定，⛔ 不进 `ORDER_ACTION_LABELS`。
PEER_OUTBOUND_SYNC = "已同步给{to_value}「{target_name}」：{reason}"

#: ⭐ 闲管家推送接收端（spec `2026-10-04-xgj-push-inbound-design.md` §D5 / R5）：订单推送（23 已退款 / 24 已关闭）把单自动改成已退单时的**带原因人话**。**新拟**（对标不接闲管家推送，
#: 语料无退单原文，两路否定证据同 `ORDER_REFUND`）；随平台状态变化的半截放 `{reason}`（R10）。 ⚠️ 同一笔成功退单还会记一条既有 `order.transition` ⇒ 一次推送在订单日志区**两行**（spec §6-2 如实登记的代价）。
ORDER_XGJ_PUSH_REFUND = "该订单在平台已{reason}，系统已自动改为已退单"

#: ⭐ 同一线（spec §D6 / R7 / R9）：这一档**改不动**（该单绩效已结账）⇒ 状态一个字不改，只记一条
#: **独立动作码**让「系统操作日志」页能单独筛给人工；正文点名「未自动改为已退单」与下一步——
#: 人工路径是既有的「先撤销结账 → 再改状态」（`order_status.SETTLED_STATUS_CHANGE_MESSAGE` 指的就是它）。
ORDER_XGJ_PUSH_REFUND_SETTLED = (
    "该订单在平台已{reason}，但绩效已结账，未自动改为已退单；请先撤销结账后人工处理"
)

#: ⭐ 闲管家推送**收款**分支（spec `2026-10-22-xgj-receipt-detail-and-finance-merge-design.md` §4-D3）：订单推送（`order_status = 22` 交易成功）把收款状态自动改成「已结款」（`PaymentStatus.SETTLED`，
#: 不是绩效的结账）。**新拟**，句式照同族 `ORDER_XGJ_PUSH_REFUND`；⚠️ 同一笔成功改款还会有一条既有 `order.change_payment_status` ⇒ 一次推送一张单**两行**日志（与退款线两行同形）。
ORDER_XGJ_PUSH_RECEIPT = "平台显示交易成功，已将订单 {order_no} 的收款状态更新为已结款"

#: ⭐ D-0359：系统主动**向上家**问「这单结没结账」的机器留痕（每问一次一条；①式 / peer 两通道统一，闲管家通道沿用 `order.xgj_push_receipt` 不重复记）。`reason` 承载结构化 detail（通道 /
#: 绑定 / 结局 / 上游原始字段 JSON；唯一写点 `apps/business/upstream_receipt_sync._write_query_log`）；翻状态本身由 `change_payment_status` 另落一行，本行只回答「问了、上家怎么说」。
RECEIPT_QUERY_RESULTS = {
    "settled": "已结款",
    "not_settled": "未结款",
    "not_found": "上家没有查到订单",
    "ambiguous": "找到多笔同号订单，结果待核实",
    "bind_error": "查询失败，上家账号不可用",
    "error": "查询失败，上家系统暂不可用",
    "skipped_no_key": "缺少上家查询单号",
    "skipped_no_link": "缺少可用的上家订单关联",
    "precheck_failed": "上家查询配置不可用",
}

ORDER_UPSTREAM_RECEIPT_QUERY = "查询订单 {order_no} 的上家收款状态：{to_value}"

# --- §5.2 20 绩效相关 ------------------------------------------------------
PERFORMANCE_ASSIGN = "{actor_name}将订单指派给{target_name}，发单价：{to_value}"
PERFORMANCE_REASSIGN = (
    "{actor_name}将订单重新指派给{target_name}，发单价有变化，原来发单价：{from_value}，"
    "新发单价：{to_value}"
)
# ⚠️ R9：有意偏离对标原句（见模块 docstring），一行一个字段
PERFORMANCE_ADJUST = "修改了{target_name}的{field_label}（原{from_value}，新{to_value}）"
# ⭐ 同 `ORDER_CLAIM`——「交单人」就是本行的操作人（`/submit/` 的
# `target_name=request.user.username`）；写成 `{target_name}` 会与「模板自带名字」各加一次
# （名字出现两次），`target_name` 列**照旧填**。
PERFORMANCE_SUBMIT = "{actor_name}提交了订单 {order_no} 的完单，绩效金额：{to_value}"
PERFORMANCE_REVIEW = "{actor_name}审核了订单 {order_no} 的绩效：{to_value}，备注：{reason}"
# ⭐ 绩效结账**照抄**对标原句（RECON §1.4 绩效相关），⛔ **逐字照抄，不要改词**：对标原文 =
# `修改了{人}的绩效清算状态为：{状态}(仅更改结算状态)`；变量映射 {人} → `target_name`（R11）、
# {状态} → `to_value`。
PERFORMANCE_SETTLE = "已将{target_name}的绩效结账状态标记为{to_value}（仅标记状态）"
ONLINE_PAYROLL_SETTLE_FIELD = "settlement_method"
ONLINE_PAYROLL_SETTLE = "已向{target_name}在线支付绩效款"
# ⭐ 财务**新增一条调整记录**的留痕。**新拟**（对标无此动作，证据见 `docs/RECON-operation-log.md` §8）。 ⚠️ **不是** `performance.adjust`：那条改绩效**既有字段**（一行一个字段），本动作新增一条
# `PerformanceAdjustment` 记录（`kind=MANUAL` 或退单回池的反向冲销）；人工修正路径 `from_value` 是空串——「变更前：」后为空是**如实**，不是渲染 bug（回池路径上是冲销前的链余额）。
PERFORMANCE_ADJUST_MANUAL = (
    "新增了{target_name}的绩效调整记录（变更前：{from_value}，变更后：{to_value}），"
    "备注：{reason}"
)
# ⭐ 绩效**删除 / 导出**：文案**逐字照抄**对标（原文、脱敏说明与变量映射见 `docs/RECON-operation-log.md` §8）。
# ⚠️ `EXECL` 是**对标原文的拼写**，⛔ 不许"修正"成 `EXCEL`；「结账状态：{reason}对应订单号」之间
# **没有逗号**是对标原文的形状，不要"补全"。
PERFORMANCE_DELETE = (
    "删除绩效记录，该绩效用户：{target_name}，预计绩效金额：{from_value}元，"
    "生效绩效金额：{to_value}元，原结账状态：{reason}，订单 {order_no}，"
    "对应绩效ID：{target_id}"
)
PERFORMANCE_EXPORT = "已将绩效和提成记录导出为 Excel"
SETTLEMENT_EXPORT = "导出了结算明细"
#: ⭐ D-0365：**我的订单收入**导出。**新拟**——对标「我的订单」页的导出（`/Emp/ExportMyEmp`）直接落文件，没有日志原文可照抄。⚠️ ⛔ 不复用 `performance.export`：那条是财务从**绩效管理页**
#: 导出记录，本动作是打手从**自己的收入页**导出**自己的**账单——受众与数据范围都不同，借同一个码会让日志把「谁从哪里导了什么」说成另一件事。归**既有**分类 `LogType.PERFORMANCE`(20)。
MY_ORDERS_EXPORT = "从「我的订单收入」页导出了收入记录"
#: ⭐ 订单批量线：**重算客服销售提成**。**新拟**——对标没有这个动作（找不到埋点，spec §2.4-U1），「借用」语义不同的既有键会让日志**说假话** ⇒ 业务方点名授权新增（Q2 = 候选 A），归既有分类 20。
#: ⚠️ 文案与前端确认框（§5.5 逐字取对标「生成或更新客服销售绩效账单」）同义，覆盖两档：**已完成单**= 「重算并更新」、**未完成单**=「只补建空行、金额不动」（spec §D15 第 5 条）⇒ 用「生成或更新」中性说法，⛔ 不谎称「已重算」。
PERFORMANCE_RECOMPUTE_CUSTOMER_COMMISSION = "生成或更新了订单 {order_no} 的客服销售绩效账单"

# --- §5.3 30 用户相关 ------------------------------------------------------
USER_CREATE = "新增用户(ID：{target_id}，用户名：{target_name})的资料"
USER_UPDATE = "编辑用户(ID：{target_id}，用户名：{target_name})的资料"
USER_RENAME = "已将登录账号由 {from_value} 改为 {to_value}"
USER_GAME_PERMISSION_ADD = "新增用户游戏权限：{game_name}"
USER_GAME_PERMISSION_REMOVE = "删除用户游戏权限：{game_name}"
USER_ROLE_CHANGE = "更改了{target_name}的职位为：{to_value}"
USER_STATUS = "更改了用户状态，将{target_name}的子账户状态从{from_value}变成{to_value}"
USER_STATUS_BATCH = "更改了用户状态，将{from_value}个子账户批量改为{to_value}"
USER_RESET_PASSWORD = "已将{target_name}的登录密码重置为初始密码"
#: `user.self_update`（spec §5.1 bid 121 同族动作）。模板**逐字照抄**对标；唯一槽位 `{渠道}` 按 spec §5.9 落 `reason`。⚠️ 本动作的值只有「系统网页」一种；`order.claim` 还有「打手App」
#: （值域唯一出处 `apps/business/claim_channel.py`），⛔ 别把两者当同一套值。一行记一次**编辑事件** （模板没有字段槽位，逐字段多行完全相同 = 噪声）；⛔ 字段值（收款账号 / 银行卡号这类 PII）不进 `reason`。
USER_SELF_UPDATE = "编辑了自己的子账号基本资料，修改渠道：{reason}"
#: **照抄**（spec §5.3 的 `user.delete` 行）：对标原文是一句**完整快照**（登录账号、手机号、职位、证件号等十三个值；语料见 `docs/RECON-operation-log.md` §「删除」）。本设计只有 5 个自由列放不下 ⇒ 按 §5.9：固定句式与「该用户登录账号：」**逐字照抄**，其余快照原样落 `reason`（自由文本列）。
#: ⛔ 手机号 / 证件号 / 结算支付宝账户与姓名**不写**（PII 纪律）；保证金 / 未结账单总额 / IP **不写**（无列）。实际写进 `reason` 的是「登录账号 + 职位 + 标记 + 备注」四项。
USER_DELETE = "删除系统用户：{target_name}，该用户登录账号：{reason}"
#: **新拟**（对标**没有**回收站 ⇒ 没有这个动作）：按 spec §5.3 的「新拟」规则照同组句式写。
USER_RESTORE = "恢复系统用户：{target_name}，该用户登录账号：{reason}"
#: ⭐ 实名认证线：用户**自己**提交一次实名核验。**新拟**——对标的实名页只给管理员看，用户侧那次提交没有对应的日志动作。⚠️ **槽位与 PII 纪律**（spec §7 / §12）：`{target_name}` =
#: **登录账号快照**（`RealNameCertRecord.user_name_snapshot`，不是姓名）；`{reason}` = **脱敏后** 的证件号（`apps/realname/views.py::mask_id_card`，前 3 后 2）；⛔ 完整证件号一个字都不进模板、不进 `reason`。
USER_REALNAME_VERIFY = "已提交{target_name}的实名认证核验，证件号（已脱敏）：{reason}"
#: ⭐ 实名认证线：管理员**重置**某个子账号的实名。**新拟**——对标那个端点
#:（`/UserAjax/ResetRnameCert`）在语料里没有对应动作。`{target_name}` = 被重置的**登录账号**；
#: `{reason}` 由调用方给一句纯值说明，⛔ 不把证件号塞进 `reason`（同上，PII 纪律）。
USER_REALNAME_RESET = "重置了{target_name}的实名认证：{reason}"
#: ⭐ D-0253：人事**作废**一条自助注册链接（立即过期）。**新拟**——对标没有「注册链接」这个
#: 功能。「是哪条链接」由 `target_kind` / `target_id` 承载；`{reason}` 装**生成时刻**（纯值，
#: 「生成于：」标签由模板正文提供）。⛔ `target_name` 留空：链接没有「名字」，塞人或时间都是假陈述。
USER_SELFREG_REVOKE = "作废了一条自助注册链接（生成于：{reason}）"

# --- §5.4 40 系统相关 ------------------------------------------------------
SYSTEM_ROLE_UPDATE = "编辑角色权限：{target_name}，原职位名称：{from_value}"
SYSTEM_ROLE_DESTROY = "删除角色职位：{target_name}"
SYSTEM_ROLE_CREATE = "新增角色职位：{target_name}"
SYSTEM_ROLE_COPY = "复制角色职位：{from_value} → {to_value}"
SYSTEM_GAME_CREATE = "新增游戏类：{target_name}"
SYSTEM_GAME_DESTROY = "删除游戏：{target_name}"
SYSTEM_GAME_UPDATE = "编辑游戏类：{target_name}"
SYSTEM_SERVER_CREATE = "新增区服：{target_name}"
SYSTEM_SERVER_UPDATE = "编辑区服：{target_name}"
SYSTEM_SERVER_DESTROY = "删除区服：{target_name}"
# ⭐ 订单来源 CRUD。**照抄**对标语料（RECON §1.4「系统相关」；`.tools/recon/oplog/templates.json`
# 的 `systemlog:40`）。⚠️ **全角 `：`（U+FF1A）与 `，`（U+FF0C）逐字照抄，不要改标点**；变量映射
# 按 spec §5.9：{名}→`target_name`、{排序}→`to_value`、{备注}→`reason`。删除句**不含排序**——对标原文如此，不许"补全"。
SYSTEM_ORDER_SOURCE_CREATE = "新增订单来源：{target_name}，排序：{to_value}，备注：{reason}"
SYSTEM_ORDER_SOURCE_UPDATE = "编辑订单来源：{target_name}，排序：{to_value}，备注：{reason}"
SYSTEM_ORDER_SOURCE_DESTROY = "删除订单来源：{target_name}，备注：{reason}"
# ⭐ 公司店铺分组（计划 `docs/plans/2026-10-26-order-source-groups.md` §3.2）。**新拟**——对标 logType=40 的三类枚举没有分组动作，句式照同组；⚠️ **槽位只有「分组」**（分组模型不建
# `remark`），⛔ 别照抄来源句的 `{备注}`、也别凭空补 `{排序}`（那是来源自己的槽位）；分组**改名**用 `from_value` 记旧名（来源侧留空 `from_value` 是照对标原句，分组侧不沿用）。
SYSTEM_ORDER_SOURCE_GROUP_CREATE = "新增公司店铺分组：{target_name}"
SYSTEM_ORDER_SOURCE_GROUP_UPDATE = "编辑公司店铺分组：{target_name}，原名称：{from_value}"
SYSTEM_ORDER_SOURCE_GROUP_DESTROY = "删除公司店铺分组：{target_name}"
# ⭐ 店铺对接 CRUD。**新拟**（对标 logType=40 的三类枚举无店铺对接动作；两路证据见
# spec §5.4）。句式照同组的 `system.order_source_*`，`平台` 顶替订单来源的 `排序` 槽位
# （店铺对接没有排序字段）。⚠️ **授权码 `access_token` 一律不进模板**（见 §5.9 的凭据纪律）。
SYSTEM_SHOP_BINDING_CREATE = "新增店铺对接：{target_name}，平台：{to_value}，备注：{reason}"
SYSTEM_SHOP_BINDING_UPDATE = "编辑店铺对接：{target_name}，平台：{to_value}，备注：{reason}"
SYSTEM_SHOP_BINDING_DESTROY = "删除店铺对接：{target_name}，备注：{reason}"
# ⭐ 闲管家店铺对接 CRUD。**新拟**（对标 logType=40 的枚举无闲管家对接动作 ⇒ 我方独有对接）。
# 句式照同组 `system.shop_binding_*`；闲管家那张表没有「平台」列（只对接闲鱼一家）⇒ 只渲染
# 名 + 备注。⚠️ **`app_secret` 一律不进模板**（凭据纪律，spec §9-S1 同族）。
SYSTEM_XGJ_BINDING_CREATE = "新增闲管家店铺对接：{target_name}，备注：{reason}"
SYSTEM_XGJ_BINDING_UPDATE = "编辑闲管家店铺对接：{target_name}，备注：{reason}"
SYSTEM_XGJ_BINDING_DESTROY = "删除闲管家店铺对接：{target_name}，备注：{reason}"

# ⭐ per-source 线：「同步店铺」是**写端点**——把上游授权列表幂等落成 `XgjShop` 行 + 自动生成的 `OrderSource`（R5/R7）。它**不改这一行本身**，借用 `_update` 会是一句假陈述 ⇒ 单独一条码，
# 句子只说"同步"与结果计数。⚠️ 保存时那次**自动**同步不记（那条路自己的 `_create`/`_update` 已经记了），只记**手动点了「同步店铺」**这一次。
SYSTEM_XGJ_BINDING_SYNC_SHOPS = "同步闲管家店铺：{target_name}，备注：{reason}"

# ⛔ 租户级鸡蛋铺线的两条动作码已成历史：产生它们的视图已随
# `docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md` 拆除 ⇒ **不再可能产生**；
# 历史台账行照旧可读（动作码是字符串、库里不做校验）。

# ⭐ 下游鸡蛋铺线：代练员保存**自己那份**下家绑定。**新拟**——沿用已拆除的 `system.jidanpu_binding_*` 同一路证据与句式；⚠️ **刻意不同名**：本线那张表与租户级那条线零
# 关系（spec §4.1），日志要分得清。⚠️ **密码一律不进模板**（spec §8-S1）：`{target_name}` 放 **站点地址**（去协议头），不是账号、更不是密码（`jidanpu_member_views._member_binding_name`）。
SYSTEM_JIDANPU_MEMBER_BINDING_CREATE = "新增代练员的下家鸡蛋铺绑定：{target_name}"
SYSTEM_JIDANPU_MEMBER_BINDING_UPDATE = "编辑代练员的下家鸡蛋铺绑定：{target_name}"

# ⭐ 上家鸡蛋铺线：客服保存**自己那份**上家打手账号绑定。**新拟**——同组同一路证据与句式； ⚠️ **刻意不同名**：三张表零关系（spec §4.1），日志要分得清。⚠️ **密码一律不进模板**
# （spec §8.3）：`{target_name}` 放**站点地址**（去协议头），不是账号、更不是密码（`jidanpu_upstream_views._upstream_binding_name`）。
SYSTEM_JIDANPU_UPSTREAM_BINDING_CREATE = "新增上家鸡蛋铺绑定：{target_name}"
SYSTEM_JIDANPU_UPSTREAM_BINDING_UPDATE = "编辑上家鸡蛋铺绑定：{target_name}"

# ⭐ 绑定上家系统设置页：统一列表里删掉一条上家绑定（真删，级联口径由模型 `on_delete` 决定）。 **新拟**——同组同一路证据与句式（对标枚举没有「删绑定」类动作）。⚠️ 模板照同组句式带
# `：{target_name}`：删除**必须**说清删的是哪一条（站点地址去协议头，同源函数）； ⚠️ **密码 / 上家子账号一律不进模板**（计划 §1 的凭据红线）。
SYSTEM_JIDANPU_UPSTREAM_BINDING_DELETE = "删除上家鸡蛋铺绑定：{target_name}"

# ⭐ 录单识别设置线：编辑识别关键词。**新拟**——对标 logType=40 的三类枚举里没有它，
# 用同组句式新写；`{target_name}` 是识别项**名称**、`{to_value}` 是新关键词串
# （spec §3-D5 / §5.2）。⚠️ `item_code` 走 `target_id`，不进句子。
SYSTEM_RECOGNITION_KEYWORDS_UPDATE = "修改了录单识别项「{target_name}」的关键词：{to_value}"

# ⭐ 系统开关线（D-0223）：改一个系统设置开关（Y/N）。**新拟**——对标 logType=40 的三类
# 动作枚举里没有它，句式照同组 `system.*`。`{target_name}` 是开关的页面文案、
# `{from_value}` / `{to_value}` 是改前 / 改后的 `"N"` / `"Y"`。⚠️ 开关的 key 走 `target_id`，不进句子。
SYSTEM_SYSTEM_SWITCH_UPDATE = "修改了系统设置开关「{target_name}」：{from_value} → {to_value}"

# ⭐ 交易平台对接设置线：账号三条写动作 + 全局设置一条。**新拟**（对标枚举里没有这类动作）。 **为什么必须留痕**：账号里装着 `app_secret` / 支付密码摘要，是凭据面，「谁在什么时候新增 /
# 改过 / 删过哪个账号」事后要能回答（同 `system.open_api_app_*` 的纪律）。 ⛔ **值一概不进日志**：模板只有 `{target_name}`（平台账号），密钥 / 支付密码明文 / md5 一个字都不进 `record(...)`。
SYSTEM_PLATFORM_INTEGRATION_CREATE = "新增交易平台对接账号：{target_name}"
SYSTEM_PLATFORM_INTEGRATION_UPDATE = "编辑交易平台对接账号：{target_name}"
SYSTEM_PLATFORM_INTEGRATION_DESTROY = "删除交易平台对接账号：{target_name}"
SYSTEM_APPROVAL_SETTINGS_UPDATE = "修改了审批中心配置"
SYSTEM_PLATFORM_SETTINGS_UPDATE = "修改了交易平台发单全局设置"

#: ⭐ D-0348：马上来查码配置的保存（PATCH，改凭据与启停——照 QQ 机器人设置页的口径：
#: 改凭据的动作**必须**记台账，⛔ 不进「刻意不记」）。
SYSTEM_ORDER_QR_CONFIG_UPDATE = "修改了马上来查码配置"

# ⭐ 交易平台自动回复线：规则三条写动作。**新拟**（对标枚举里没有「自动回复规则」），句式照
# 同组 `system.*`。`{target_name}` = **游戏名（或「全部游戏」）+ 时机文案**，⛔ **不是**规则正文
#（`content` 是用户自由文本：抄进日志等于复制到另一张表，也容易撞 `target_name` 长度上限）。
SYSTEM_PLATFORM_AUTO_REPLY_CREATE = "新增交易平台自动回复：{target_name}"
SYSTEM_PLATFORM_AUTO_REPLY_UPDATE = "编辑交易平台自动回复：{target_name}"
SYSTEM_PLATFORM_AUTO_REPLY_DESTROY = "删除交易平台自动回复：{target_name}"
# ⭐ 规则图片上传：`POST platform-auto-replies/upload-image/`。**新拟**（同一证据口径）。
# **为什么单独一条**：它**不写规则行**（只往对象存储落一个对象）⇒ 折进 `_update` 会让「谁传了图」
# 与「谁改了规则」分不开。⛔ 对象名 / 文件内容 / 文件名一个字都不进日志（模板里没有任何占位符）。
SYSTEM_PLATFORM_AUTO_REPLY_IMAGE_UPLOAD = "上传了交易平台自动回复图片"

# ⭐ QQ 机器人线（D-0265）：保存机器人凭据 / 开关（`PATCH api/qqbot/binding/`）。**新拟**——对标没有机器人对接页。为什么记：改的是**凭据与启停**，「谁在什么时候换过 AppID / secret / 动过启用开关」事后必须能回答；「保存后连没连上」由 `last_error` / `connection_state`（ws 进程
# 写入方）承载，与「改过配置」是两件事。⛔ **值一概不进日志**（`app_secret` 只写不读）：被改的字段名走 `field_name` 列，`from_value` / `to_value` 留空。
QQBOT_BINDING_UPDATE = "修改了 QQ 机器人配置"

# ⭐ 企微自建应用线（D-0379）：保存企微凭据 / 开关（`PUT api/wecom/config/`）。**新拟**——对标没有企微对接页；
# 句式与凭据纪律照同组 `system.qqbot_binding_update`（改的是凭据与启停，被改字段名走 `field_name` 列，
# ⛔ secret / aes_key 的值一概不进日志）。⚠️ 与 wechatmp 配置 PUT 的「刻意不记」是两条线的不同裁定，各自留痕。
SYSTEM_WECOM_CONFIG_UPDATE = "修改了企微自建应用配置"
SYSTEM_WXPUSHER_CONFIG_UPDATE = "修改了消息推送订阅配置"
SYSTEM_INTERNAL_CHAT_DIRECTORY_UPDATE = "修改了内部沟通通讯录"
SYSTEM_INTERNAL_CHAT_VISITOR_AVAILABILITY = "将访客接待状态从{from_value}改为{to_value}"
SYSTEM_INTERNAL_CHAT_VISITOR_TRANSFER = "将访客会话 {target_id} 转接给员工 {to_value}"
SYSTEM_INTERNAL_CHAT_VISITOR_CLOSE = "结束了访客会话 {target_id}"
SYSTEM_INTERNAL_CHAT_NOTICE_SETTINGS_UPDATE = "修改了内部沟通系统通知规则"

# ⭐ 上下游订单互通线：**我方独有**的页面写端点。**新拟**——沿用 `system.xgj_binding_*` 同一路证据（对标那一页 `/System/homesystemsetting` 自己不做操作日志），句式照同组 `system.*`。 ⚠️ **两个令牌与握手密码一律不进模板**（spec §9-S4 的凭据纪律）。⚠️ 这两条只覆盖**页面**端点：
# 两个机器同步端点（`peer-sync/bind/` / `peer-sync/unbind/`）按「机器对机器、没有页面操作人」登记进 `tests/test_operation_log_ledger.py` 的 `DELIBERATELY_NOT_LOGGED`。
SYSTEM_PEER_BINDING_CREATE = "{actor_name}绑定了上家系统{target_name}"
SYSTEM_PEER_BINDING_DELETE = "{actor_name}解除了与上家系统{target_name}的绑定"
#: ⭐ 大厅镜像：「同步上家抢单大厅」开关 / 「大厅价格百分比」两格的修改。**新拟**——沿用同组 `system.peer_binding_*` 同一路证据；⚠️ 换 `enabled`（订单同步）是既有概念，这两格是**新概念**
#: （接收侧大厅镜像）⇒ 单独一条码，⛔ 不并进 `_create` / `_delete`（那两条各自只描述一次握手 / 解绑）。 `{to_value}` 是**纯值**说明（如「同步抢单大厅=开启，价格百分比=90」，R10）；⛔ 两个令牌 / 握手密码不进模板。
SYSTEM_PEER_BINDING_UPDATE = "{actor_name}修改了上家系统{target_name}的大厅同步设置：{to_value}"

# ⭐ 上下游订单互通 P2：上家侧连续投递失败到阈值（`>= 10`）⇒ **自动停绑**（spec §16.6 第 4 条 / R17：阈值写死、无界面可调）。**新拟**——同一路证据；这条**必须**是一条操作日志
# （`record()` 刻意没有 `summary` 形参 ⇒ 调用方不许拼串）。⚠️ `{target_name}` 放**下家域名** （下游绑定表没有"名字"列），`{reason}` 是人话原因「连续失败 N 次已自动停止推送」；令牌与握手密码不进模板。
SYSTEM_PEER_BINDING_AUTO_REVOKE = "自动停止了向下家系统{target_name}推送订单：{reason}"

# ⭐ 微信公众号集成线（D-0268）：账号绑到 / 解绑公众号 openid。**新拟**——对标没有「公众号
# 绑定」⇒ 句式照同组 `system.*` 写。⚠️ **openid 不是密钥，但仍截短进 `{reason}`**（对账线索；
# 完整 openid 可从绑定行查，不必进日志）。绑定是账号安全动作，值得留痕。
SYSTEM_WECHATMP_BINDING_CREATE = "绑定了微信公众号（openid 前缀：{reason}）"
SYSTEM_WECHATMP_BINDING_DELETE = "解除了微信公众号的绑定"

# ⭐ 开放平台应用管理线：创建 / 改 / 轮换密钥三条（**新拟**；**证据等级 = 推断**——对标没有「给第三方开凭据」这件事 ⇒ 句式照同族写：动作 + 对象名 + 关键值）。为什么必须留痕：创建一把能读 **全部订单 + 成本 + 客户联系方式 + 订单原文**的第三方凭据，必须可审计（同族先例 `system.peer_binding_create`）。
# ⚠️ **`app_secret` 一律不进模板、不进任何列**（凭据纪律）：唯一出现的是 `app_key`——**按设计就不是机密**（明文入库 + 唯一索引）；轮换那条**只记"换了"这件事**、⛔ 不记新旧密钥。
SYSTEM_OPEN_API_APP_CREATE = "创建了开放平台应用：{target_name}（AppKey：{to_value}）"
# ⚠️ 一条**改动作**覆盖三个可改字段（改名 / 停用启用 / IP 白名单，spec §7.3 的 PATCH）：
# 调用方**每个真正变化的字段写一行**（与账号线 `user.game_permission_add` /
# `_remove` 一次请求两行同形），⛔ 不是把三个字段挤进一句（那会漏掉后两个）。
SYSTEM_OPEN_API_APP_UPDATE = "修改了开放平台应用：{target_name}（{field_label}：{from_value} → {to_value}）"
# ⚠️ **轮换是独立的一条码、⛔ 不并进"改"**：它是**凭据事件**（旧密钥当场失效 ⇒
# 第三方瞬时断线，spec §9-12 的代价），事后要能单独筛出来回答「这把密钥什么时候换过」。
SYSTEM_OPEN_API_APP_ROTATE_SECRET = (
    "轮换了开放平台应用：{target_name} 的 AppSecret（旧密钥立即失效）"
)
# ⭐ 开放平台推送台账：手工重投一条投递失败的推送。**新拟**（对标没有这个页面 ⇒ 句式照**同组**写；证据等级 = 推断）。为什么必须留痕：「把已判失败、对方**没收到的**事件重新发给对方」是**对外的** 副作用，事后必须能回答「谁在什么时候把哪一条重投了」；⚠️ 与 `system.open_api_app_*` 分开一条码
# （那条是"改配置"，这条真产生一次对外投递）。⛔ **`reason` 不进模板**（对方响应体片段，可能含对方系统内容）；`{to_value}` 装**事件中文名**（`OpenApiDelivery.get_event_display()`，「事件：」标签由正文提供，R10）。
SYSTEM_OPEN_API_DELIVERY_RETRY = (
    "已将开放平台应用 {target_name} 的回调重新排队（事件：{to_value}，订单 {order_no}）"
)

# ⭐ 问题反馈线（spec `2026-10-08-feedback-design.md` §8）：管理员处置一条反馈。**新拟**——对标没有「反馈处置」动作 ⇒ 句式照同组 `performance.review` 新写；归 `40 系统相关`（**系统自身**的运维动作，读码 + 排除法）。⚠️ 只有**处置**这一个端点记（提交与签发票据**刻意不记**，见
# `DELIBERATELY_NOT_LOGGED`）；`to_value` = 处置后状态中文，`reason` = 备注纯值（R10）。 ⛔ 反馈正文不进模板、不进任何列（提交人自由文本）；正文不写 `#{target_id}`（目标由 `target_kind`/`target_id` 两列承载）。
FEEDBACK_HANDLE = "处理了问题反馈 {target_id}，处理状态：{to_value}（原因：{reason}）"

# ⭐ 打手侧开放接口线：内部应用管理端点的三条写路径（spec `docs/specs/2026-10-08-taker-open-api-design.md` §6）。**新拟**，理由与 `system.open_api_app_*` 同一条（对标没有"给第三方开凭据"，无模板可抄）。⛔⭐ **与上家侧分开三条码**：两条通道是两张表、
# 两套契约、两个页面（spec §2），共用一个码会让日志页把两种凭据混成一句。⚠️ **`app_secret` 一律不进模板**（`D-0201` 不放松）：唯一出现的是 `app_key`（按设计不是机密）；轮换只记"换了"、⛔ 不记新旧密钥。 `{reason}` 装**绑定打手显示名**（创建那条；R11 名字进 `target_name` 已被应用名占用 ⇒ R10 纯值）。
SYSTEM_TAKER_API_APP_CREATE = (
    "创建了打手侧应用：{target_name}（AppKey：{to_value}，绑定打手：{reason}）"
)
# ⚠️ 一条**改动作**覆盖三个可改字段（改名 / 停用启用 / IP 白名单，spec §6 的 PATCH）：调用方**每个真正变化的字段写一行**（与上家侧那条同形），⛔ 不是把三个字段挤进一句。
# ⛔ `taker`（绑定账号）**不可改** ⇒ 它不会出现在本模板的 `field_label` 里（理由与判据在 `apps/takerapi/serializers.py::AppUpdateSerializer` 的 docstring）。
SYSTEM_TAKER_API_APP_UPDATE = (
    "修改了打手侧应用：{target_name}（{field_label}：{from_value} → {to_value}）"
)
# ⚠️ **轮换是独立的一条码、⛔ 不并进"改"**：它是**凭据事件**（旧密钥当场失效 ⇒ 外部工作室
# 瞬时断线，spec §6 的"秒级生效、无宽限期"），事后要能单独筛出来回答「这把密钥什么时候换过」。
SYSTEM_TAKER_API_APP_ROTATE_SECRET = (
    "轮换了打手侧应用：{target_name} 的 AppSecret（旧密钥立即失效）"
)

# ⭐ 打手侧回调台账：手工重投一条投递失败的回调推送。**证据等级 = 推断**，与上家侧 `system.open_api_delivery_retry` 同一条理由 ⇒ 句式照同形写（留痕理由同：对外副作用）。 ⛔⭐ **与上家侧分开一条码**：台账是**两张表**（`TakerApiDelivery` ↔ `OpenApiDelivery`）、
# 收件人是**两类人**（打手 ↔ 上家），共用一个码会把两件事读成一件。⚠️ **`reason` 不进模板** （对方响应体片段）；`{to_value}` 装**事件中文名**（`TakerApiDelivery.get_event_display()`）。
SYSTEM_TAKER_API_DELIVERY_RETRY = (
    "已将打手侧应用 {target_name} 的回调重新排队（事件：{to_value}，订单 {order_no}）"
)

# ⭐ 打手钱包 / 提现审核线（D-0269）。**新拟**：对标没有这一整块 ⇒ 句式照同域（钱 + 单号）既有动作写。 ⚠️ **分类 = `70 其它`**（`LogType.OTHER`）：操作日志的 8 个分类里没有「财务 / 钱包」，这几条不写订单 / 绩效 / 用户 / 会员 / 排团任何一域（排除法）——塞进 20 会读成绩效动作、塞进 40 会读成系统设置。
# 金额一律**字符串**（调用方 `f"{amount:.2f}"`），「金额：」「元」标签由模板正文提供（R10）。 ⚠️ `wallet.deposit` 记**发起充值**（钱还没到账；到账由 Jeepay 回调入账，回调机器对机器不记日志）。
WALLET_RELEASE = "申请了解冻押金：{to_value} 元"
# ⭐ 解冻申请被「有单在跑」静默清空要留痕（到期复核发现有进行中的单 ⇒ 只清申请、不退钱、没有通知）。 ⚠️ **单独一条码**（⛔ 不复用 `wallet.release`）：那条记「**申请**」，本码记「申请**作废**」——
# 相反方向共用一个码会把它们读成同一件。分类 70 同上族；机器来源（到期复核顺带跑）⇒ 调用方必须 `actor_id=None` + `system=True`；`{to_value}` 装申请金额、`{reason}` 装**申请时刻**（非自由文本）。
WALLET_RELEASE_REQUEST_VOIDED = (
    "解冻申请因有进行中的订单作废：{target_name}，"
    "该笔 {to_value} 元押金继续冻结（申请时间：{reason}）"
)
WALLET_WITHDRAW = "申请了提现：{to_value} 元（提现单号：{reason}）"
WALLET_DEPOSIT = "发起了押金充值：{to_value} 元（充值单号：{reason}）"

# ⭐ 提现审核后台：**五个写动作各一条码**。审核人与打款人可以是同一人（业务方拍板），所以「通过」与「登记打款」**各落一条**——同一人连点两步会看到两行，这正是要的（两步各自可追）。
# `finance.withdraw_refresh` 记「**谁点过刷新**」（可能把单推到 paid / failed，也可能什么都没变）； `reason` 装刷新**之后**的状态中文名。
WITHDRAW_APPROVE = "通过了打手提现申请：{target_name}，金额：{to_value} 元"
WITHDRAW_REJECT = "驳回了打手提现申请：{target_name}，金额：{to_value} 元，原因：{reason}"
WITHDRAW_TRANSFER = (
    "发起了打手提现代付：{target_name}，金额：{to_value} 元（商户单号：{reason}）"
)
WITHDRAW_REFRESH = (
    "刷新了打手提现代付状态：{target_name}，金额：{to_value} 元（当前：{reason}）"
)
WITHDRAW_MARK_PAID = (
    "已登记 {target_name} 的提现人工打款结果，金额 {to_value} 元（打款单号：{reason}）"
)
# ⭐ 财务中断：`approved` 上打款前拦下，钱退回余额。
# ⚠️ 与「驳回」分开：驳回只对 `pending`（还没审核），中断只对 `approved`（已通过、还没打款）。
WITHDRAW_INTERRUPT = (
    "中断了打手提现：{target_name}，金额：{to_value} 元，原因：{reason}"
)

# ⭐ 客服后台打手钱包调账 / 押金规则。**新拟**：对标没有这一块 ⇒ 句式照同域（钱 + 原因）既有动作写；
# 分类 = 70 其它（理由同上域，排除法）。⚠️ **钱包调账与押金规则各一条码**：前者动一个人的钱，后者
# 动「谁能接单」的全局判据（一行改错会让整租户打手接不了单），事后追查必须能分开筛。金额字符串 + R10 同上。
WALLET_ADJUST = "调整了打手钱包：{target_name}，{field_label}：{from_value} → {to_value} 元（原因：{reason}）"
DEPOSIT_RULE_UPDATE = "修改了游戏押金金额：{target_name}，{from_value} → {to_value} 元"

# ⭐ 打手**个人押金额度**（计划 `docs/plans/2026-10-23-taker-deposit-quota-plan.md` §6）。 **新拟**：对标没有「按人配押金额度」，句式照 `DEPOSIT_RULE_UPDATE`（同域：改「接单要冻多少」）；分类 = 70 其它（同上）。⚠️ **开关与金额分开两条码**：开关动「按谁的口径算」（全局 ↔ 个人），
# 金额动某款游戏冻多少，必须能分开筛。金额字符串（R10）；开关 from/to 是中文「关 / 开」（调用方映射，⛔ 不把 True/False 写进日志正文）。
TAKER_DEPOSIT_QUOTA_SWITCH = (
    "修改了打手个人押金额度开关：{target_name}，{from_value} → {to_value}"
)
TAKER_DEPOSIT_QUOTA_AMOUNT = (
    "修改了打手个人押金额度：{target_name}，{game_name}：{from_value} → {to_value} 元"
)

# --- §5.5 70 其它 ----------------------------------------------------------
PROJECT_CREATE = "新增代练项目：{target_name}"
PROJECT_UPDATE = "修改代练项目：{target_name}"
PROJECT_DESTROY = "删除代练项目：{target_name}"
CATEGORY_CREATE = "新增项目分类：{target_name}"
CATEGORY_UPDATE = "修改项目分类：{target_name}"
CATEGORY_DESTROY = "删除项目分类：{target_name}"


ACTION_TEMPLATES: dict[str, str] = {
    # 10 订单相关
    "order.view_credentials": ORDER_VIEW_CREDENTIALS,
    "order.open_edit": ORDER_OPEN_EDIT,
    "order.change_taker_price": ORDER_CHANGE_TAKER_PRICE,
    # ⚠️ `order.change_payment_status` 这**一个字符串**同时是两套登记的 code：① RBAC 节点 code （`apps/rbac/catalog.py` 的「改收款状态」，`OrderViewSet.ORDER_PERMISSIONS` 消费它）；
    # ② 本表的审计动作 code（`order_views.payment_status` 写日志用它）。**有意同名便于对照，但两张登记表互不相干**（改一张不连带改另一张，同名 ≠ 同一件事）。
    "order.change_payment_status": ORDER_CHANGE_PAYMENT_STATUS,
    # ⚠️ 同形：`order.change_offer_price` 也是「RBAC 节点 code 与审计动作 code 同名但互不相干」
    # ——RBAC 节点是 `order.set_offer_price`（20，本端点消费它），与审计动作**不同名**；
    # 不要把两者当同一张表。
    "order.change_offer_price": ORDER_CHANGE_OFFER_PRICE,
    "order.claim": ORDER_CLAIM,
    "order.create": ORDER_CREATE,
    "order.update": ORDER_UPDATE,
    "order.transition": ORDER_TRANSITION,
    "order.throw_to_hall": ORDER_THROW_TO_HALL,
    "order.withdraw": ORDER_WITHDRAW,
    "order.clear_taker": ORDER_CLEAR_TAKER,
    "order.change_sales_agent": ORDER_CHANGE_SALES_AGENT,
    "order.refund": ORDER_REFUND,
    "order.copy": ORDER_COPY,
    "order.note": ORDER_NOTE,
    "order.wecom_chat_link": ORDER_WECOM_CHAT_LINK,
    "order.penalty_submit": ORDER_PENALTY_SUBMIT,
    "order.penalty_approve": ORDER_PENALTY_APPROVE,
    "order.ship_result": ORDER_SHIP_RESULT,
    "order.split_ship_hold": ORDER_SPLIT_SHIP_HOLD,
    "order.split_main_commission_error": ORDER_SPLIT_MAIN_COMMISSION_ERROR,
    "order.delete": ORDER_DELETE,
    "order.delete_screenshot": ORDER_DELETE_SCREENSHOT,
    "order.restore": ORDER_RESTORE,
    "order.jidanpu_remote_claim": ORDER_JIDANPU_REMOTE_CLAIM,
    "order.jidanpu_remote_taken": ORDER_JIDANPU_REMOTE_TAKEN,
    "order.jidanpu_withdraw": ORDER_JIDANPU_WITHDRAW,
    "order.jidanpu_member_auto_claim": ORDER_JIDANPU_MEMBER_AUTO_CLAIM,
    "order.jidanpu_direct_assign": "处理了订单 {order_no} 的鸡蛋铺下家指派",
    "order.peer_withdraw": ORDER_PEER_WITHDRAW,
    "order.peer_promote_price_override": ORDER_PEER_PROMOTE_PRICE_OVERRIDE,
    # ⭐⭐ peer 同步的 9 条机器行：前缀刻意**不是** `order.` ⇒ 不进「交易订单操作记录」页；
    # 订单详情页（`/orders/<单号>/audit/` 按 `order_no` 取行）照常看得到。
    "peer.inbound_order": PEER_INBOUND_ORDER,
    "peer.inbound_assign": PEER_INBOUND_ASSIGN,
    "peer.inbound_grab": PEER_INBOUND_GRAB,
    "peer.inbound_update": PEER_INBOUND_UPDATE,
    "peer.inbound_screenshot": PEER_INBOUND_SCREENSHOT,
    "peer.hall_claim": PEER_HALL_CLAIM,
    "peer.inbound_hall_collect": PEER_INBOUND_HALL_COLLECT,
    "peer.upstream_status": PEER_UPSTREAM_STATUS,
    "peer.upstream_screenshot": PEER_UPSTREAM_SCREENSHOT,
    "peer.outbound_sync": PEER_OUTBOUND_SYNC,
    "order.xgj_push_refund": ORDER_XGJ_PUSH_REFUND,
    "order.xgj_push_refund_settled": ORDER_XGJ_PUSH_REFUND_SETTLED,
    "order.xgj_push_receipt": ORDER_XGJ_PUSH_RECEIPT,
    "order.upstream_receipt_query": ORDER_UPSTREAM_RECEIPT_QUERY,
    "order.export": ORDER_EXPORT,
    "order.dailiantong_push": ORDER_DAILIANTONG_PUSH,
    "order.dailiantong_withdraw": ORDER_DAILIANTONG_WITHDRAW,
    "order.dailiantong_price": ORDER_DAILIANTONG_PRICE,
    "order.dailiantong_time": ORDER_DAILIANTONG_TIME,
    "order.dailiantong_message": ORDER_DAILIANTONG_MESSAGE,
    "order.dailiantong_qr": ORDER_DAILIANTONG_QR,
    "order.dailiantong_auto_reply": ORDER_DAILIANTONG_AUTO_REPLY,
    "order.dailiantong_accept": ORDER_DAILIANTONG_ACCEPT,
    "order.dailiantong_sync": ORDER_DAILIANTONG_SYNC,
    "order.dailiantong_up_price": ORDER_DAILIANTONG_UP_PRICE,
    "order.dailiantong_arbitration": ORDER_DAILIANTONG_ARBITRATION,
    "order.dailiantong_value": ORDER_DAILIANTONG_VALUE,
    "order.dailiantong_off_sell": ORDER_DAILIANTONG_OFF_SELL,
    "order.dailiantong_lock": ORDER_DAILIANTONG_LOCK,
    "order.dailiantong_blacklist": ORDER_DAILIANTONG_BLACKLIST,
    "order.dailiantong_template": ORDER_DAILIANTONG_TEMPLATE,
    "order.dailiantong_repush": ORDER_DAILIANTONG_REPUSH,
    "order.dailiantong_amount": ORDER_DAILIANTONG_AMOUNT,
    "order.dailiantong_edit": ORDER_DAILIANTONG_EDIT,
    "order.dailiantong_progress": ORDER_DAILIANTONG_PROGRESS,
    "order.dailiantong_agree_cancel": ORDER_DAILIANTONG_AGREE_CANCEL,
    "order.dailiantong_clear_taker": ORDER_DAILIANTONG_CLEAR_TAKER,
    "order.malailai_qr_fetch": ORDER_MALAILAI_QR_FETCH,
    # 20 绩效相关
    "performance.assign": PERFORMANCE_ASSIGN,
    "performance.reassign": PERFORMANCE_REASSIGN,
    "performance.adjust": PERFORMANCE_ADJUST,
    "performance.adjust_manual": PERFORMANCE_ADJUST_MANUAL,
    "performance.submit": PERFORMANCE_SUBMIT,
    "performance.review": PERFORMANCE_REVIEW,
    "performance.settle": PERFORMANCE_SETTLE,
    "performance.delete": PERFORMANCE_DELETE,
    "performance.export": PERFORMANCE_EXPORT,
    "settlement.export": SETTLEMENT_EXPORT,
    "my_orders.export": MY_ORDERS_EXPORT,
    "performance.recompute_customer_commission": PERFORMANCE_RECOMPUTE_CUSTOMER_COMMISSION,
    # 30 用户相关
    "user.create": USER_CREATE,
    "user.update": USER_UPDATE,
    "user.rename": USER_RENAME,
    "user.game_permission_add": USER_GAME_PERMISSION_ADD,
    "user.game_permission_remove": USER_GAME_PERMISSION_REMOVE,
    "user.role_change": USER_ROLE_CHANGE,
    "user.status": USER_STATUS,
    "user.status_batch": USER_STATUS_BATCH,
    "user.reset_password": USER_RESET_PASSWORD,
    "user.self_update": USER_SELF_UPDATE,
    "user.delete": USER_DELETE,
    "user.restore": USER_RESTORE,
    # ⭐ `user.realname_*` 两条都不以 `order.` 开头 ⇒ `ORDER_ACTION_LABELS` 一个字都不用动
    # （那张短名表的键集由 `test_order_action_labels_cover_exactly_the_order_prefixed_actions`
    # 按 `order.` 前缀**恰好**钉住）。
    "user.realname_verify": USER_REALNAME_VERIFY,
    "user.realname_reset": USER_REALNAME_RESET,
    "user.selfreg_revoke": USER_SELFREG_REVOKE,
    # 40 系统相关
    "system.role_update": SYSTEM_ROLE_UPDATE,
    "system.role_destroy": SYSTEM_ROLE_DESTROY,
    "system.role_create": SYSTEM_ROLE_CREATE,
    "system.role_copy": SYSTEM_ROLE_COPY,
    "system.game_create": SYSTEM_GAME_CREATE,
    "system.game_destroy": SYSTEM_GAME_DESTROY,
    "system.game_update": SYSTEM_GAME_UPDATE,
    "system.server_create": SYSTEM_SERVER_CREATE,
    "system.server_update": SYSTEM_SERVER_UPDATE,
    "system.server_destroy": SYSTEM_SERVER_DESTROY,
    "system.order_source_create": SYSTEM_ORDER_SOURCE_CREATE,
    "system.order_source_update": SYSTEM_ORDER_SOURCE_UPDATE,
    "system.order_source_destroy": SYSTEM_ORDER_SOURCE_DESTROY,
    "system.order_source_group_create": SYSTEM_ORDER_SOURCE_GROUP_CREATE,
    "system.order_source_group_update": SYSTEM_ORDER_SOURCE_GROUP_UPDATE,
    "system.order_source_group_destroy": SYSTEM_ORDER_SOURCE_GROUP_DESTROY,
    "system.shop_binding_create": SYSTEM_SHOP_BINDING_CREATE,
    "system.shop_binding_update": SYSTEM_SHOP_BINDING_UPDATE,
    "system.shop_binding_destroy": SYSTEM_SHOP_BINDING_DESTROY,
    "system.xgj_binding_create": SYSTEM_XGJ_BINDING_CREATE,
    "system.xgj_binding_update": SYSTEM_XGJ_BINDING_UPDATE,
    "system.xgj_binding_destroy": SYSTEM_XGJ_BINDING_DESTROY,
    "system.xgj_binding_sync_shops": SYSTEM_XGJ_BINDING_SYNC_SHOPS,
    # ⛔ `system.jidanpu_binding_create` / `_update` 两行已随**租户级**那条线拆除
    # （说明见常量区那段）。⛔ 别把这两行加回来——产生它们的视图已不存在。
    "system.jidanpu_member_binding_create": SYSTEM_JIDANPU_MEMBER_BINDING_CREATE,
    "system.jidanpu_member_binding_update": SYSTEM_JIDANPU_MEMBER_BINDING_UPDATE,
    "system.jidanpu_upstream_binding_create": SYSTEM_JIDANPU_UPSTREAM_BINDING_CREATE,
    "system.jidanpu_upstream_binding_update": SYSTEM_JIDANPU_UPSTREAM_BINDING_UPDATE,
    "system.jidanpu_upstream_binding_delete": SYSTEM_JIDANPU_UPSTREAM_BINDING_DELETE,
    "finance.alipay_authorization": "付款支付宝授权操作：{reason}",
    "finance.online_payroll": "在线结算付款单 {target_name}：{reason}",
    "system.recognition_keywords_update": SYSTEM_RECOGNITION_KEYWORDS_UPDATE,
    "system.system_switch_update": SYSTEM_SYSTEM_SWITCH_UPDATE,
    "system.platform_integration_create": SYSTEM_PLATFORM_INTEGRATION_CREATE,
    "system.platform_integration_update": SYSTEM_PLATFORM_INTEGRATION_UPDATE,
    "system.platform_integration_destroy": SYSTEM_PLATFORM_INTEGRATION_DESTROY,
    "system.approval_settings_update": SYSTEM_APPROVAL_SETTINGS_UPDATE,
    "system.platform_settings_update": SYSTEM_PLATFORM_SETTINGS_UPDATE,
    "system.order_qr_config_update": SYSTEM_ORDER_QR_CONFIG_UPDATE,
    "system.platform_auto_reply_create": SYSTEM_PLATFORM_AUTO_REPLY_CREATE,
    "system.platform_auto_reply_update": SYSTEM_PLATFORM_AUTO_REPLY_UPDATE,
    "system.platform_auto_reply_destroy": SYSTEM_PLATFORM_AUTO_REPLY_DESTROY,
    "system.platform_auto_reply_image_upload": SYSTEM_PLATFORM_AUTO_REPLY_IMAGE_UPLOAD,
    "system.peer_binding_create": SYSTEM_PEER_BINDING_CREATE,
    "system.peer_binding_delete": SYSTEM_PEER_BINDING_DELETE,
    "system.peer_binding_update": SYSTEM_PEER_BINDING_UPDATE,
    "system.peer_binding_auto_revoke": SYSTEM_PEER_BINDING_AUTO_REVOKE,
    "system.wechatmp_binding_create": SYSTEM_WECHATMP_BINDING_CREATE,
    "system.wechatmp_binding_delete": SYSTEM_WECHATMP_BINDING_DELETE,
    # ⚠️ 开放平台应用三条 + 打手侧应用三条：**轮换各自单独一条**，⛔ 不并进 `_update`。
    "system.open_api_app_create": SYSTEM_OPEN_API_APP_CREATE,
    "system.open_api_app_update": SYSTEM_OPEN_API_APP_UPDATE,
    "system.open_api_app_rotate_secret": SYSTEM_OPEN_API_APP_ROTATE_SECRET,
    "system.open_api_delivery_retry": SYSTEM_OPEN_API_DELIVERY_RETRY,
    "feedback.handle": FEEDBACK_HANDLE,
    "system.taker_api_app_create": SYSTEM_TAKER_API_APP_CREATE,
    "system.taker_api_app_update": SYSTEM_TAKER_API_APP_UPDATE,
    "system.taker_api_app_rotate_secret": SYSTEM_TAKER_API_APP_ROTATE_SECRET,
    "system.qqbot_binding_update": QQBOT_BINDING_UPDATE,
    "system.wecom_config_update": SYSTEM_WECOM_CONFIG_UPDATE,
    "system.wxpusher_config_update": SYSTEM_WXPUSHER_CONFIG_UPDATE,
    "system.internal_chat_directory_update": SYSTEM_INTERNAL_CHAT_DIRECTORY_UPDATE,
    "system.internal_chat_visitor_availability": SYSTEM_INTERNAL_CHAT_VISITOR_AVAILABILITY,
    "system.internal_chat_visitor_transfer": SYSTEM_INTERNAL_CHAT_VISITOR_TRANSFER,
    "system.internal_chat_visitor_close": SYSTEM_INTERNAL_CHAT_VISITOR_CLOSE,
    "system.internal_chat_notice_settings_update": SYSTEM_INTERNAL_CHAT_NOTICE_SETTINGS_UPDATE,
    "system.taker_api_delivery_retry": SYSTEM_TAKER_API_DELIVERY_RETRY,
    # 70 其它
    # ⭐ 打手钱包 / 提现审核这族 8 条：分类 = 70 的理由见 `WALLET_RELEASE` 那段。
    "wallet.release": WALLET_RELEASE,
    "wallet.release_request_voided_by_activity": WALLET_RELEASE_REQUEST_VOIDED,
    "wallet.withdraw": WALLET_WITHDRAW,
    "wallet.deposit": WALLET_DEPOSIT,
    "finance.withdraw_approve": WITHDRAW_APPROVE,
    "finance.withdraw_reject": WITHDRAW_REJECT,
    "finance.withdraw_transfer": WITHDRAW_TRANSFER,
    "finance.withdraw_refresh": WITHDRAW_REFRESH,
    "finance.withdraw_mark_paid": WITHDRAW_MARK_PAID,
    "finance.withdraw_interrupt": WITHDRAW_INTERRUPT,
    "finance.taker_wallet_adjust": WALLET_ADJUST,
    "finance.taker_deposit_rule_update": DEPOSIT_RULE_UPDATE,
    "finance.taker_deposit_quota_switch": TAKER_DEPOSIT_QUOTA_SWITCH,
    "finance.taker_deposit_quota_amount": TAKER_DEPOSIT_QUOTA_AMOUNT,
    "project.create": PROJECT_CREATE,
    "project.update": PROJECT_UPDATE,
    "project.destroy": PROJECT_DESTROY,
    "category.create": CATEGORY_CREATE,
    "category.update": CATEGORY_UPDATE,
    "category.destroy": CATEGORY_DESTROY,
}

ACTION_LOG_TYPES: dict[str, int] = {
    "order.view_credentials": LogType.ORDER,
    "order.open_edit": LogType.ORDER,
    "order.change_taker_price": LogType.ORDER,
    "order.change_payment_status": LogType.ORDER,
    "order.change_offer_price": LogType.ORDER,
    "order.claim": LogType.ORDER,
    "order.create": LogType.ORDER,
    "order.update": LogType.ORDER,
    "order.transition": LogType.ORDER,
    "order.throw_to_hall": LogType.ORDER,
    "order.withdraw": LogType.ORDER,
    "order.clear_taker": LogType.ORDER,
    "order.change_sales_agent": LogType.ORDER,
    "order.refund": LogType.ORDER,
    "order.copy": LogType.ORDER,
    # ⭐ 订单日志 ⇒ 10：**依据是读码，不是猜**（`order.*` 一族在本表全部是 `LogType.ORDER`，本动作是订单域动作）。⚠️ 对标**给不出**分类依据：那一页「订单日志」端点虽带 `LogType` 字段，
    # 实测全 `null`、页面从不读它（recon §7.1）；操作日志端点里没有订单日志这种动作（recon §8.2） ⇒ 这是一条**我方自拟**的分类。
    "order.note": LogType.ORDER,
    "order.wecom_chat_link": LogType.ORDER,
    "order.penalty_submit": LogType.ORDER,
    "order.penalty_approve": LogType.ORDER,
    "order.ship_result": LogType.ORDER,
    "order.split_ship_hold": LogType.ORDER,
    "order.split_main_commission_error": LogType.ORDER,
    "order.delete": LogType.ORDER,
    "order.delete_screenshot": LogType.ORDER,
    "order.restore": LogType.ORDER,
    "order.jidanpu_remote_claim": LogType.ORDER,
    "order.jidanpu_remote_taken": LogType.ORDER,
    "order.jidanpu_withdraw": LogType.ORDER,
    "order.jidanpu_member_auto_claim": LogType.ORDER,
    "order.jidanpu_direct_assign": LogType.ORDER,
    "order.peer_withdraw": LogType.ORDER,
    "order.peer_promote_price_override": LogType.ORDER,
    # ⭐⭐ 9 条 `peer.*` 取 **40 系统相关**：依据是读码——结构网 `test_order_prefixed_actions_are_exactly_the_log_type_ten_actions` 规定非 `order.` 前缀
    # 不得登记成 10；本组裁定**不收进**「交易订单操作记录」页，留在系统日志整族可筛，订单详情页按 `order_no` 取行、照常可见。
    "peer.inbound_order": LogType.SYSTEM,
    "peer.inbound_assign": LogType.SYSTEM,
    "peer.inbound_grab": LogType.SYSTEM,
    "peer.inbound_update": LogType.SYSTEM,
    "peer.inbound_screenshot": LogType.SYSTEM,
    "peer.hall_claim": LogType.SYSTEM,
    "peer.inbound_hall_collect": LogType.SYSTEM,
    "peer.upstream_status": LogType.SYSTEM,
    "peer.upstream_screenshot": LogType.SYSTEM,
    "peer.outbound_sync": LogType.SYSTEM,
    "order.xgj_push_refund": LogType.ORDER,
    "order.xgj_push_refund_settled": LogType.ORDER,
    "order.xgj_push_receipt": LogType.ORDER,
    "order.upstream_receipt_query": LogType.ORDER,
    "order.export": LogType.ORDER,
    "order.dailiantong_push": LogType.ORDER,
    "order.dailiantong_withdraw": LogType.ORDER,
    "order.dailiantong_price": LogType.ORDER,
    "order.dailiantong_time": LogType.ORDER,
    "order.dailiantong_message": LogType.ORDER,
    "order.dailiantong_qr": LogType.ORDER,
    "order.dailiantong_auto_reply": LogType.ORDER,
    "order.dailiantong_accept": LogType.ORDER,
    "order.dailiantong_sync": LogType.ORDER,
    "order.dailiantong_up_price": LogType.ORDER,
    "order.dailiantong_arbitration": LogType.ORDER,
    "order.dailiantong_value": LogType.ORDER,
    "order.dailiantong_off_sell": LogType.ORDER,
    "order.dailiantong_lock": LogType.ORDER,
    "order.dailiantong_blacklist": LogType.ORDER,
    "order.dailiantong_template": LogType.ORDER,
    "order.dailiantong_repush": LogType.ORDER,
    "order.dailiantong_amount": LogType.ORDER,
    "order.dailiantong_edit": LogType.ORDER,
    "order.dailiantong_progress": LogType.ORDER,
    "order.dailiantong_agree_cancel": LogType.ORDER,
    "order.dailiantong_clear_taker": LogType.ORDER,
    "order.malailai_qr_fetch": LogType.ORDER,
    "performance.assign": LogType.PERFORMANCE,
    "performance.reassign": LogType.PERFORMANCE,
    "performance.adjust": LogType.PERFORMANCE,
    "performance.adjust_manual": LogType.PERFORMANCE,
    "performance.submit": LogType.PERFORMANCE,
    "performance.review": LogType.PERFORMANCE,
    "performance.settle": LogType.PERFORMANCE,
    "performance.delete": LogType.PERFORMANCE,
    "performance.export": LogType.PERFORMANCE,
    "settlement.export": LogType.PERFORMANCE,
    "my_orders.export": LogType.PERFORMANCE,
    # ⭐ 重算客服提成归**既有**分类 20；⛔ 不新增 `LogType`（spec §D7/§D17）。
    "performance.recompute_customer_commission": LogType.PERFORMANCE,
    "user.create": LogType.USER,
    "user.update": LogType.USER,
    "user.rename": LogType.USER,
    "user.game_permission_add": LogType.USER,
    "user.game_permission_remove": LogType.USER,
    "user.role_change": LogType.USER,
    "user.status": LogType.USER,
    "user.status_batch": LogType.USER,
    "user.reset_password": LogType.USER,
    "user.self_update": LogType.USER,
    "user.delete": LogType.USER,
    "user.restore": LogType.USER,
    "user.realname_verify": LogType.USER,
    "user.realname_reset": LogType.USER,
    "user.selfreg_revoke": LogType.USER,
    "system.role_update": LogType.SYSTEM,
    "system.role_destroy": LogType.SYSTEM,
    "system.role_create": LogType.SYSTEM,
    "system.role_copy": LogType.SYSTEM,
    "system.game_create": LogType.SYSTEM,
    "system.game_destroy": LogType.SYSTEM,
    "system.game_update": LogType.SYSTEM,
    "system.server_create": LogType.SYSTEM,
    "system.server_update": LogType.SYSTEM,
    "system.server_destroy": LogType.SYSTEM,
    "system.order_source_create": LogType.SYSTEM,
    "system.order_source_update": LogType.SYSTEM,
    "system.order_source_destroy": LogType.SYSTEM,
    "system.order_source_group_create": LogType.SYSTEM,
    "system.order_source_group_update": LogType.SYSTEM,
    "system.order_source_group_destroy": LogType.SYSTEM,
    "system.shop_binding_create": LogType.SYSTEM,
    "system.shop_binding_update": LogType.SYSTEM,
    "system.shop_binding_destroy": LogType.SYSTEM,
    "system.xgj_binding_create": LogType.SYSTEM,
    "system.xgj_binding_update": LogType.SYSTEM,
    "system.xgj_binding_destroy": LogType.SYSTEM,
    "system.xgj_binding_sync_shops": LogType.SYSTEM,
    # ⛔ `system.jidanpu_binding_create` / `_update` 两行已随**租户级**那条线拆除
    # （不再可能产生；历史台账行照旧可读）。
    "system.jidanpu_member_binding_create": LogType.SYSTEM,
    "system.jidanpu_member_binding_update": LogType.SYSTEM,
    "system.jidanpu_upstream_binding_create": LogType.SYSTEM,
    "system.jidanpu_upstream_binding_update": LogType.SYSTEM,
    "system.jidanpu_upstream_binding_delete": LogType.SYSTEM,
    "finance.alipay_authorization": LogType.SYSTEM,
    "finance.online_payroll": LogType.PERFORMANCE,
    "system.recognition_keywords_update": LogType.SYSTEM,
    "system.system_switch_update": LogType.SYSTEM,
    "system.platform_integration_create": LogType.SYSTEM,
    "system.platform_integration_update": LogType.SYSTEM,
    "system.platform_integration_destroy": LogType.SYSTEM,
    "system.approval_settings_update": LogType.SYSTEM,
    "system.platform_settings_update": LogType.SYSTEM,
    "system.order_qr_config_update": LogType.SYSTEM,
    "system.platform_auto_reply_create": LogType.SYSTEM,
    "system.platform_auto_reply_update": LogType.SYSTEM,
    "system.platform_auto_reply_destroy": LogType.SYSTEM,
    "system.platform_auto_reply_image_upload": LogType.SYSTEM,
    "system.peer_binding_create": LogType.SYSTEM,
    "system.peer_binding_delete": LogType.SYSTEM,
    "system.peer_binding_update": LogType.SYSTEM,
    "system.peer_binding_auto_revoke": LogType.SYSTEM,
    "system.wechatmp_binding_create": LogType.SYSTEM,
    "system.wechatmp_binding_delete": LogType.SYSTEM,
    "system.open_api_app_create": LogType.SYSTEM,
    "system.open_api_app_update": LogType.SYSTEM,
    "system.open_api_app_rotate_secret": LogType.SYSTEM,
    "system.open_api_delivery_retry": LogType.SYSTEM,
    "feedback.handle": LogType.SYSTEM,
    "system.taker_api_app_create": LogType.SYSTEM,
    "system.taker_api_app_update": LogType.SYSTEM,
    "system.taker_api_app_rotate_secret": LogType.SYSTEM,
    "system.qqbot_binding_update": LogType.SYSTEM,
    "system.wecom_config_update": LogType.SYSTEM,
    "system.wxpusher_config_update": LogType.SYSTEM,
    "system.internal_chat_directory_update": LogType.SYSTEM,
    "system.internal_chat_visitor_availability": LogType.SYSTEM,
    "system.internal_chat_visitor_transfer": LogType.SYSTEM,
    "system.internal_chat_visitor_close": LogType.SYSTEM,
    "system.internal_chat_notice_settings_update": LogType.SYSTEM,
    "system.taker_api_delivery_retry": LogType.SYSTEM,
    # ⭐ 打手钱包 / 提现审核这族 8 条都归 70 其它（分类理由见 `WALLET_RELEASE` 段）。
    "wallet.release": LogType.OTHER,
    "wallet.release_request_voided_by_activity": LogType.OTHER,
    "wallet.withdraw": LogType.OTHER,
    "wallet.deposit": LogType.OTHER,
    "finance.withdraw_approve": LogType.OTHER,
    "finance.withdraw_reject": LogType.OTHER,
    "finance.withdraw_transfer": LogType.OTHER,
    "finance.withdraw_refresh": LogType.OTHER,
    "finance.withdraw_mark_paid": LogType.OTHER,
    "finance.withdraw_interrupt": LogType.OTHER,
    "finance.taker_wallet_adjust": LogType.OTHER,
    "finance.taker_deposit_rule_update": LogType.OTHER,
    "finance.taker_deposit_quota_switch": LogType.OTHER,
    "finance.taker_deposit_quota_amount": LogType.OTHER,
    "project.create": LogType.OTHER,
    "project.update": LogType.OTHER,
    "project.destroy": LogType.OTHER,
    "category.create": LogType.OTHER,
    "category.update": LogType.OTHER,
    "category.destroy": LogType.OTHER,
}

#: 「交易订单操作记录」页（`GET /api/audit/order-action-logs/`）「操作说明」列的取值。 **键 = `order.` 前缀的全部动作码**（⛔ 不推算条数；键集写成**字面量**、不从 `ACTION_LOG_TYPES` 派生——派生会自指恒真，E-0024/E-0065；结构网在 `tests/test_operation_log_model.py`）。
#: **另立一张表的理由**（spec `2026-10-04-trade-action-log-design.md` D6）：对标那一列是列宽 100px 的 `actionName` 短名，整句 `summary` 塞进去只会被截成半句（变更值已在「变更内容」列，信息不丢）。 ⛔ 这张表**只服务显示**，不进 `ACTION_TEMPLATES` / `ACTION_LOG_TYPES`（那是写入面的登记）。
ORDER_ACTION_LABELS: dict[str, str] = {
    "order.open_edit": "进入编辑订单",
    "order.change_taker_price": "改接单价格",
    "order.change_payment_status": "改收款状态",
    "order.change_offer_price": "改发单价",
    "order.claim": "大厅自助接单",
    "order.create": "录入订单",
    "order.update": "编辑订单",
    "order.transition": "改订单状态",
    "order.throw_to_hall": "抛入接单大厅",
    "order.withdraw": "从大厅撤回",
    "order.clear_taker": "清除接单人",
    "order.change_sales_agent": "换销售客服",
    "order.refund": "更新累计退款额",
    "order.copy": "复制订单",
    "order.note": "添加订单日志",
    "order.wecom_chat_link": "关联企微群聊",
    "order.penalty_submit": ORDER_PENALTY_SUBMIT,
    "order.penalty_approve": ORDER_PENALTY_APPROVE,
    "order.ship_result": "店铺发货结果",
    "order.split_ship_hold": "拆单组暂不发货",
    "order.split_main_commission_error": "主单提成计算失败",
    "order.delete": "删除订单",
    "order.delete_screenshot": "删除订单截图",
    "order.restore": "从回收站恢复",
    "order.jidanpu_remote_claim": "接下鸡蛋铺单",
    "order.jidanpu_remote_taken": "鸡蛋铺打手接单",
    "order.jidanpu_withdraw": "撤下鸡蛋铺单",
    "order.jidanpu_member_auto_claim": "下家接单自动接手",
    "order.jidanpu_direct_assign": "指派鸡蛋铺下家打手",
    "order.peer_withdraw": "上家撤单",
    "order.peer_promote_price_override": "接单后采用本店展示价",
    "order.xgj_push_refund": "平台退款自动退单",
    "order.xgj_push_refund_settled": "平台退款未自动退单",
    "order.xgj_push_receipt": "平台交易成功同步收款状态",
    "order.upstream_receipt_query": "上家收款查询",
    "order.export": "导出订单",
    "order.dailiantong_push": "抛到代练通",
    "order.dailiantong_withdraw": "撤回代练通单",
    "order.dailiantong_price": "改代练通价格",
    "order.dailiantong_time": "代练通补时",
    "order.dailiantong_message": "代练通留言",
    "order.dailiantong_qr": "发送代练通接单说明",
    "order.dailiantong_auto_reply": "代练通自动回复",
    "order.dailiantong_accept": "代练通手动验收",
    "order.dailiantong_sync": "同步代练通状态",
    "order.dailiantong_up_price": "代练通加价",
    "order.dailiantong_arbitration": "代练通申请客服介入",
    "order.dailiantong_value": "评价代练通打手",
    "order.dailiantong_off_sell": "代练通上下架",
    "order.dailiantong_lock": "代练通锁定／解锁",
    "order.dailiantong_blacklist": "代练通打手黑名单",
    "order.dailiantong_template": "代练通订单要求模板",
    "order.dailiantong_repush": "重发代练通推送",
    "order.dailiantong_amount": "代练通补款",
    "order.dailiantong_edit": "编辑代练通订单",
    "order.dailiantong_progress": "代练通追加进度截图",
    "order.dailiantong_agree_cancel": "同意代练通撤销",
    "order.dailiantong_clear_taker": "清除代练通占位接手人",
    "order.malailai_qr_fetch": "订单群码自动拉取",
}

#: 「交易订单操作记录」页**不收**的 `order.*` 动作——**显式枚举**，⛔ 不用「理由文本含某个词」当后门（`AGENTS.md` §5-G / E-0065）；行集合、右侧「操作统计」、两个候选下拉**共用**这一份。今天只有 `order.view_credentials`（查看账号密码是**读动作**；业务方裁定不进这一页）。
#: ⚠️ **埋点没有删**：`order_views.py` 那处 `record(...)` 照旧写（安全审计要用），本集合只决定显示； `ORDER_ACTION_LABELS` 的键集 = 「`order.` 全部动作」**减去本集合**（结构网在 `tests/test_operation_log_model.py`）。
DETAIL_HIDDEN_ACTIONS = frozenset({
    "order.view_credentials",
    "order.open_edit",
    "peer.outbound_sync",
    "peer.inbound_screenshot",
    "peer.upstream_screenshot",
    "order.upstream_receipt_query",
})

ORDER_ACTION_EXCLUDED: frozenset[str] = frozenset({
    "order.view_credentials",
})

# 真实列名 → **模板里显示的中文**（R8/R9）。`field_name` 列仍存真实列名，只有渲染 summary
# 时才查这张表。取值以 `models.py` 的 `verbose_name` 为准（免得日志与业务侧各写一个名字），
# 但**不是逐字照抄**——过长的缩成适合日志行的短标签；查不到的列退化成列名本身。
FIELD_LABELS: dict[str, str] = {
    # Order（`order.update` 逐字段）
    "status": "状态",
    "order_type": "订单类型",
    "game": "游戏",
    "server": "区服",
    "category": "项目分类",
    "project": "代练/陪玩项目",
    "project_text": "项目名称",
    "content": "客户原始需求",
    "demand": "需求描述（打手看的）",
    "time_requirement": "时间要求",
    "game_account": "游戏账号",
    "game_sub_account": "子账号",
    "game_role_name": "角色名",
    "current_info_1": "当前信息1",
    "current_info_2": "当前信息2",
    "customer_phone": "联系电话",
    "customer_qq": "QQ/微信",
    "customer_ww": "旺旺/YY",
    "total_price": "订单总价",
    "received_price": "接单实收款",
    "platform_price": "发平台价格",
    "hall_price": "发内部抢单大厅价",
    "deposit_safe": "安全保证金",
    "deposit_efficiency": "效率保证金",
    "fee": "产生的手续费",
    "payment_method": "支付方式",
    "payment_status": "收款状态",
    "source": "订单来源",
    "source_order_no": "来源单号",
    "sales_agent": "接单销售客服",
    "is_urgent": "是否加急",
    "remark": "重要备注",
    "tag": "自定义标记",
    # ⭐ 第三方平台订单管理页：「更改标签」写的是**订单标签**，与上面的「自定义标记」
    # (`tag`) 是两列（对标也把标签 / 标记当两件事）。
    "order_label": "订单标签",
    # Performance（`performance.adjust` 逐字段）
    "taker_price": "接单价覆盖值",
    "deduct_mode": "扣点模式",
    "deduct_value": "扣点值",
    "temp_amount": "预计绩效",
    "bonus_amount": "生效绩效",
    "review_status": "审核状态",
    "emp_type": "收入类型",
    # ApiApp / TakerApp（`system.open_api_app_update` / `system.taker_api_app_update` 逐字段）
    # ⚠️ `state` 与上面订单的 `status` 是**两个不同的列** ⇒ 必须各有一条，⛔ 不能共用一条标签；
    # 取值照 `ApiApp` 模型字段的 `verbose_name`（`apps/openapi/models.py`）。
    "name": "应用名",
    "state": "状态",
    "ip_allowlist": "IP 白名单",
    # ⭐ 打手侧那条改动作逐字段用的是**同样三个** `field_name`（`TakerApp` 的可改列与 `ApiApp` 同名）⇒ **共用这三条标签**、⛔ 不另开一份（同名同义开两份必然漂；两族的 **动作码**分开，见常量区）。⚠️ `taker` **不在**这里：改它 = 不换凭据换掉整个权限面，
    # PATCH 白名单当场拒——理由与判据在 `apps/takerapi/serializers.py::AppUpdateSerializer`。 ⭐ 回调配置两列也能从 `PATCH /api/open/apps/<pk>/` 改 ⇒ `field_name` 会进 `system.open_api_app_update` 的模板，**必须**登记：查不到退化成裸列名，而「句子里不该
    # 出现裸列名」正是 `tests/test_openapi_app_api.py::AppPatchAuditTests` 守的那条。
    "callback_url": "回调地址",
    "callback_state": "回调开关",
    # ⭐ 客服后台打手钱包调账：调账模板里的 `{field_label}` 走 `field_name` → 这张表；
    # 取值照 `TakerWallet` 字段的 `verbose_name`（`apps/accounts/models.py`）。
    # ⚠️ 查不到会退化成裸列名 `balance` / `frozen`——那正是「句子里不该出现裸列名」要防的形态。
    "balance": "可用余额",
    "frozen": "押金冻结额",
}
