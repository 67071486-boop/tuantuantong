"""**事件写入点登记网**：新增 `Order.status` / `OrderScreenshot.status` 写入点却忘了接推送（或忘了登记为什么不推）⇒ 当场红（设计文档 §7.2.1 / §13.4-18）。⭐ 不用仓库里手维护的绕过清单——那种行号清单会烂，本网的**两向**断言在漂移的当次就红。
四条判据（每条都能被单独弄红）：① **两向恰好**——AST 扫出的 `(文件, 作用域, 形状, 对象)` 集合与登记表**逐键相等**（多一处=忘了登记、少一处=登记的落点已不存在、表烂了）；② 每条登记带**冻结枚举**标签（`推送` / `有意不推` / `非目标模型`，⛔ 无「理由文本含词」后门）；③ 档位与模型判死——`推送`/`有意不推` 只许 `Order`/`OrderScreenshot`，`非目标模型` 必须点名 `OTHER_MODELS` **冻结枚举**里的模型，且从调用链或局部 queryset 绑定核对模型名；④ AST 看不见的落点必须显式登记进 `AST_INVISIBLE_SITES`（一旦变得可扫 ⇒ 红，要求移进正式登记表）。
⚠️ 盲区如实登记（⛔ 别读成「全覆盖」）：`setattr` 动态写、状态藏变量进 `defaults=`（只有白名单一条）、`x.status = …` 档的模型是**读出来的**；⭐⭐ **共用引擎**（`openapi/dispatch.py` 的 `_attempt` / `_mark_terminal`）里那两句 `row.status = …` 分不出族 ⇒ 打手侧那一族在引擎里的写点扫不到，⛔ 不许据此登记假白名单、也装不进 `AST_INVISIBLE_SITES`（它的键恰在扫描结果里、且手工表文件集合恰为一文件）——详见 `OTHER_MODELS` 里 `TakerApiDelivery` 那条的注释。本网只有**全量套件**才报 ⇒ 受影响网清单必须含本文件（E-0091）；弄红证明：临时删一条登记 ⇒ 判据①红，逐字编辑恢复（D-0361 同法重取）。"""
from __future__ import annotations

import ast
import collections
from dataclasses import dataclass
from pathlib import Path

from django.test import SimpleTestCase

#: `src/apps` 的绝对路径（本文件在 `src/tests/` 下）。
APPS_DIR = Path(__file__).resolve().parent.parent / "apps"

#: 三种形状（`Assign` 的目标 / 两种写调用）。⚠️ 冻结枚举：加形状要**同一次**改扫描器与判据。
KIND_ASSIGN = "assign"
KIND_CREATE = "create"
KIND_UPDATE = "update"

#: 认成"写状态"的调用名（`.save()` 不算：它写的是**列**，不是"把状态设成什么值"）。
WRITE_CALLS = frozenset({"create", "update", "get_or_create", "update_or_create", "bulk_create", "bulk_update"})

#: 三个**互斥**的标签（冻结枚举）。
PUSH = "推送"
SKIP = "有意不推"
OTHER = "非目标模型"
LABELS = (PUSH, SKIP, OTHER)

#: 目标模型（本网存在的意义就是这两个）。
TARGET_MODELS = ("Order", "OrderScreenshot")

#: 被扫到、但**不是**目标模型的那些表（`x.status = …` 是它们的合法写点，与推送无关）。
#: ⚠️ 这是一张**显式枚举**的白名单：出现下一个新模型 ⇒ 判据 3 红（要求点名，⛔ 不许匿名放过）。
OTHER_MODELS = (
    "Approval",
    "OnlinePayrollTransfer",
    "OnlinePayrollGatewayBatch",
    "AlipaySettlementAuthorization",
    "Authorization",
    "QQBotGroup",  # qqbot 线（D-0265）：机器人自己的群档案（status=active/gone）
    "QQBotOutboundMessage",  # qqbot 线（D-0265）：发件箱（status=pending/sent/failed）
    "JidanpuSyncEvent",
    "RemoteHallOrder",
    "JidanpuUpstreamHallOrder",
    "PeerSyncEvent",
    # ⭐ 团团通大厅镜像线（D-0303）：下家侧镜像行自己的状态机
    # （listed/claiming/promoted/gone），不是订单状态、与推送无关（落点见
    # `EVENT_SITES` 里 `model="PeerHallMirror"` 那一批）。
    "PeerHallMirror",
    "OpenApiDelivery",
    "Feedback",
    "ChannelArticle",  # 频道文章线：文章的 draft/published 状态，与订单推送无关
    # ⭐⭐ 打手侧那张投递台账（`apps/takerapi/models.py::TakerApiDelivery`）：靠它**自己**的可扫落点进这张表——手工重投端点
    #    `takerapi/api/app_views.py::TakerAppDeliveryRetryView.post` 的 `delivery.status = …` ⇒ 扫出键 `('takerapi/api/app_views.py', 'TakerAppDeliveryRetryView.post', 'assign', 'delivery')`、模型 `TakerApiDelivery`、标签「非目标模型」⇒ 判据 3 要求它**在**这张表（⛔ 不许匿名放过）。
    #    ⚠️ 本行与「⛔ 拿共用引擎那两句去声称打手侧那张表被管住」是**两件事**（那两句依旧扫不到打手侧那一族，见模块 docstring 盲区段），别读混。
    "TakerApiDelivery",
    # ⭐ D-0296，大厅模拟单：演示卡自己的 active/retired 状态，
    # 与订单推送无关（三个落点见 `EVENT_SITES` 末尾那三条）。
    "HallSimulatedOrder",
    "WecomNotification",
    "WecomAftersalesSettings",
    "WxPusherNotice",
    "FinanceExportJob",
)
#: 共用引擎（`openapi/dispatch.py` 的 `_attempt` / `_mark_terminal`）分不出上家侧和打手侧，
#: 引擎里的写点扫不到打手侧。`TakerApiDelivery` 在这张表里，是因为重投端点自己有可扫落点。
#: 两件事别混：引擎盲区还在；可扫落点必须点名。

@dataclass(frozen=True)
class Site:
    """一条登记：`(文件, 作用域名, 形状, 对象)` + 标签 + 模型 + 计数 + 理由。"""
    path: str
    scope: str
    kind: str
    owner: str
    count: int = 1
    label: str = PUSH
    model: str = "Order"
    reason: str = ""
    queryset_caller: str | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.path, self.scope, self.kind, self.owner)


#: ⭐ **登记表**（两向恰好断言的对象）。逐条理由都写在条目里，⛔ 不写"见某处"。
#: 口径：`count` 是该作用域里**这一种形状**的落点数（同一作用域里第二处也算一次 ⇒
#: 「把同函数里的第二处删掉」也会红）。
EVENT_SITES: tuple[Site, ...] = (
    Site("business/export_jobs.py", "_claim_export", KIND_ASSIGN, "job", count=2,
         label=OTHER, model="FinanceExportJob", reason="导出任务被认领或耗尽重试，订单无需对外发送事件。"),
    Site("business/export_jobs.py", "_publish", KIND_ASSIGN, "stored",
         label=OTHER, model="FinanceExportJob", reason="记录导出文件已生成，订单无需对外发送事件。"),
    Site("business/export_jobs.py", "_record_failure", KIND_ASSIGN, "stored",
         label=OTHER, model="FinanceExportJob", reason="记录导出失败或等待重试，订单无需对外发送事件。"),
    Site(
        "integration_sync/delivery.py", "recover_interrupted_deliveries", KIND_UPDATE, "stopped",
        label=OTHER, model="OpenApiDelivery",
        reason="恢复业务应用回调的待投递状态，修改的是投递台账。",
    ),
    Site(
        "openapi/api/integration_orders.py", "IntegrationOrderActionView.post", KIND_ASSIGN, "published",
        label=SKIP, model="Order",
        reason="在内存中的订单副本上模拟发大厅状态，用于判断是否会同步到外部平台。",
    ),
    Site("business/approvals.py", "decide_approval", KIND_ASSIGN, "approval",
         label=OTHER, model="Approval", reason="审批结果由审批记录保存，罚款订单独立走正常状态流转。"),
    Site("wecom/notifications.py", "queue_payment_mismatch", KIND_CREATE, "WecomNotification", count=2,
         label=OTHER, model="WecomNotification", reason="企微消息记录的发送状态。"),
    Site("wecom/notifications.py", "_recover_invalidated_payment_notices", KIND_CREATE, "WecomNotification",
         label=OTHER, model="WecomNotification", reason="企微消息记录的发送状态。"),
    Site("wecom/notifications.py", "_fail", KIND_ASSIGN, "row",
         label=OTHER, model="WecomNotification", reason="企微消息记录的发送状态。"),
    Site("wecom/notifications.py", "_retry_lookup", KIND_ASSIGN, "row",
         label=OTHER, model="WecomNotification", reason="企微消息记录的发送状态。"),
    Site("wecom/notifications.py", "_run_locked", KIND_ASSIGN, "row",
         label=OTHER, model="WecomNotification", reason="企微消息记录的发送状态。"),
    Site("wecom/aftersales.py", "set_settings", KIND_ASSIGN, "row", count=2,
         label=OTHER, model="WecomAftersalesSettings", reason="管理员保存售后设置后的规则同步状态。"),
    Site("wecom/aftersales.py", "sync_settings", KIND_ASSIGN, "row", count=4,
         label=OTHER, model="WecomAftersalesSettings", reason="后台同步企微售后规则后的配置状态。"),
    Site("business/online_payroll.py", "apply_transfer_result", KIND_ASSIGN, "row",
         label=OTHER, model="OnlinePayrollTransfer", reason="转账记录状态。"),
    Site("business/online_payroll.py", "begin_submission", KIND_ASSIGN, "row",
         label=OTHER, model="OnlinePayrollTransfer", reason="转账记录制单标记。"),
    Site("business/online_payroll.py", "record_submission", KIND_ASSIGN, "row",
         label=OTHER, model="OnlinePayrollTransfer", reason="转账记录制单结果。"),
    Site("business/online_payroll.py", "begin_sealing", KIND_ASSIGN, "gateway",
         label=OTHER, model="OnlinePayrollGatewayBatch", reason="转账批次状态。"),
    Site("business/online_payroll.py", "cancel_batch", KIND_UPDATE, "OnlinePayrollTransfer",
         label=OTHER, model="OnlinePayrollTransfer", reason="关闭从未发送的付款单。"),
    Site("business/online_payroll.py", "cancel_batch", KIND_UPDATE, "OnlinePayrollGatewayBatch",
         label=OTHER, model="OnlinePayrollGatewayBatch", reason="关闭从未发送的支付批次。"),
    Site("business/online_payroll.py", "record_sealed", KIND_ASSIGN, "gateway", count=2,
         label=OTHER, model="OnlinePayrollGatewayBatch", reason="上游付款码结果。"),
    Site("business/online_payroll.py", "request_payment_refresh", KIND_ASSIGN, "gateway",
         label=OTHER, model="OnlinePayrollGatewayBatch", reason="重新查询原批次付款码。"),
    Site("business/api/alipay_authorization_views.py", "_invoke_alipay_control", KIND_ASSIGN, "row",
         label=OTHER, model="AlipaySettlementAuthorization", reason="付款账号授权记录自身的状态，不是订单状态。"),
    Site("settlement_control/views.py", "_authorize", KIND_UPDATE, "Authorization",
         label=OTHER, model="Authorization", reason="集中服务的付款授权状态，不是订单状态。"),
    Site("settlement_control/views.py", "_finish_refresh", KIND_UPDATE, "Authorization", count=3,
         label=OTHER, model="Authorization", reason="集中服务的付款授权状态，不是订单状态。"),
    Site("settlement_control/views.py", "_identity_operation", KIND_ASSIGN, "active", count=2,
         label=OTHER, model="Authorization", reason="付款身份及解除授权的状态，不是订单状态。"),
    Site("settlement_control/tests.py", "IdentityAndRevokeFlowTests.setUp", KIND_UPDATE, "Authorization",
         label=OTHER, model="Authorization", reason="付款授权测试数据的状态，不是订单状态。"),
    Site("settlement_control/management/commands/reconcile_settlement_attempt.py", "Command.handle", KIND_UPDATE, "AlipaySettlementAuthorization",
         label=OTHER, model="AlipaySettlementAuthorization", reason="付款授权人工核对后的状态，不是订单状态。"),
    # ── qqbot 线（D-0265）：QQBotGroup.status 两处（非订单状态、有意不推）──
    Site(
        "qqbot/events.py", "_upsert_group", KIND_ASSIGN, "group",
        label=OTHER, model="QQBotGroup",
        reason=(
            "群登记 upsert 时把 status 置 active（进群事件/@消息路径）。这是机器人自己"
            "的群档案状态，不是订单状态——无订单事件可言。"
        ),
    ),
    Site(
        "qqbot/events.py", "_on_group_del_robot", KIND_ASSIGN, "group",
        label=OTHER, model="QQBotGroup",
        reason=(
            "机器人被移出群 ⇒ status 置 gone。同上，群档案状态、非订单状态。"
        ),
    ),
    Site(
        "qqbot/worker.py", "GroupSender._handle_row", KIND_ASSIGN, "row",
        label=OTHER, model="QQBotOutboundMessage",
        reason="发送成功 ⇒ row.status=sent（发件箱自己的生命周期，非订单状态）。",
    ),
    Site(
        "qqbot/worker.py", "GroupSender._send", KIND_ASSIGN, "row",
        label=OTHER, model="QQBotOutboundMessage",
        reason="发送中/降级路径的状态流转（发件箱生命周期，非订单状态）。",
    ),
    Site(
        "qqbot/worker.py", "GroupSender._mark_failed", KIND_ASSIGN, "row",
        label=OTHER, model="QQBotOutboundMessage",
        reason="终态失败（日限/超试次）⇒ row.status=failed（发件箱生命周期，非订单状态）。",
    ),
    # ── 推送：状态变化 ────────────────────────────────────────────────────────────
    Site(
        "business/order_status_persistence.py", "persist_order_status", KIND_ASSIGN, "order",
        label=PUSH, model="Order",
        reason="本地流转 apply_transition 和状态回传 apply_upstream_events 在各自事务内保存后入队。",
    ),
    Site(
        "business/jidanpu_upstream.py", "_promote_accepted_pool_order", KIND_ASSIGN, "order",
        label=SKIP, model="Order",
        reason=(
            "补齐上家已确认的同次大厅接单。复用转正的 set_taker、order.claim 审计、鸡蛋铺交单待办和群聊，"
            "沿用上家大厅接单的回调范围，不新增开放平台或打手开放接口状态回调。"
        ),
    ),
    # 租户级鸡蛋铺线已拆，不存在的写入点不登记。这不是放宽：本网只要求登记还在的落点。
    Site(
        "business/peer_inbound.py", "apply_events", KIND_ASSIGN, "order", count=4,
        label=PUSH, model="Order",
        reason=(
            "上下游同步接收侧**四处**直接写 `order.status`：`order.update` 支 1 处 + "
            "`order.assign` / `order.grab` / `order.withdraw` 支 3 处（同一 `else` 的三种 kind）。"
            "它们拿到的都是**真实的本地状态变化**（§7.2.1 裁决 1）⇒ 全推；"
            "同一状态在一个 pending 窗口里重复进来由 `dispatch._is_duplicate` 去重。"
        ),
    ),
    # ── 推送：新增进度图（`status='ready'` 的三个写入点）──────────────────────────────
    Site(
# ⭐ 内网认领与对外开放平台的认领端点调**同一个**领域函数 `claim_screenshot` ⇒ 这处 `row.status = …` 登记在该函数的作用域名下。
# ⭐ 这条写入点同时服务两条路，仍只算**一个**落点（两条路共用同一份实现，⛔ 没有第二处 `row.status = …`）；
# 判据没放宽：同一个文件、同一条 `assign`、同一个对象名、同标签与同模型；本网是两向恰好集合 ⇒ 作用域名/形状/对象一变而登记没跟上就红。
        "business/api/screenshot_views.py", "claim_screenshot", KIND_ASSIGN, "row",
        label=PUSH, model="OrderScreenshot",
        reason=(
            "进度图变 `ready` 的写入点①（认领；内网与对外开放平台共用这**一份**实现）。"
            "它与既有 peer 广播**并列**接在同一个 "
            "`if row.img_type == IMG_ORDER` 里（群聊二维码不推：第三方没有它的语义）。"
        ),
    ),
    Site(
        "business/peer_inbound.py", "_apply_screenshot", KIND_CREATE, "OrderScreenshot",
        label=PUSH, model="OrderScreenshot",
        reason=(
            "写入点②（上下游同步接收侧）：今天**不走**既有入队函数 ⇒ T8 在这里显式接一句。"
            "上面那条并发 `IntegrityError` 幂等分支不经过这里 ⇒ 重复事件不会重复推。"
        ),
    ),
    Site(
        "business/jidanpu_upstream_pics.py", "store_remote_pics", KIND_CREATE, "OrderScreenshot",
        label=PUSH, model="OrderScreenshot",
        reason=(
            "写入点③（把远端那张单上的图拉回来入库）：同样不走既有入队函数；`IntegrityError` "
            "幂等分支 `continue` 掉 ⇒ 不重复推。"
            "⚠️ **2026-10-09 改准**（原登记的作用域名是 `pull_order_screenshots`）：那颗 "
            "`create()` 随「读列表之后那一整段（下载 / 落库 / 幂等）」**整段抽到** "
            "`store_remote_pics`（⛔ 没有第二处 `create()`，也确实**没有多一个**落点 —— "
            "`pull_order_screenshots`、②式回调 `action=30`、②式官方 API 全量拉图**三条路共用这一份**）"
            "⇒ 登记的作用域名跟着搬，**判据一个字没放宽**（仍是同一个文件、同一个模型、"
            "同一个 `KIND_CREATE`；本网是两向恰好集合 ⇒ 不搬就红）。"
        ),
    ),
    # ⭐⭐ **写入点④（业务方拍板补推）**：代练通打手进度图拉回入库——②③的同族（外部平台的图变成**本地 ready 行**，
    #    §7.2.1 裁决 3 的「回拉到的」那一类）。两族派发接在 savepoint 块内 `create()` 之后 ⇒
    #    幂等 `IntegrityError` 分支与循环开头 `exists()` 预查都不经过、不重复推；打手侧收件人 = **当前接手人**（代练通外部打手不在本租户 ⇒ 常态零行）。
    Site(
        "business/dailiantong_progress_sync.py",
        "sync_progress_screenshots_for_push", KIND_CREATE, "OrderScreenshot",
        label=PUSH, model="OrderScreenshot",
        reason=(
            "写入点④（代练通打手进度图拉回入库）：外部平台的图变成本地 ready 行，"
            "与②③同族 ⇒ 按 §7.2.1 裁决 3 全推（业务方 2026-10-28 拍板补推）。"
            "两族派发接在 savepoint 块内 `create()` 之后：`IntegrityError` 幂等分支"
            "与循环开头 `exists()` 预查都不经过 ⇒ 并发 / 重复同步不重复推"
            "（`tests/test_dailiantong_progress_sync.py::ProgressSyncOpenapiPushTests` 三条钉住）。"
        ),
    ),
    # ── 有意不推：6 处「出生即带状态」的 create（§7.2.1 裁决 2）──────────────────────
    Site(
        "business/api/order_views.py", "OrderViewSet.copy", KIND_CREATE, "Order",
        label=SKIP, model="Order",
        reason="建单（复制单）：`status=UNASSIGNED` 是**出生状态**，不是状态变化；第三方感知新建单靠它自己那次录单的应答。",
    ),
    # ⭐ 拆单：详情页 `POST /orders/<单号>/split/` 落子单那处 `Order.objects.create(status=UNASSIGNED, …)` ⇒ **有意不推**，依据可重跑：
    #    ① `status=UNASSIGNED` 是**出生状态**（§7.2.1 裁决 2「建单不是状态变化」，与上一格 `OrderViewSet.copy` 同形）；② 出网事件词表**没有**「建单」类（`apps/business/peer_dispatch.py` 的五个入口）；③ 录单唯一入口也不推建单事件（`apps/business/order_intake.py::create_order` 落库是 `serializer.save()`，唯一出网入队是「抛内部抢单大厅」那一支）⇒ 拆单与录单口径一致；
    #    ④ 单独给拆单推 ⇒ 订阅方收得到「拆出来的单」、却收不到手工录/复制的单（同一件事三种入口只推一种，比不推更糟）。✅ ①②③ 由 `test_the_split_create_site_still_does_not_push` 钉住（作用域里出现入队调用 ⇒ 红，逼人回来重判这条登记）。
    Site(
        "business/api/order_views.py", "OrderViewSet.split_local_order", KIND_CREATE, "Order",
        label=SKIP, model="Order",
        reason=(
            "建单（拆单落子单）：`Order.objects.create(status=UNASSIGNED, …)` 是**出生状态**、"
            "不是状态变化 ⇒ 按 §7.2.1 裁决 2「建单不推」，与上一格（复制单）同口径。"
            "⚠️ **不是「忘了接」**：全仓没有「建单」类事件——出网的订单事件**恰好六种**"
            "（`peer_dispatch.py` 的模块 docstring 逐条点名：assign / grab / withdraw / update / "
            "screenshot.add / screenshot.delete），而录单那条唯一入口 `order_intake.create_order` "
            "同样不推建单事件（它唯一的出网入队是「抛大厅」那一支的 `queue_hall_launch`）"
            "⇒ 拆单与录单口径一致；只给拆单推会让订阅方只看到拆出来的单。"
            "⚠️ 另一条边界：本端点改的是当前单的 `project_text`，而 `order.update` 那条事件的"
            "口径是「**状态 + 发单价**」（`notify_order_price_changed` 的 docstring）⇒ 也不推。"
        ),
    ),
    Site(
        "business/api/order_views.py", "OrderViewSet.split_priced_order", KIND_CREATE, "Order",
        label=SKIP, model="Order",
        reason=(
            "并列拆单新建订单时 `UNASSIGNED` 是出生状态；订单事件词表没有建单事件，"
            "只有按调用上下文启用的本地企微账单重核，没有为子单单独入队出网事件。"
        ),
    ),
    # ⚠️ D-0324：建单语句从 `record_assigned_order` 搬进它的建单核心
    #    `_create_pool_order`（复活重录与首次录池共用同一份投影）——判据 / 理由一字未改。
    Site(
        "business/jidanpu_upstream.py", "_create_pool_order", KIND_CREATE, "Order",
        label=SKIP, model="Order",
        reason="建单（上家派单落成我方新单）：出生即带状态，同上。",
    ),
    # ⛔ **撤条（D-0325 口径）**：`promote_upstream_mirror` 的 KIND_CREATE 登记已删——转正改成「复用隐藏单优先」，
    #    两支字段（含 `status`）收进**共用**的 `promote_fields` 字典、`create(**promote_fields)` 展开 ⇒ 状态字面量不在调用关键字里，本网形状**扫不到**（留条 = 永不命中的白名单，比不登记更糟）。
    #    行为判据不丢：`tests.test_upstream_hall_shadow.PromoteReuseTests` 与 `test_jidanpu_upstream_hall` 的转正用例断言库里 `IN_PROGRESS`（与 `test_order_time_fields` 同口径撤条）。
    Site(
        "business/peer_inbound.py", "apply_events", KIND_CREATE, "Order",
        label=SKIP, model="Order",
        reason=(
            "建单（上下游首单 / 补回来的单）：出生即带状态，同上。⚠️ 与同一作用域里那 4 处 "
            "`order.status = …`（推送）是**两件事**：那 4 处改的是**已在库的单**。"
        ),
    ),
    # ── 非目标模型（别的表的同名列；与推送无关，但必须点名，⛔ 不许匿名放过）─────────────
    Site("accounts/wxpusher_binding.py", "send_test_notice", KIND_CREATE, "WxPusherNotice",
         label=OTHER, model="WxPusherNotice",
         reason="记录已经开始尝试的测试通知，只写通知队列自身的状态。"),
    Site("accounts/wxpusher_binding.py", "send_test_notice", KIND_ASSIGN, "notice",
         label=OTHER, model="WxPusherNotice", count=2,
         reason="记录测试通知是否获受理，只写通知队列自身的状态。"),
    Site("accounts/wxpusher_notifications.py", "_queue_delivery", KIND_CREATE, "WxPusherNotice",
         label=OTHER, model="WxPusherNotice",
         reason="写入业务通知及初始发送状态，只写通知队列自身的状态。"),
    Site("accounts/wxpusher_notifications.py", "_reserve_delivery", KIND_ASSIGN, "notice",
         label=OTHER, model="WxPusherNotice", count=2,
         reason="保存手机通知是否跳过或开始发送，只写通知队列自身的状态。"),
    Site("accounts/wxpusher_notifications.py", "_reserve_chat_delivery", KIND_ASSIGN, "notice",
         label=OTHER, model="WxPusherNotice", count=2,
         reason="保存聊天通知是否跳过或随合并批次开始发送，只写通知队列。"),
    Site("accounts/wxpusher_notifications.py", "_reserve_chat_delivery", KIND_UPDATE, "WxPusherNotice",
         label=OTHER, model="WxPusherNotice", count=2, reason="批量保存聊天通知的跳过或发送预约状态。"),
    Site("accounts/wxpusher_notifications.py", "_reserve_hall_delivery", KIND_ASSIGN, "notice",
         label=OTHER, model="WxPusherNotice", count=2, reason="标记大厅通知跳过或开始发送。"),
    Site("accounts/wxpusher_notifications.py", "_reserve_hall_delivery", KIND_UPDATE, "WxPusherNotice",
         label=OTHER, model="WxPusherNotice", count=2, reason="批量保存大厅通知的跳过或发送预约状态。"),
    Site("accounts/wxpusher_notifications.py", "_send_hall_delivery", KIND_ASSIGN, "notice",
         label=OTHER, model="WxPusherNotice", reason="按收件人的回执标记大厅通知结果。"),
    Site("accounts/wxpusher_notifications.py", "_send_hall_delivery", KIND_UPDATE, "WxPusherNotice",
         label=OTHER, model="WxPusherNotice", reason="批量保存大厅通知结果。"),
    Site("accounts/wxpusher_notifications.py", "_send_personal_delivery", KIND_UPDATE, "WxPusherNotice",
         label=OTHER, model="WxPusherNotice", reason="保存业务通知或群聊合并批次的受理结果。"),
    Site("business/jidanpu_member.py", "_pull_deferred_pics", KIND_UPDATE, "pending",
         label=OTHER, model="JidanpuSyncEvent",
         reason="拉图每轮限流后把待办台账行收口（`member.status_in` 那句人话由 skipped 改 done）；"
                "只写台账，不改订单/截图状态。⭐ 2026-10-26（拆节奏 + 拉图限流）登记。"),
    Site("business/api/jidanpu_member_views.py", "_converge_in_flight_events", KIND_UPDATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="事件台账的终态收敛（`JidanpuSyncEvent.status`），与订单状态无关。"),
    Site("business/api/jidanpu_upstream_views.py", "_converge_in_flight_events", KIND_UPDATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="同上（上家线那一半）。"),
    # ── 回调重试上限：计次台账的两处写入（create / update），与订单状态无关 ──
    Site("business/jidanpu_openapi_consume.py", "_note_callback_failure", KIND_CREATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent",
         reason="回调失败按事件计次（新建一行台账，kind=openapi.callback_retry）；只写台账，不改订单状态。"),
    Site("business/jidanpu_openapi_consume.py", "_note_callback_failure", KIND_UPDATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent",
         reason="同上：计次自增与到达上限后的「已放弃」标记；只写台账，不改订单状态。"),
    # `write_event` 在 `jidanpu_common`。租户级收敛模块已删，不留空登记。
    Site("business/jidanpu_common.py", "write_event", KIND_CREATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="鸡蛋铺同步事件的 outbox 写入（`status=pending`）。⚠️ 2026-10-08 从 `jidanpu_hall.py` 原样搬来。"),
    Site("business/jidanpu_member.py", "_finish", KIND_UPDATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="事件收口。"),
    Site("business/jidanpu_direct_taker.py", "_finish", KIND_UPDATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="记录鸡蛋铺直派处理结果，订单状态未变更。"),
    Site("business/jidanpu_first_assignment.py", "_reserve", KIND_ASSIGN, "retained",
         label=OTHER, model="JidanpuSyncEvent", reason="复用明确拒绝的录单事件，将同步台账重新标为待处理。"),
    Site("business/jidanpu_first_assignment.py", "_reject_assignment", KIND_ASSIGN, "event",
         label=OTHER, model="JidanpuSyncEvent", reason="记录鸡蛋铺明确拒绝指派，将同步台账标为失败并保留录单草稿。"),
    Site("business/jidanpu_member.py", "_retry", KIND_UPDATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="失败重排。"),
    Site("business/jidanpu_member.py", "_retry", KIND_ASSIGN, "event",
         label=OTHER, model="JidanpuSyncEvent", reason="同一个 `_retry` 里的内存赋值（`event` 是 `JidanpuSyncEvent` 实例）。"),
    Site("business/jidanpu_member.py", "_queue_member_event", KIND_UPDATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", count=2,
         reason="在飞任务重开额度，或在新一轮重抛时收口旧任务。"),
    Site("business/jidanpu_member.py", "_reset_exhausted_event", KIND_ASSIGN, "event",
         label=OTHER, model="JidanpuSyncEvent", reason="重开已用尽额度的同步事件。"),
    Site("business/jidanpu_member.py", "queue_withdrawals", KIND_CREATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="为已离厅的隐藏单登记下家撤回任务。"),
    Site("business/jidanpu_member.py", "queue_withdrawals", KIND_UPDATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="批量复位已用尽额度的撤回任务。"),
    Site("business/jidanpu_upstream.py", "_finish", KIND_UPDATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="事件收口（上家线那份）。"),
    Site("business/jidanpu_upstream.py", "_queue_out_event", KIND_UPDATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="pending 合并时写回。"),
    Site("business/jidanpu_upstream.py", "_queue_out_event", KIND_ASSIGN, "existing",
         label=OTHER, model="JidanpuSyncEvent", reason="同上那一句的内存赋值。"),
    Site("business/jidanpu_upstream.py", "_record", KIND_CREATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="事件写行。"),
    Site("business/jidanpu_upstream.py", "_retry", KIND_UPDATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="失败重排。"),
    Site("business/jidanpu_upstream.py", "_retry", KIND_ASSIGN, "event",
         label=OTHER, model="JidanpuSyncEvent", reason="同 `_retry` 的内存赋值。"),
    # ⭐ 打手 App 按需拉图：手工请求行的**结论写回**。它写的是
    # `JidanpuSyncEvent` 自己的 status / reason / processed_at（台账行就是留痕），
    # 与订单 / 进度图状态无关 ⇒ 非目标模型；不推送、也不该推送。
    Site("business/jidanpu_upstream.py", "_finish_pull_pics_request", KIND_UPDATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent",
         reason="手工拉图请求的结论写回（status / reason / processed_at），台账自身状态。"),
    Site("business/jidanpu_upstream.py", "_finish_pull_pics_request", KIND_ASSIGN, "event",
         label=OTHER, model="JidanpuSyncEvent",
         reason="同一处写回之后的**内存赋值**（`event` 是 `JidanpuSyncEvent` 实例）。"),
    # `RemoteHallOrder` 已删。上家镜像行 `JidanpuUpstreamHallOrder` 还在，下面继续点名。
    Site("business/jidanpu_upstream.py", "_resolve_taken_claim", KIND_UPDATE, "JidanpuUpstreamHallOrder",
         label=OTHER, model="JidanpuUpstreamHallOrder",
         reason="请求和后台共用的上家拒绝处理，只下架仍匹配本次预占的镜像卡。"),
    Site("business/jidanpu_upstream.py", "_converge_promoted", KIND_UPDATE, "JidanpuUpstreamHallOrder",
         label=OTHER, model="JidanpuUpstreamHallOrder", reason="同上。"),
    Site("business/management/commands/rescue_gone_mirrors.py", "Command._rescue_schema",
         KIND_UPDATE, "JidanpuUpstreamHallOrder",
         label=OTHER, model="JidanpuUpstreamHallOrder",
         reason="E-0271 救回命令（D-0274 ①，2026-10-20）：把「判 gone 后又被拉取轮见到」的镜像行"
                "条件更新回 `listed`、清 `gone_at`——此刻还没有我方订单，无出站业务事件可推"
                "（有意不推；救回后的正常拉取/认领照走既有写点）。"),
    Site("business/jidanpu_upstream.py", "_mark_gone", KIND_UPDATE, "JidanpuUpstreamHallOrder",
         label=OTHER, model="JidanpuUpstreamHallOrder", reason="同上。"),
    Site("business/jidanpu_upstream.py", "claim_upstream_mirror", KIND_UPDATE, "JidanpuUpstreamHallOrder",
         label=OTHER, model="JidanpuUpstreamHallOrder", count=2,
         reason="上家大厅镜像行。⚠️ **2026-10-29 起 count = 2**：第 1 处 = `listed → claiming`"
                " 的本地预占；第 2 处 = 归属绑定停用 / 缺失的僵尸卡**命中即下架**"
                "（`listed → gone`，业务方报障修法，照 `_mark_gone` 的条件更新形态）"
                "——两处都是镜像行自己的状态列，仍不是我方订单、有意不推。"),
    Site("business/jidanpu_upstream.py", "_claim_recheck_defuse_gone", KIND_UPDATE, "JidanpuUpstreamHallOrder",
         label=OTHER, model="JidanpuUpstreamHallOrder",
         reason="点卡先探一跳（僵尸窗治理二期③，2026-11-05）：接单前滞卡段查复核答「上游已无」"
                "⇒ 条件更新 `listed → gone` 就地下架（照停绑拆雷支与 `_mark_gone` 的形态）。"
                "镜像行自己的状态列，不是订单状态、有意不推。"),
    Site("business/jidanpu_upstream.py", "batch_unlist_orphan_hall_rows", KIND_UPDATE, "JidanpuUpstreamHallOrder",
         label=OTHER, model="JidanpuUpstreamHallOrder",
         reason="停绑/换绑/删号生效点批量下架孤儿镜像卡（僵尸窗治理⑤，2026-11-05）：接管保护筛真孤儿"
                "⇒ 条件更新 `listed → gone`（照停绑拆雷支与 `_mark_gone` 的形态）。"
                "镜像行自己的状态列，不是订单状态、有意不推。"),
    Site("business/jidanpu_upstream.py", "_promote_accepted_pool_order", KIND_UPDATE, "reservation",
         label=OTHER, model="JidanpuUpstreamHallOrder", queryset_caller="promote_upstream_mirror",
         reason="本地订单另行处理时只关闭旧镜像预占，保留正式订单及其接手人。"),
    Site("business/jidanpu_upstream.py", "promote_upstream_mirror", KIND_UPDATE, "reservation",
         label=OTHER, model="JidanpuUpstreamHallOrder", reason="确认抢单后将仍匹配本次预占的镜像卡转正。"),
    Site("business/jidanpu_upstream.py", "batch_unlist_orphan_hall_rows", KIND_CREATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="批量记录本地镜像卡下架。"),
    Site("business/jidanpu_upstream.py", "sync_hall", KIND_CREATE, "JidanpuUpstreamHallOrder",
         label=OTHER, model="JidanpuUpstreamHallOrder", reason="镜像行入库（出生状态）。"),
    Site("business/jidanpu_upstream.py", "sync_hall", KIND_UPDATE, "JidanpuUpstreamHallOrder",
         label=OTHER, model="JidanpuUpstreamHallOrder", count=3,
         reason="上家镜像行的回环下架、已匹配镜像重新入厅、后台释放抢单锁。"),
    Site("business/jidanpu_upstream.py", "_admit_unmapped_card", KIND_UPDATE, "JidanpuUpstreamHallOrder",
         label=OTHER, model="JidanpuUpstreamHallOrder",
         reason="未匹配的镜像首次入厅，按配置记价格比例。"),
    Site("business/jidanpu_upstream.py", "record_assigned_order", KIND_ASSIGN, "card",
         label=OTHER, model="JidanpuUpstreamHallOrder",
         reason="上家直接指派后收起同一张大厅镜像卡。"),
    Site("business/jidanpu_upstream.py", "_release_claiming_to_hall", KIND_UPDATE, "JidanpuUpstreamHallOrder",
         label=OTHER, model="JidanpuUpstreamHallOrder",
         reason="D-0335（2026-10-30）消锁定：请求路接单失败当场把 `claiming` 条件更新放回"
                " `listed`（清两接单列 + 写 last_error）——镜像行自己的状态列，"
                "仍不是我方订单、有意不推（后台回收半边 = sync_hall）。"),
    Site("business/peer_direct_taker.py", "receive_assignment", KIND_CREATE, "PeerSyncEvent",
         label=OTHER, model="PeerSyncEvent", reason="记录下家直派回执结果供幂等重试。"),
    Site("business/peer_direct_taker.py", "_finish_assignment", KIND_CREATE, "PeerSyncEvent",
         label=OTHER, model="PeerSyncEvent", reason="记录上家直派完成及对应回执，供核对和恢复使用。"),
    Site("business/peer_worker.py", "_send_pending_event", KIND_UPDATE, "PeerSyncEvent",
         label=OTHER, model="PeerSyncEvent",
         reason="同步台账按当前租约和版本条件更新为终态 `failed`。"),
    Site("business/peer_worker.py", "_mark_event_done", KIND_UPDATE, "PeerSyncEvent",
         label=OTHER, model="PeerSyncEvent", reason="同步台账条件更新为 `done`。"),
    Site("business/peer_worker.py", "_mark_event_dropped", KIND_UPDATE, "PeerSyncEvent",
         label=OTHER, model="PeerSyncEvent", reason="绑定失效时条件更新同步台账为 `failed`。"),
    Site("business/peer_worker.py", "run_reconcile", KIND_UPDATE, "PeerSyncEvent",
         label=OTHER, model="PeerSyncEvent", reason="对账按原快照条件更新同步台账为 `pending`。"),
    Site("business/peer_worker.py", "_rescue_stale_upstream_events", KIND_UPDATE, "PeerSyncEvent",
         label=OTHER, model="PeerSyncEvent",
         reason="超龄的上行台账按原快照重排为 `pending`，保留尝试次数以限制救援次数。"),
    Site("internal_chat/peer_chat.py", "_detach", KIND_UPDATE, "PeerSyncEvent",
         label=OTHER, model="PeerSyncEvent",
         reason="共享关系结束后作废未投递的聊天出站行，阻止继续向原绑定发送。"),
    Site("internal_chat/peer_chat.py", "_pull", KIND_UPDATE, "_pending_inbox",
         label=OTHER, model="PeerSyncEvent",
         reason="拉取时关闭本地订单已不存在的聊天入站行，无需再向外投递。"),
    Site("internal_chat/peer_chat.py", "ServicePeerChatView.post", KIND_UPDATE, "_pending_inbox",
         label=OTHER, model="PeerSyncEvent",
         reason="聊天核心确认已持久处理的入站行，接收回执只记本地进度，无需回送。"),
    # ⭐ `hall.*` 事件烧完退避梯进终态 `failed` 时，克隆重投一条救援事件（`meta.rescued=true`、attempts=0、每条至多一次）。
    #    救援事件走**同一条投递路**（`claim_due_events` 正常取活）⇒ 它就是要被推的 outbox 行本体，不是「有意不推」。
    Site("business/peer_worker.py", "_rescue_hall_event", KIND_CREATE, "PeerSyncEvent",
         label=OTHER, model="PeerSyncEvent",
         reason="救援重投行入库（B1/M-4：`hall.*` 终态后的一次性半功率重投；"
                "克隆体照常投递，无需单独接推送）。"),
    # ⭐ D-0303：`enqueue_hall_event` 排 `hall.take` 时把同键未投的 `hall.throw` **当场作废**（`pending → done` + 人话原因）
    #    ——投递最老优先、去重不刷 created_at；不作废会让「throw(未投) → take → 再抛」把实挂的单错钉成 gone。台账自己的状态，与订单推送无关。
    #    ⚠️ 作废逻辑住在 `_void_pending_hall_throw`（镜像行广播 `enqueue_hall_mirror_event` 共用同一份）⇒ 作用域名跟着实现走，判据没放宽（同文件、同形状、同模型；⛔ 没有第二处 `pending` 的 status 更新）。
    Site("business/peer_dispatch.py", "_void_pending_hall_throw", KIND_UPDATE,
         "PeerSyncEvent",
         label=OTHER, model="PeerSyncEvent",
         reason="outbox 台账自己的状态（take 作废同键未投 throw，M1；P4 抽成两条入队路共用的"
                "一处实现），不是订单状态。"),
    Site("openapi/dispatch.py", "_attempt", KIND_ASSIGN, "row",
         label=OTHER, model="OpenApiDelivery", reason="开放平台推送台账自己的状态（`row` 是 `OpenApiDelivery`）——⛔ 不是订单状态。"),
    Site("openapi/dispatch.py", "_mark_terminal", KIND_ASSIGN, "row",
         label=OTHER, model="OpenApiDelivery", reason="同上（推送行转终态）。"),
    # ⭐⭐ `prepare_retry` 是两族**共用**的手工重投件（重投收在一处共用实现）⇒ 它写的是 `spec.delivery_model` 的那个实例：可能是 `OpenApiDelivery`，也可能是 `TakerApiDelivery`。
    #    ⚠️ 本网的登记**只能点名一个模型**（`Site.model` 是单值）⇒ 这里点一族，另一族的名已在 `OTHER_MODELS` 里；⛔ 别据此以为这条只服务上家侧。
    #    ⚠️ 它与两个视图登记**不是重复**：视图是**入口**、这条是**真正落笔**的地方。
    Site("openapi/dispatch.py", "prepare_retry", KIND_ASSIGN, "delivery",
         label=OTHER, model="OpenApiDelivery",
         reason="两族共用的手工重投件：把一条**投递失败**的推送行重排回 `pending`"
                "（`delivery` 是 `spec.delivery_model` 的实例 ⇒ 可能是 `OpenApiDelivery`"
                " 也可能是 `TakerApiDelivery`）——⛔ 不是订单状态。"),
# ⚠️ 视图层**没有** `delivery.status = …`（重投收进共用件 `apps/openapi/dispatch.py::prepare_retry`，两族视图都调它）⇒ 视图条目不登记：本网两向恰好断言会同时报「有写入点没登记」（新落点已登记）与「登记表里有已不存在的落点」⇒ 按本网纪律（表烂了就删，⛔ 别留假保证）。
#    手工重投确实会**间接**引发一次对外投递（常驻投递进程下一轮再投），但那不是「订单状态变化」事件 ⇒ 按本网语义（收 `Order.status` / `OrderScreenshot.status`）非目标；事件是否该入队由 `apps/openapi/dispatch.py` 那侧判。
#    反馈单的 `row.status = …` 扫不到模型名 ⇒ 处置状态必须点名进 `OTHER_MODELS`（不是订单状态、也不是进度图；判据仍是两向恰好集合）。
    Site("business/api/feedback_views.py", "FeedbackHandleView.patch", KIND_ASSIGN, "row",
         label=OTHER, model="Feedback",
         reason="反馈单的处置状态，与订单/进度图推送无关，故不推送"
                "（`row` 是 `Feedback`）。⛔ 不是订单状态。"),
    # ⭐⭐ 打手侧内部页的**手工重投**（spec `docs/specs/2026-10-09-taker-callback-design.md` §7）：落笔已收进共用件 `prepare_retry`（两族视图都调它）⇒ 视图里没有 `delivery.status = …`、视图条目不登记（两向恰好断言会同时报「新落点没登记」与「登记表里有已不存在的落点」）；
    #    判据 3 要求「非目标模型」**必须点名冻结枚举里的模型** ⇒ `TakerApiDelivery` 已在 `OTHER_MODELS`（⛔ 不许匿名放过）；它确实**间接**引发一次对外投递（常驻 `takerapi_dispatch` 下一轮再投一次），但那不是「订单状态变化」事件 ⇒ 非目标模型；⛔ 这一条**不改变**模块 docstring 登记的盲区（共用引擎 `_attempt` / `_mark_terminal` 那两句依旧分不出族）。
    #    ⭐ 频道文章作品线：`article_serializers.apply_status` 的 `article.status = new_status`（扫成 `('business/api/article_serializers.py', 'apply_status', 'assign', 'article')`）写的是 `ChannelArticle` 的**草稿/已发布**状态 ⇒ 按判据 3 点名「非目标模型」，`ChannelArticle` 已在 `OTHER_MODELS`（判据未放宽，仍是两向恰好）。
    Site("business/api/article_serializers.py", "apply_status", KIND_ASSIGN, "article",
         label=OTHER, model="ChannelArticle",
         reason="频道文章的草稿/已发布状态，与订单/进度图推送无关，故不推送"
                "（`article` 是 `ChannelArticle`）。⛔ 不是订单状态。"),
    # ⭐ D-0296，大厅模拟单：`HallSimulatedOrder` 的三个可扫落点——模拟单是**演示卡**（不建订单行、不进状态机 / 结算 / 对账），
    #    其 `active/retired` 状态与 `Order.status` / `OrderScreenshot.status` 无关
    #    ⇒ 按判据 3 点名「非目标模型」，`HallSimulatedOrder` 已在 `OTHER_MODELS`。
    Site("business/api/hall_sim_views.py", "HallSimClaimView.post", KIND_UPDATE,
         "HallSimulatedOrder",
         label=OTHER, model="HallSimulatedOrder",
         reason="模拟单假接手：演示卡 active → retired，不是订单状态，无出站事件"),
    Site("business/hall_simulated.py", "_create_from_source", KIND_CREATE,
         "HallSimulatedOrder",
         label=OTHER, model="HallSimulatedOrder",
         reason="模拟单生成：演示卡出生状态（active），不是订单状态"),
    Site("business/hall_simulated.py", "_recycle", KIND_UPDATE, "HallSimulatedOrder",
         label=OTHER, model="HallSimulatedOrder",
         reason="模拟单回收：在厅超时下架（active → retired），不是订单状态"),
    # ⭐ D-0303，团团通大厅镜像：`PeerHallMirror` 的五个可扫落点——镜像行是**上家单在下家的投影**（不是订单），
    #    其状态机服务「抢单确认制」，与 `Order.status` / `OrderScreenshot.status` 无关 ⇒ 按判据 3 点名「非目标模型」，`PeerHallMirror` 已在 `OTHER_MODELS`。
    #    （镜像行自己的对外通知就是 peer 线的 hall.* 事件，由上家侧触发点 `peer_dispatch.enqueue_hall_event` 排——本表语义是「写订单/进度图状态要不要推」，与那套广播互不覆盖。）
    Site("business/peer_inbound.py", "_hall_mirror_upsert_throw", KIND_CREATE,
         "PeerHallMirror",
         label=OTHER, model="PeerHallMirror", count=2,
         reason="镜像行入库（出生 listed）：投影行的状态，不是订单状态，无出站事件。"
                "⭐ **2026-10-25 重数 1 → 2（大厅慢路拉取，D-0304）**：拉取侧为接住"
                "「拉取 × 推送同卡」的竞态改用 `get_or_create`（本网把它的 create 面算作"
                "一处 create），另一处仍是推送支的 `create`；两处都是同一种「出生 listed」。"),
    Site("business/peer_inbound.py", "_hall_mirror_upsert_throw", KIND_UPDATE,
         "PeerHallMirror",
         label=OTHER, model="PeerHallMirror", count=2,
         reason="gone 行复活回 listed 的条件更新。⭐ **2026-10-25 重数 1 → 2（D-0304）**："
                "拉取侧 `get_or_create` 的 update 面 + 推送支原来的条件更新。"
                "claiming/promoted 的 `last_seen_at` 触碰与 listed 的投影刷新走 `**fields` "
                "展开、本网的形状扫不到——扫描边界，同模块 docstring 的盲区清单。"
                "都是投影行自己的状态，不是订单状态"),
    Site("business/peer_inbound.py", "mark_missing_hall_rows_gone", KIND_ASSIGN,
         "row", label=OTHER, model="PeerHallMirror",
         reason="⭐ 2026-10-25（D-0304）大厅慢路**拉全才判消失**：本轮完整 pass 没见到的"
                "陈旧 `listed` 卡逐行条件更新成 `gone`（并补发 `hall.take`）。"
                "镜像行自己的生命周期，不是订单状态。"),
    Site("business/peer_inbound.py", "mark_missing_hall_rows_gone", KIND_UPDATE,
         "PeerHallMirror", label=OTHER, model="PeerHallMirror",
         reason="⭐ 2026-10-25（D-0304）：上一条的**条件更新**本体（`pk + listed + 年龄`）。"
                "同一处判消失的另一半；投影行自己的状态，不是订单状态。"),
    # ── 镜像卡隐藏单：卡落地时建/复活那张单（状态一律 listed 派生，不出站事件）──
    Site("business/hall_shadow.py", "ensure_for_card", KIND_ASSIGN, "order",
         label=SKIP, count=1,
         reason="大厅卡落地时建隐藏单：状态按卡的 listed 形态派生（WAITING_GRAB），"
                "不是本地动作、也不产生订单事件（对端的卡由 hall.* 广播承载）。"),
    Site("business/hall_shadow.py", "ensure_for_card", KIND_UPDATE, "Order",
         label=SKIP, count=1,
         reason="复活/刷新已有隐藏单（gone→listed 的条件更新）：同上，同步驱动、不出站事件。"),
    Site("business/hall_shadow.py", "refund_for_card", KIND_UPDATE, "Order",
         label=SKIP, count=1,
         reason="卡离厅（gone）时把隐藏单条件更新成 REFUNDED：同步驱动的收尾，"
                "不产生订单事件（对端的退卡由 hall.take 承载）。"),
    # ── ⭐ D-0325 / D-0329：**上家**镜像卡隐藏单——与 `hall_shadow` 那三条**同形同判**（照抄模板、登记各自落点）。
    #    隐藏单不进面向人的读面、生命周期由镜像卡驱动（对端可见性由 member.launch / member.withdraw 事件承载）
    #    ⇒ 有意不推任何 openapi 订单事件。
    Site("business/upstream_hall_shadow.py", "ensure_for_card", KIND_ASSIGN, "order",
         label=SKIP, count=1,
         reason="上家大厅卡在厅时建隐藏单：复活支回写内存状态（WAITING_GRAB）——"
                "状态按卡的 listed 形态派生，不是本地动作、也不产生订单事件"
                "（对端可见性由 member.launch 承载）。"),
    Site("business/upstream_hall_shadow.py", "ensure_for_card", KIND_CREATE, "Order",
         label=SKIP, count=1,
         reason="上家大厅卡在厅时取/建隐藏单（get_or_create 的 create 面，"
                "出生即 WAITING_GRAB）：建单不是状态变化，且隐藏单有意不出站"
                "（同 hall_shadow 那条的判据）。"),
    Site("business/upstream_hall_shadow.py", "ensure_for_card", KIND_UPDATE, "Order",
         label=SKIP, count=1,
         reason="复活已有隐藏单（REFUNDED/软删 → WAITING_GRAB 的条件更新）："
                "同步驱动、不出站事件（重抛由 member.launch 承载）。"),
    Site("business/upstream_hall_shadow.py", "refund_for_card", KIND_UPDATE, "Order",
         label=SKIP, count=1,
         reason="上家卡离厅（gone 一族）时把隐藏单条件更新成 REFUNDED：同步驱动的收尾，"
                "不产生订单事件（下家副本的撤单由 member.withdraw 承载）。"),
    Site("business/upstream_hall_shadow.py", "refund_for_cards", KIND_UPDATE, "Order",
         label=SKIP, count=1,
         reason="批量下架时同样只退待抢隐藏单，外部鸡蛋铺撤回由 member.withdraw 承载。"),
    Site("business/upstream_hall_shadow.py", "refund_for_card", KIND_CREATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="记录本次引擎退款，供复活时区分人工退款。"),
    Site("business/upstream_hall_shadow.py", "refund_for_cards", KIND_CREATE, "JidanpuSyncEvent",
         label=OTHER, model="JidanpuSyncEvent", reason="批量记录各隐藏单本次引擎退款。"),
    Site("business/peer_inbound.py", "_apply_peer_order_fields", KIND_ASSIGN, "order",
         label=SKIP, count=1,
         reason="peer 入向事件把订单字段（含状态）写成对端给的值：对端事实到达，"
                "不是本地动作、也不回推给对方（否则回声）。"),
    Site("business/recycle.py", "restore_order", KIND_ASSIGN, "order",
         label=SKIP, model="Order",
         reason="回收站恢复：删除时在厅（WAITING_GRAB 且非隐藏单）⇒ 状态写回 UNASSIGNED，"
                "等业务方手工重抛（2026-10-27 拍板）。人工驱动的收尾、不是本地抛厅动作 "
                "⇒ 有意不推任何订单事件（本批也明令恢复侧不排重抛事件）。"),
    Site("business/peer_inbound.py", "_apply_hall_event", KIND_UPDATE, "PeerHallMirror",
         label=OTHER, model="PeerHallMirror", count=2,
         reason="镜像行条件更新（重数 = 2：第 1 处 = hall.take 的 listed → gone；"
                "第 2 处 = hall.claim_refused 按 gone_at 证据分两支"
                "（有证据 → gone、无证据 → listed，M2））。⚠️ **2026-10-25 重数 3 → 2**："
                "claim_refused 的两支合并成一处条件更新（同一 `_apply_hall_event` 内一条 update），"
                "扫描计数随实现收缩，不是登记漏项。⚠️ hall.take 对 claiming 行"
                "写 gone_at 证据那一支**不带 status**（状态不动，M2）⇒ 本网的形状"
                "扫不到它——扫描边界，同模块 docstring 的盲区清单。"),
    Site("business/peer_inbound.py", "_promote_claiming_mirror", KIND_UPDATE,
         "PeerHallMirror",
         label=OTHER, model="PeerHallMirror",
         reason="抢单确认制的转正回填（claiming → promoted + promoted_order）："
                "投影行自己的状态，不是订单状态。⚠️ 实现**只有这一处**、调用点在建单支与"
                "更新支各一次（hall 的回填必须两支都能到，见该函数 docstring）"),
    # ⭐ 接单卡回收：`reclaim_stuck_claims` 把卡死 5 分钟的 `claiming` 行条件更新回 `listed`（同时清四个接单列）；`_record_reclaim` 记一条**本地**台账。
    #    两者都是镜像行 / 台账自己的生命周期，与订单推送无关（⛔ 不是漏接推送：这一支**有意**零出站——协议里没有「撤销接单」这个动作，见该函数 docstring）。
    Site("business/peer_hall.py", "reclaim_stuck_claims", KIND_UPDATE, "PeerHallMirror",
         label=OTHER, model="PeerHallMirror",
         reason="卡死回收：`claiming` 超过 5 分钟且未转正 ⇒ 条件更新回 `listed` 并清四个"
                "接单列（`claiming_by` / `claiming_at` / `claim_downstream` / `claim_id`）。"
                "镜像行自己的生命周期，不是订单状态；**纯本地、零出站**。"),
    Site("business/peer_hall.py", "_record_reclaim", KIND_CREATE, "PeerSyncEvent",
         label=OTHER, model="PeerSyncEvent",
         reason="回收台账（`direction=\"in\"` / `status=done`）：我们登记的**已知事实**，"
                "不是待投递事件（worker 只取 `direction=out` 的 pending）⇒ 与推送无关。"),
    # ⭐ P4（多跳）：中间层占住镜像行（`listed → claiming` + `claim_downstream` / `claim_id`）。
    Site("business/peer_inbound.py", "_forward_mirror_claim", KIND_UPDATE,
         "PeerHallMirror",
         label=OTHER, model="PeerHallMirror",
         reason="多跳中间层占住下游要的那张卡（`listed → claiming` + 谁在要 + 本层转发标识）："
                "镜像行自己的状态机，不是订单状态；对外通知是随后转给下游的 `hall.*` 应答。"),
    Site("business/peer_hall.py", "claim_peer_mirror", KIND_UPDATE, "PeerHallMirror",
         label=OTHER, model="PeerHallMirror",
         reason="本地预占（listed → claiming + 抢单人）：投影行自己的状态，不是订单状态"),
    # ⭐ D-0347（同步抢单直连）：人类单跳的**请求内收口**两处——无结论时把行放回大厅、以及映射终局前的行读取；都只改**投影行自己的状态**（`claiming → listed`），不是订单状态；
    #    对外通知走既有协议（对端 outbox 的 `order.assign` / `hall.claim_refused` 照发，与本函数的放回无关），⛔ 不是漏接推送。
    Site("business/peer_hall.py", "claim_peer_mirror_sync", KIND_UPDATE, "PeerHallMirror",
         label=OTHER, model="PeerHallMirror",
         reason="同步抢单直推的终局映射（行读取 / 重取）：预占在 `claim_peer_mirror` 登记；"
                "本函数只在**对端判决**落地后重读行（promoted 由 `peer_inbound.apply_events` "
                "写、refused 由 refused 支写）——本函数本体对 `PeerHallMirror` 的写只有"
                " AST 扫到的 `update` = 无结论放回那一处（见 `_release_to_hall` 条）。"),
    Site("business/peer_hall.py", "claim_peer_mirror_sync._release_to_hall", KIND_UPDATE,
         "PeerHallMirror",
         label=OTHER, model="PeerHallMirror",
         reason="同步抢单给不出终局 ⇒ 当场放回大厅（`claiming → listed` + 清两接单列 + "
                "行上人话）。投影行自己的状态机（D-0347：给不出结论就放弃，迟到的判决由"
                "既有补派 / refused 支收口），不是订单状态，纯本地零出站。"),
    # ⭐ 关接收开关 / 解绑时**下架存量卡**（`listed → gone`），以及跳数超限丢弃时记的那条**本地**台账；两处都与订单推送无关（⛔ 不是漏接推送）：
    #    下架只改投影行自己的状态；台账是 `direction="in"` / `status=done` 的已知事实（worker 只取 `direction=out` 的 pending）。
    Site("business/peer_hall.py", "unlist_binding_mirrors", KIND_UPDATE, "PeerHallMirror",
         label=OTHER, model="PeerHallMirror",
         reason="关开关 / 解绑时批量下架：把这份绑定名下 `listed` 的镜像行标 `gone`"
                "（`claiming` 不动）。投影行自己的状态，不是订单状态"
                "（业务方 2026-10-24 裁定：关掉开关时存量镜像全部下架，不留僵尸卡）。"
                "⭐ B3（终审修复轮）：每张真的翻成 `gone` 的卡会照 `hall.take` 的形状"
                "**补发**给本租户的下家（多跳下游手里的 `listed` 卡也要消失）——那是"
                "镜像行自己的对外事件（`peer_dispatch.enqueue_hall_mirror_event`），"
                "与订单状态推送无关，不是漏接推送。"),
    Site("business/peer_inbound.py", "_record_hop_limit", KIND_CREATE, "PeerSyncEvent",
         label=OTHER, model="PeerSyncEvent",
         reason="跳数超限丢弃台账（`direction=\"in\"` / `status=\"done\"` / "
                "`kind=\"peer.hall_hop_limit\"`）：我们登记的**已知事实**，不是待投递事件"
                "（worker 只取 `direction=out` 的 pending）⇒ 与推送无关。"),
    # ⭐ 绑定停用（页面解绑 / 对端解绑通知）时，把这份绑定名下 `claiming` 的卡**逐行条件更新**回 `listed`（清四个接单列）+ 一条 `direction="in"` 台账。
    #    都是投影行 / 台账自己的生命周期，与订单推送无关（⛔ 不是漏接推送：这一支**有意**零出站——协议里没有「撤销接单」这个动作）。
    Site("business/peer_hall.py", "release_claiming_mirrors", KIND_UPDATE,
         "PeerHallMirror",
         label=OTHER, model="PeerHallMirror",
         reason="绑定停用 ⇒ 抢单未确认的卡退回 `listed`（⛔ 不是 `gone`：上家那张单可能还在厅）"
                "＋ 清四个接单列。投影行自己的状态，不是订单状态；**纯本地、零出站**。"),
    Site("business/peer_hall.py", "_record_claim_release", KIND_CREATE, "PeerSyncEvent",
         label=OTHER, model="PeerSyncEvent",
         reason="绑定停用的退回台账（`direction=\"in\"` / `status=done`）：我们登记的**已知事实**，"
                "不是待投递事件（worker 只取 `direction=out` 的 pending）⇒ 与推送无关。"),
)


@dataclass(frozen=True)
class InvisibleSite:
    """AST **看不见**、但必须显式登记的写入点（判据 4）。"""
    path: str
    scope: str
    kind: str
    owner: str
    label: str
    model: str
    reason: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.path, self.scope, self.kind, self.owner)


#: ⚠️ 今天**恰好一条**：夹具单的状态藏在**变量**里（`get_or_create(..., defaults=fields)`，`fields` 由 `_fixture_fields` 造、里面有 `"status"`）
#: ⇒ 上面四条形状一条都扫不到它（它们只看 `create(status=…)` / `.update(status=…)` / 字典字面量）。
#: ⛔ 不许因为它扫不到就当它不存在：§7.2.1 裁决 2 点名要求它登记成「有意不推」。
AST_INVISIBLE_SITES: tuple[InvisibleSite, ...] = (
    InvisibleSite(
        "platform/management/commands/seed_e2e_write_orders.py", "Command.handle",
        KIND_CREATE, "Order",
        label=SKIP, model="Order",
        reason=(
            "建单（E2E 写租户的订单夹具）：`defaults=fields` 里的 `status` 是**出生状态**，"
            "不是状态变化 ⇒ 有意不推。⚠️ 本网看不见它（状态在一个变量里），"
            "所以它在这一张**手工**表里；它一旦改成 `create(status=…)` 那种能扫到的形状，"
            "判据 4 会红，要求把它移进正式登记表。"
        ),
    ),
)


def _scopes(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """文件里全部函数 / 方法的作用域名 + 节点（照 `test_order_read_path_net` 的口径：
    `Class.method` / 顶层函数 `func` —— ⛔ 不用裸方法名，那会把两个类的同名方法并成一个）。"""
    found: list[tuple[str, ast.AST]] = []

    class Walker(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def _visit_fn(self, node):
            found.append((".".join(self.stack + [node.name]), node))
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _visit_fn
        visit_AsyncFunctionDef = _visit_fn

    Walker().visit(tree)
    return found


def _root_name(node: ast.AST) -> str:
    """一条表达式链最左端的名字（`Order.objects.filter(…).update` ⇒ `Order`）。"""
    while True:
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        else:
            break
    return node.id if isinstance(node, ast.Name) else ""


def _queryset_model(site: Site) -> str:
    if site.owner == site.model:
        return site.owner
    tree = ast.parse((APPS_DIR / site.path).read_text(encoding="utf-8"))
    scopes = dict(_scopes(tree))
    scope = scopes[site.scope]
    if site.queryset_caller is not None:
        calls = [
            node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == site.scope
        ]
        caller = scopes[site.queryset_caller]
        if len(calls) != 1 or calls[0] not in ast.walk(caller) or not any(
            kw.arg == site.owner and isinstance(kw.value, ast.Name) and kw.value.id == site.owner
            for kw in calls[0].keywords
        ):
            return site.owner
        scope = caller
    bindings = [
        node.value for node in ast.walk(scope)
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == site.owner for target in node.targets
        )
    ]
    if len(bindings) == 1:
        return _root_name(bindings[0])
    factory = scopes.get(site.owner)
    if not bindings and factory and len(factory.body) == 1 and isinstance(factory.body[0], ast.Return):
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == site.owner
               for node in ast.walk(scope)):
            return _root_name(factory.body[0].value)
    return site.owner


def _dict_has_status(node: ast.AST) -> bool:
    """字典字面量里有没有 `"status"` 这个键（`defaults={"status": …}` 那一档）。"""
    return isinstance(node, ast.Dict) and any(
        isinstance(key, ast.Constant) and key.value == "status" for key in node.keys
    )


def _bulk_update_has_status(call: ast.Call) -> bool:
    fields = (
        call.args[1] if len(call.args) > 1
        else next((kw.value for kw in call.keywords if kw.arg == "fields"), None)
    )
    return isinstance(fields, (ast.List, ast.Tuple)) and any(
        isinstance(field, ast.Constant) and field.value == "status" for field in fields.elts
    )


def _counts(rows) -> collections.Counter:
    """登记表 ⇒ `Counter[(文件, 作用域, 形状, 对象)]`，**按 `site.count` 累加**。
    ⚠️ ⛔ 不是 `Counter(生成器)`：后者数的是「登记了几条」，同一条登记写 `count=4` 也只会算 1 ⇒ 两向断言在**重数**那一维上恒真（同族自指断言，`AGENTS.md` §5-G / E-0024）。"""
    counter: collections.Counter = collections.Counter()
    for row in rows:
        counter[row.key] += row.count
    return counter


def _scan() -> collections.Counter:
    """扫描 `src/apps/**/*.py`（**跳过 `migrations/`**）⇒ `Counter[(文件, 作用域, 形状, 对象)]`（文件名带相对 `src/apps/` 的路径：basename 会撞，本仓已有若干同名文件）。"""
    counts: collections.Counter = collections.Counter()
    for path in sorted(APPS_DIR.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        relative = str(path.relative_to(APPS_DIR))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for scope, node in _scopes(tree):
            for child in ast.walk(node):
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Attribute) and target.attr == "status":
                            counts[(relative, scope, KIND_ASSIGN, _root_name(target.value))] += 1
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr in WRITE_CALLS
                ):
                    method = child.func.attr
                    owner = _root_name(child.func)
                    writes_status = any(kw.arg == "status" for kw in child.keywords) or any(
                        kw.arg == "defaults" and _dict_has_status(kw.value)
                        for kw in child.keywords
                    )
                    if method == "bulk_create" and owner == "JidanpuSyncEvent":
                        # 批量插入同步事件总会写初始状态，含模型默认值。
                        writes_status = True
                    elif method == "bulk_update":
                        writes_status = _bulk_update_has_status(child)
                    if not writes_status:
                        continue
                    # 该字典只作为通知行 get_or_create 的 defaults；不是模型查询。
                    if (relative, scope, owner) == (
                        "wecom/notifications.py", "_recover_order_journal", "defaults"
                    ):
                        continue
                    kind = KIND_UPDATE if method in ("update", "bulk_update") else KIND_CREATE
                    counts[(relative, scope, kind, owner)] += 1
    return counts


def _scanned_keys() -> set[tuple[str, str, str, str]]:
    return set(_scan())


#: 「出网入队 / 通知」调用的**方法名前缀**（`_push_calls` 用）——**显式枚举**表：
#: 本仓的出网入口今天都长这三个前缀（`peer_dispatch.py` 的 `enqueue_*` 全族 + `notify_order_price_changed`、`jidanpu_member.queue_*` 那一族）。
#: 出现**新的**前缀 ⇒ 本表要同一次补（否则「有意不推」那条判据会漏掉那个新入口）。
PUSH_CALL_PREFIXES = ("enqueue", "notify", "queue")


def _called_name(call: ast.Call) -> str:
    """一次调用的**被调名**：`enqueue_order_grabbed(...)` ⇒ `enqueue_order_grabbed`（`ast.Name`）；`jidanpu_member.queue_hall_launch(...)` ⇒ `queue_hall_launch`（`ast.Attribute`）。
    ⚠️ **两种形状都要认**（判据能失败的前提）：只认 `ast.Attribute` 会漏掉**裸函数调用**——本仓最核心的两个出网入口（`enqueue_order_grabbed` / `notify_order_price_changed`）正是裸函数调用 ⇒ 那样写出来的判据**恒真**（在真正要防的场景上扫不到任何东西）。"""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _push_calls(relative_path: str, scope: str) -> list[str]:
    """某个作用域里出现的出网入队 / 通知调用名（**去重、排序**）。
    ⚠️ 本文件少数几处**读源码**的判据之一（另一处是 `test_the_ast_invisible_entries_…` 断言 `defaults=fields` 仍在）：判据问的是「某个成因在不在」，AST 扫描（`_scan`）答的是「写状态的落点在哪」，两者不重叠。"""
    tree = ast.parse((APPS_DIR / relative_path).read_text(encoding="utf-8"))
    node = dict(_scopes(tree)).get(scope)
    if node is None:
        return [f"<作用域 {scope} 不存在>"]
    return sorted(
        {
            name
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and (name := _called_name(child)).startswith(PUSH_CALL_PREFIXES)
        }
    )


class EventSiteRegisterTests(SimpleTestCase):
    """判据 1–4（逐条见模块 docstring）。"""

    def test_notice_defaults_exception_does_not_hide_order_writes(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        tree = ast.parse((APPS_DIR / 'wecom/notifications.py').read_text(encoding='utf-8'))
        node = dict(_scopes(tree))['_recover_order_journal']
        self.assertTrue(any(isinstance(child, ast.Assign) and isinstance(child.value, ast.Dict)
                            and any(isinstance(target, ast.Name) and target.id == 'defaults' for target in child.targets)
                            for child in ast.walk(node)))
        self.assertTrue(any(isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                            and child.func.attr == 'get_or_create' and _root_name(child.func) == 'WecomNotification'
                            and any(kw.arg == 'defaults' and isinstance(kw.value, ast.Name) and kw.value.id == 'defaults'
                                    for kw in child.keywords) for child in ast.walk(node)))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'wecom').mkdir()
            (root / 'wecom/notifications.py').write_text(
                'def _recover_order_journal(order):\n'
                '    defaults = {}\n'
                '    defaults.update(status="failed")\n'
                '    order.status = "ISSUE"\n', encoding='utf-8')
            with patch(__name__ + '.APPS_DIR', root):
                found = _scan()
        order_site = ('wecom/notifications.py', '_recover_order_journal', KIND_ASSIGN, 'order')
        self.assertEqual(found[order_site], 1)
        self.assertEqual((found - _counts(EVENT_SITES))[order_site], 1)
        self.assertNotIn(('wecom/notifications.py', '_recover_order_journal', KIND_UPDATE, 'defaults'), found)

    def test_every_scanned_site_is_registered_and_every_entry_still_exists(self):
        """⭐ 两向**恰好**：AST 扫到的 == 登记表。两个方向各自的失败信息分开写（排障要看得出是哪一向）。"""
        scanned = _scan()
        registered = _counts(EVENT_SITES)
        missing = {key: n for key, n in scanned.items() if registered.get(key) != n}
        self.assertEqual(
            missing, {},
            "有写入点没登记（或登记的重数不对）："
            f"{sorted(missing)} —— ⛔ 新增/搬动 `Order.status` / `OrderScreenshot.status` 的写点时，"
            "**同一次**把它登记进 `EVENT_SITES`（要么接推送、要么写明为什么有意不推）。",
        )
        stale = {key: n for key, n in registered.items() if scanned.get(key) != n}
        self.assertEqual(
            stale, {},
            "登记表里有**已经不存在**（或重数变了）的落点："
            f"{sorted(stale)} —— 表烂了就会给出假保证，⛔ 别留着。",
        )

    def test_every_entry_carries_a_frozen_label_and_a_consistent_model(self):
        """判据 2 + 3：标签只能是三档之一；模型归属与标签自洽；**调用形状**还要与 queryset 根名相符。"""
        for site in EVENT_SITES:
            with self.subTest(site=site.key):
                self.assertIn(site.label, LABELS)
                if site.label == OTHER:
                    self.assertIn(
                        site.model, OTHER_MODELS,
                        f"{site.key}：`{OTHER}` 那档必须点名一个**冻结枚举**里的别的模型"
                        "（新模型要同一次加进 `OTHER_MODELS`，⛔ 不许匿名放过）",
                    )
                else:
                    self.assertIn(
                        site.model, TARGET_MODELS,
                        f"{site.key}：`{site.label}` 只许出现在 {TARGET_MODELS} 上",
                    )
                    self.assertTrue(
                        site.reason,
                        f"{site.key}：每条登记都必须写清理由（`推送` 也要写清接在哪）",
                    )
                if site.kind in (KIND_CREATE, KIND_UPDATE):
                    self.assertEqual(
                        _queryset_model(site), site.model,
                        f"{site.key}：写入调用的 queryset 必须来自登记的模型",
                    )
                else:
                    self.assertTrue(site.owner, f"{site.key}：认不出被赋值的对象名")

    def test_the_ast_invisible_entries_are_still_invisible_and_still_real(self):
        """判据 4：手工登记的那些**今天确实扫不到**，而且它们真的落在一个存在的（文件, 作用域）里。两个方向都要：
        某条**已能被扫到** ⇒ 红（要求把它移进正式登记表，⛔ 不许留在手工表里绕过两向断言）；那条（文件, 作用域）**不存在了** ⇒ 红（表烂了）。"""
        scanned = _scanned_keys()
        for entry in AST_INVISIBLE_SITES:
            with self.subTest(site=entry.key):
                self.assertNotIn(
                    entry.key, scanned,
                    f"{entry.key}：它现在**能**被 AST 扫到了 ⇒ 必须从 `AST_INVISIBLE_SITES` "
                    "移进 `EVENT_SITES`（手工表只是给「看不见的形状」兜底的）",
                )
                self.assertIn(entry.label, LABELS)
                if entry.label == OTHER:
                    self.assertIn(entry.model, OTHER_MODELS)
                else:
                    self.assertIn(entry.model, TARGET_MODELS)
                self.assertTrue(entry.reason, f"{entry.key}：必须写清理由")
        files = {entry.path for entry in AST_INVISIBLE_SITES}
        self.assertEqual(
            files,
            {"platform/management/commands/seed_e2e_write_orders.py"},
            "手工表是按**具体落点**维护的；换了文件要同一次改这条自证断言",
        )
        source = (APPS_DIR / files.pop()).read_text(encoding="utf-8")
        # 那条"看不见"的成因必须还在（状态经由一个变量交给 `defaults=`）：
        # 少了它说明现场变了，手工登记的理由也就不成立了。
        self.assertIn("defaults=fields", source)

    def test_the_split_create_site_still_does_not_push(self):
        """两类子单出生时无需旧开放平台推送，按实际拆单实现检验。"""
        path = "business/api/order_views.py"
        self.assertEqual(_push_calls(path, "OrderViewSet.split_local_order"), [])
        scope = "OrderViewSet.split_priced_order"
        self.assertEqual(
            _push_calls(path, scope), ["queue_externalpay_recheck"],
            "并列拆单只有本地企微账单重核",
        )
        tree = ast.parse((APPS_DIR / path).read_text(encoding="utf-8"))
        priced_split = dict(_scopes(tree))[scope]
        rechecks = {
            node for node in ast.walk(priced_split)
            if isinstance(node, ast.Call) and _called_name(node) == "queue_externalpay_recheck"
        }
        self.assertEqual(len(rechecks), 3, "来源单、已有成员和主单各保留一个重核入口")
        gated = set()
        for node in ast.walk(priced_split):
            if not isinstance(node, ast.If):
                continue
            condition = node.test
            conditions = (
                condition.values
                if isinstance(condition, ast.BoolOp) and isinstance(condition.op, ast.And)
                else [condition]
            )
            if any(ast.unparse(part) == "price_context.recheck_external_refunds" for part in conditions):
                for statement in node.body:
                    gated.update(rechecks.intersection(ast.walk(statement)))
        self.assertEqual(gated, rechecks, "每个本地账单重核入口都必须允许调用方禁用")
        # 阳性对照（**两种调用形状各一条**，否则上面那条在「扫描器只认一半形状」时是恒真的假绿）：
        # ① 裸函数调用形状；② 属性调用形状 —— 两处都是本表里已登记的**推送**位点。
        self.assertIn(
            "enqueue_order_grabbed",
            _push_calls("business/api/order_views.py", "OrderViewSet.claim"),
            "扫描器漏了**裸函数调用**形状（`enqueue_order_grabbed(...)`）",
        )
        self.assertIn(
            "queue_hall_launch",
            _push_calls("business/api/order_views.py", "OrderViewSet.throw_to_hall"),
            "扫描器漏了**属性调用**形状（`jidanpu_member.queue_hall_launch(...)`）",
        )

    def test_the_scan_itself_is_not_vacuous(self):
        """自证夹具：扫描器**真的扫到了东西**、两个目标模型**都在**集合里——防「扫描器坏了 ⇒ 空集合 ⇒ 两向断言恒真」（上面三条断言在空集合上全绿）。"""
        scanned = _scan()
        self.assertGreater(sum(scanned.values()), 0, "扫描器一个落点都没扫到（自证失败）")
        owners = {key[3] for key in scanned}
        for model in TARGET_MODELS:
            self.assertIn(model, owners, f"扫描结果里没有 {model} 的落点（自证失败）")
        # 出生即带状态的 create 必须各在不同作用域。数字变了先看扫描真值，不要心算加减。
        # 扫不到的落点（关键字展开）不要留一条永不命中的登记。
        create_entries = [
            site for site in EVENT_SITES
            if site.kind == KIND_CREATE and site.label == SKIP
        ]
        self.assertEqual(
            len(create_entries) + len(AST_INVISIBLE_SITES), 7,
            "§7.2.1 裁决 2 今天点名的是**恰好 7 处**「出生即带状态」的 create"
            "（6 处能被扫到 + 1 处手工登记；包含旧拆单与并列拆单两条）"
            "——多一处 / 少一处都要同一次改这条与登记表",
        )
