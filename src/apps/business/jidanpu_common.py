"""三条鸡蛋铺线共用的公共件。租户级 `jidanpu_hall` 已删除。本模块是那批搬过来的符号的定义处；
`jidanpu_upstream` 直接 import 其中几个，那是消费，不是再导出。
别处还有同名但另一份的函数（例如 upstream 自己的 `_money_text`），不要据此改这里。"""

from __future__ import annotations

import contextvars
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.business import jidanpu
from apps.business.jidanpu_order_template import ADD_ORDER_TEMPLATE, ADD_ORDER_TOP_LEVEL
from apps.business.models import JidanpuSyncEvent, OrderStatus

#: 只 import `JidanpuSyncEvent` / `OrderStatus`。`RemoteHallOrder` 已删除。


# ── 远端来源取值 JIDANPU（原文 jidanpu_hall.py 第 109-110 行） ────────────
#: ⚠️ **本块与原文的唯一差别**：取值写法由 `RemoteHallOrder.SourceSystem.JIDANPU` 改成 **自带字面值** `"jidanpu"`——那个枚举的字面值就是它（`apps/business/models.py` 的 `RemoteHallOrder.SourceSystem.JIDANPU = "jidanpu", "鸡蛋铺"`），**值一字未改**；
#: 换成字面值是因为 `RemoteHallOrder` 会被后续步骤删掉，而本模块⛔ 不 import 它（计划 §2.3 第 7 行）。 ⛔ **已落地**：`RemoteHallOrder` 连同它的 `SourceSystem` 枚举已删除（迁移 `0057_remove_jidanpuorderlink_order_and_more`）⇒ 上面那句「会被删掉」讲的是**当时**；本常量的字面值与语义**一字未改**，今天它就是 `"jidanpu"` 的唯一出处。下面那行注释逐字搬自原文。 P2 只实现这一种远端来源（D1 的通用抽象里先落一个取值）。
JIDANPU = "jidanpu"


# ── 台账 reason 文案（原文 jidanpu_hall.py 第 134-138 行那一段里的 4 个） ────────────
NO_COUNT_REASON = "上游没有给出大厅总行数，本轮无法判定是否拉全"
MISSING_ORDER_ID_REASON = "远端大厅行缺少 orderId，已跳过（无法作为镜像单的幂等键）"
#: ⭐ D-0300 后全仓唯一写点只剩 `promoted` 一支（gone 行改为自动复活、不再写它），「已消失」半截与「只记台账、不改状态」的普适口吻已过时。
#: ⛔ **字面值不能动**：它是 `note_once` 幂等键的成分（改了会多出一行新台账），也是 `rescue_gone_mirrors` 命令存量证据通道①的**精确查询键**（改了会把存量证据行筛丢）。
TERMINAL_REAPPEAR_REASON = "该单此前已是终态（已转正 / 已消失），又出现在远端大厅：只记台账、不改状态"
GONE_REASON = "该单已不在鸡蛋铺大厅（被接走或已被撤回）"


# ── 上家镜像 `gone` 行复活的台账 kind / reason（D-0300）────────────
#: **唯一出处**：`sync_hall` 行循环的自动复活与 `rescue_gone_mirrors` 管理命令（存量批量兜底 / 审计工具）共用这两个常量——⛔ 两处各写一份字符串（E-0011 家族：搜「主词」必须
#: 只有一个出处）。`reason` 是**中性**文案（⛔ 不写「人工」：循环里那次是自动复活），两个写点共用同一句。
KIND_RESCUED = "upstream.hall_rescued"
RESCUE_REASON = "该单曾被判 gone 后又在上游大厅重现，已复活回可拉状态"


# ── 台账唯一写口（原文 jidanpu_hall.py 第 148-157 行） ────────────
def write_event(
    *, direction: str, kind: str, status: str, target_type: str, target_ref: str,
    reason: str = "", payload: dict | None = None,
) -> JidanpuSyncEvent:
    """台账唯一写口（spec §3.3）：`payload` 里**不许放凭据**（S1）。"""
    return JidanpuSyncEvent.objects.create(
        direction=direction, kind=kind, status=status,
        target_type=target_type, target_ref=target_ref,
        reason=reason, payload=payload,
    )


# ── 镜像行默认键表（原文 jidanpu_hall.py 第 272-312 行） ────────────
#: 镜像行的**默认键表**：键与值都按「与原生行同一套键」写死。 ⚠️ 键与 `HallListSerializer.Meta.fields` 的**恰好相等**由 `tests/test_jidanpu_hall_merge.py::test_the_two_sources_have_exactly_the_same_keys` 钉住
#: （⛔ 别在这里 import 序列化器：领域层不依赖 API 层，且「两侧同源」会让那条断言恒真）。 ⚠️ 取值口径：镜像行**照远端给的样子**（业务方 D5 = 选 A）——远端投影掉的一律 `""` / `None`， ⛔ **不编数据**（例如 `payment_status` 留空串而不是 `UNSETTLED`：远端没给结算状态）。
REMOTE_ROW_BASE: dict[str, object] = {
    "order_no": "",
    "source_order_no": "",
    "source": "",
    #: 镜像单在大厅里的显示状态 = `WAITING_GRAB`（原生大厅行也是它）。
    #: ⚠️ 这是**本计划拟定**的显示口径：远端投影把 `orderSta` 写成「待接单」，
    #: 而我方前端那一列读的是我们自己的状态词枚举。
    "status": OrderStatus.WAITING_GRAB,
    "game": None,
    "server": None,
    "project_text": "",
    "total_price": "0.00",
    "customer_phone": "",
    "is_urgent": False,
    "created_at": None,
    "refund_fee": "0.00",
    "taker_name": "",
    "game_role_name": "",
    "customer_ww": "",
    "hall_price": None,
    "sales_agent": "",
    "created_by": "",
    "remark": "",
    "completed_at": None,
    "time_requirement": "",
    "payment_status": "",
    "payment_method": "",
    "net_received": "0.00",
    "hall_publisher_name": "",
    #: ⭐ `taker_settled`（打手绩效是否已结账；钱包发薪线起的载荷新键）
    #: ⇒ 与 `HallListSerializer.Meta.fields` 恰好同键的镜像行也必须带上它。
    #: 值恒 `False`：镜像单**不是我方的订单**、没有 MAIN 绩效行（与「缺 MAIN 行 ⇒ 未结账」同口径）。
    "taker_settled": False,
    #: ⭐ `platform_push`（这单已抛到平台的摘要 / 没有 ⇒ `null`）⇒ 与 `HallListSerializer.Meta.fields` 恰好同键的镜像行也必须带上它。
    #: 值恒 `None`：镜像单**不是我方的订单**，本仓没有它的代练通推送行（与 `taker_settled` 的「键在、值取没有的那一档」同一条处置）。
    "platform_push": None,
    #: ⭐ 镜像行主键：**P2 镜像行没有「本线镜像表」的主键** ⇒ 恒 `None`；本线那一路在 `jidanpu_upstream.serialize_upstream_row` 里覆盖成 `row.pk`。
    #: ⚠️ 它必须在这里（= 与 `HallListSerializer.Meta.fields` 恰好同键，否则 P2 那条形态网红）。
    #: ⛔ 不要因为「P2 用不到」就删掉这一行。
    "mirror_pk": None,
    #: ⭐⭐ D-0296 的**四个加法键**（键表仍与 `HallListSerializer.Meta.fields` 恰好同形；取值口径见序列化器同名键的注释）：
    #: - `sim_pk`：模拟单行主键，照 `mirror_pk` 同一条先例——非本路恒 `None`，模拟单行由 `hall_simulated` 的载荷构建处填自己的主键；
    #: - `is_preferred` / `is_exited` / `exit_kind`：镜像行可接行的基线值（ `is_preferred` 在 `serialize_upstream_row` 里按**勾兑展示价**覆写；可接行恒「在厅、无离厅方式」）。
    "sim_pk": None,
    "is_preferred": False,
    "is_exited": False,
    "exit_kind": None,
}


# ── 金额与录单表单（原文 jidanpu_hall.py 第 315-319 行 / 第 701-703 行 / 第 817-864 行） ────────────
def _money_text(value) -> str | None:
    """金额统一成**两位小数字符串**（与 `OrderListSerializer` 的金额键同形）。"""
    if value is None:
        return None
    return f"{Decimal(value):.2f}"


#: **无抛单价**（`Order.hall_price is None`）时的人话原因（spec §5.4：金额可以为 0/负，
#: 但**没填** ≠ 0）⇒ 记一条 `skipped` 台账，⛔ 不推上游（NULL 当 0 会把免费单送进真大厅）。
LAUNCH_NO_PRICE_REASON = "该单没有抛单价，未推到鸡蛋铺"


# ── 出向「项目名称 / 备注」那一格的宽度上限（⭐ D1 自②式 outbox 抽来共用）───
#: ⭐⭐ **发出去的「项目名称」那一格的宽度上限**（`project`，①式表单与②式 `CreateOrder` / `ModifyOrder` **共用这一个常量**）—— **唯一一处**，⛔ 别在别处再写一遍这个数字。 ## 为什么必须有它（红队 I1） `Order.project_text` 的列宽已放宽到 **900**（**D-0218**）。而**对方那一格**的宽度概念是**独立的**：我方自己的**入向**在 `jidanpu_upstream.py::_clip_for_order(row.get("project"), "project_text")` 里就把它裁到我方列宽 —— 说明**对方那一格有宽度概念**（那条线段是照对方真实的字段长度收的）。 ⇒ 不在这里钉一个**与模型列宽解耦**的上限，列宽一放宽，900 字的项目名就会在 **出向**被对方拒 ⇒ `failed` → 退避 → `abandoned`、**静默烂在台账里**（`ModifyOrder` 那一格被拒时我们拿不到任何"只是太长"的信号）。 ## ⚠️ 如实登记（**证据级别**，随 E-0242 落定） - ⭐ **真机实测（E-0242）**：对方 `project` 那一格有**字节级**上限 ≈**732** —— 当天成功 438 张全部 ≤732 字节、失败各张全部 ≥734 字节，**零重叠**（现场与取证见
#: `docs/ERRORS.md` 的 E-0242）。此前这里按**字符 300** 裁、登记着「对方真实宽度没实测过」 —— E-0242 那批卡单正是这个口径第一次被真机量出来。 - ⚠️⚠️ **字节 ≠ 字符**：按字符 300 裁，中文最多可到 900 字节 ⇒ 照样被对方拒。现在**按字节**裁（UTF-8 边界安全，见 `_clip_project`）。 - 取 **700**：对方实测 732 之下留 32 字节安全余量，与**前端 700 字节闸**（E-0242 同车修复）同一口径——前端放行的，后端不再多裁一刀。 - ⛔ 别因为「中文比 300 字符变短了」调回字符口径：这条上限管的是**对端那一格的字节数**。 ⚠️ **裁剪之后必须仍然成功**（⛔ 不是判失败、⛔ 不是跳过）：②式的判据是"请求体里那一格被裁到本常量"＋"这一轮仍然 `done`"（见 `tests/test_jidanpu_member_openapi_outbox.py` 的 `ProjectTests`）；①式的判据见 `tests/test_jidanpu_member_form_bounds.py`。 ⛔ **不许**把它写成 `Order._meta.get_field("project_text").max_length`：模型列宽放宽的 **前提**就是出向这一格自己钉住（两格宽度**不是**同一件事）。 ## ⭐ 为什么落在本模块（D1，M-9）原先它（连同 `_clip_project`）只住在②式的 outbox 模块里 ⇒ **①式录单表单** （`build_add_order_form`）的 `project` / `remark` 两格**不裁**——E-0242 只修了②式半边， 250 个中文（750 字节）的入向长单在①式 `launch` / `assign` 会被对方拒、烧完退避进 `abandoned`（E-0242 当天 6 张卡单同形）。抽到本模块（三条线共用的公共件宿主）之后 **两条线共用同一个数字与同一把剪刀**，⛔ 不许任何一条线再抄第二份。
PROJECT_TEXT_OUTBOUND_MAX_BYTES = 700


def _clip_project(text: str) -> str:
    """出向「项目名称 / 备注」那一格的值：**按 `PROJECT_TEXT_OUTBOUND_MAX_BYTES` 个 UTF-8 字节裁**（**唯一**裁剪点；E-0242 起按字节、不再按字符——理由与证据级别见那个常量的注释）。 ⚠️ **在出向边界裁**：上游那些端点**共用同一格**（`project`；备注 `remark` / `orderRemark`
    同理）⇒ 裁剪只有这一处实现，调用方各自在自己的出向边界调它，⛔ 不在各调用点各写一次切片（两处必然漂，而漂的后果是"录单那条裁了、改单那条没裁"这种半新半旧）。改单备注口也要过这把剪刀。漏一处就是一边裁了、一边没裁。 ⚠️ **UTF-8 边界安全**：先按字节切、再 `errors="ignore"` 解码——多字节字符被拦腰
    切断的残字节直接丢弃（这一格本来就是「超长被裁」的语义，残字节没有保留价值）。 ⛔ 不 strip、⛔ 不归一化：这一格是对方的匹配键之一，改它的首尾空白等于改语义（与 `_game_area` 只 `strip()` 判空、不改值的口径一致）。 ⚠️ `None` 不该到这里（调用点都已 `or ""`）—— 真到这儿也让 `AttributeError` （`None.encode`）**响亮**地炸，⛔ 不用 `str()` 把它糊过去（那会把 `None` 变成 `"None"` 发到对方那一格）。"""
    return text.encode("utf-8")[:PROJECT_TEXT_OUTBOUND_MAX_BYTES].decode(
        "utf-8", errors="ignore"
    )


def build_add_order_form(order) -> dict:
    """录单表单 JSON（`strParameter` 的内容），基底是**生成物模板**。 ⚠️ **两份 `gameName` 不同义**（旧 spec §2.8-2 实测）：**表单里是游戏 ID**。 ⛔ 区服不同步（旧 spec A4）、⛔ 密码不同步 ⇒ 这两处**不动模板里的空值**。 ⚠️ **键名以生成物模板为准**（spec §5.2 的映射表把内容字段标成「推断（以模板对照确认）」）：
    本机捕获实测的表单输入键是 `dlcontent`（内容）/ `gameRole`（角色）/ `ww`（旺旺）； `orderContent` / `roleName` / `WangWang` 是**订单行**的输出字段名（spec §4.3 大厅投影那一列）， ⛔ 不是这张表单的键。`emps` 同因：它是**顶层**参数（`ADD_ORDER_TOP_LEVEL` 里那份），
    表单里**没有**它 ⇒ 本函数不往表单塞这个键。 ⭐ **`project` / `remark` 两格在出向边界裁字节**（D1，M-9 的①式半边；与②式同一把剪刀 `_clip_project` / 同一个常量）：`Order.project_text` 列宽 900（D-0218）下 250 个中文就是 750 字节，**超过对方 ≈732 字节的实测上限** ⇒ 不裁的长单会被对方拒、烧完退避进 `abandoned`（E-0242 当天 6 张卡单同形，见那个常量注释的「为什么落在本模块」）。 ⛔ **裁剪之后照常发**（不是判失败、不是跳过）：与②式同一条口径。"""
    form = dict(ADD_ORDER_TEMPLATE)
    form.update(
        {
            "souresOrderID": order.order_no,
            "gameName": str(order.game_id or ""),
            "project": _clip_project(order.project_text or ""),
            "price": _money_text(order.total_price) or "0",
            "dlcontent": order.content or "",
            "moblie": order.customer_phone or "",
            "qq": order.customer_qq or "",
            "ww": order.customer_ww or "",
            "gameAcc": order.game_account or "",
            "gameRole": order.game_role_name or "",
            "remark": _clip_project(order.remark or ""),
            # 不指派：落在鸡蛋铺的「未分配」池子里（旧 spec §2.8-5 读码）。
            "recUser": "0",
        }
    )
    return form


def build_add_order_top_level(order, *, source_value: str) -> dict:
    """录单的顶层参数：基底是生成物模板，**游戏名**与来源值在这里。 ⚠️ 顶层 `gameName` 是**游戏名**（与表单里那个游戏 ID 同名不同义）。
    ⚠️ 来源值同时写进**表单**的 `orderSoures`（本机捕获实测的表单输入键）——见 `consume_launch_event`；`orderSoure` 是**订单行**的输出字段名（spec §4.3），这里照计划保留。"""
    top = dict(ADD_ORDER_TOP_LEVEL)
    top.update(
        {
            "gameName": order.game.name if order.game_id else "",
            "orderSoure": source_value,
        }
    )
    return top


# ── 接单竞态文案与审计动作码（原文 jidanpu_hall.py 第 1103-1110 行 / 第 1147 行） ────────────
#: 文案（spec §6.4，**逐字**）。
ALREADY_TAKEN_MESSAGE = "该笔订单已被其他打手接取"
IN_PROGRESS_MESSAGE = "该笔订单正在处理中"
TEMPORARY_FAILURE_MESSAGE = "暂时无法接单，请稍后再试"
#: 「远端占住已成功、但我方落库失败（如 `CommissionError`）」那一档（业务方裁定：
#: 复用 `ErrorCode.BUSINESS`(`1002`)、**不新开码**；⛔ 不许翻成「操作失败 / 已被别人接走」那类说假话的码）。
RESERVED_PROMOTE_PENDING_MESSAGE = "正在抢单，请稍后刷新查看"
NOT_CONFIGURED_MESSAGE = "还没配置好鸡蛋铺同步账号，请联系管理员"
ASSIGN_AUDIT_ACTION = "order.jidanpu_remote_claim"


# ── 身份自愈台账 kind（原文 jidanpu_hall.py 第 1522-1526 行） ────────────
#: 「绑定的同步账号身份已按登录账号自愈」那条**幂等**痕迹的 `kind` / `reason`。 ⚠️ `reason` 必须是**稳定**的一句话（幂等键 = `(kind, target_ref, reason)` 三元组，
#: 见 `_note_once`）——新旧 uid / 姓名进 `payload`，⛔ 不进 `reason`（否则每换一次身份就多一条「同一个事实」的行）。
IDENTITY_HEAL_KIND = "binding.identity_healed"


# ── 退单原因（原文 jidanpu_hall.py 第 2008-2009 行） ────────────
#: 业务方指定的退单原因（**逐字**，≤50 字；`jidanpu.set_order_status` 会自己卡上限）。
WITHDRAW_REMARK = "订单已被发单方打手接取"


# ── B-1 挡住集合（原文 jidanpu_hall.py 第 2012-2132 行） ────────────
#: B-1 **无条件**不回退的那个态：`REFUNDED`（**与「是否已结账」无关**）。 ⚠️ **这是一处偏离业务方字面口径的裁定**（编排者裁定；已另行告知业务方）：业务方原话「鸡蛋铺打手接了就同步我们订单到代练中」字面上也包含 `REFUNDED`，但**我方已把单退给客户（钱已经动了）之后远端又有人接**，不能靠一次**裸 `update()`** 把它复活——本仓既有口径也是「已落账的钱不自动翻」。实测出来的链条： - B-1 的状态写是**裸 `Order.objects.filter(...).update()`** ⇒ **不走 `apply_transition`**、目标态钩子一个都不跑； - 而「上一轮退款的结清」**只**挂在回池钩子的 `from_status == REFUNDED` 守卫上（`performance._on_pool_entry` → `refunds.reverse_previous_round_refund`）； - `refund_fee` 被 `on_order_refunded` 置成**毛收款**是**单向**的，上面那个冲销是它**唯一** 的复位点； ⚠️ **D-0181**：那句「被 `on_order_refunded` 置成毛收款」**已不成立**（退单不再写 `refund_fee`）⇒ 那段推理的字面前提消失，但**结论的方向不变**：回池那一刻仍是「上一轮结清」的唯一落点（今天它服务的是**部分退款**留下的累计额与客服行的退款冲减链）。⛔ 本档的行为（`REFUNDED` 无条件挡住 B-1）**一个字未改**。 - ⇒ 把 `REFUNDED` 推成 `IN_PROGRESS` 之后，那一单再也走不到「回池**且来源态是 `REFUNDED`**」⇒ 上一轮退款**结不掉**：重做那一轮 `net_received = 0`、利润为负、客服提成变**负数**（`refunds` / `performance` 的 docstring 自述实测 `−12.00`）。 ⚠️ 而且 B-1 的扫描面**只看 `hall.launch done`、不看 `hall.withdraw`** ⇒「先撤下再退单」的
#: 单仍留在扫描面里 ⇒ 这条路径比直觉更常见。 ⛔ **不改成 `apply_transition`**（那会把 `IN_PROGRESS` 的节点集与目标态钩子引进来，风险远超本任务）；⛔ 也**不动** `reverse_previous_round_refund` 的触发点（属另一条线）。 ⛔ **现行口径**（业务方裁定；详见 `docs/plans/2026-09-29-sync-account-taker.md`，决策由 `docs/DECISIONS.md` 收口）：远端打手接了我方抛出的单 ⇒ 我方接单人 = 同步账号对应的本地人员，B-1 **不删任何行**、改为**条件抢占**那条无主行 + 按他自己的提成配置写金额。⇒ 本档（`REFUNDED`）的行为：**接单人 / 金额都不写** （`locked` 为真 ⇒ 抢占与写金额两步都跳过）、状态一个字不改（台账与审计照记）；判据 = `test_b1_leaves_a_refunded_order_alone_and_its_refund_round_still_settles`。 ⚠️ 下面那段「删行 vs 补行」的实测**仍然成立**——留着是为了说明「为什么挡住状态比删行/补行更要紧」；无主 MAIN 行在两种处置下**有可观测差别**： - 对照 A（**不删**，即 `on_order_refunded` 留下的形状）：`temp_amount=0.00`、 `bonus_amount=0.00`、**扣点两列保留**（实测 `deduct_mode=比例 / 10.00`）； - 对照 B（**删了再补**：回池那一刻 `ensure_main_performance_of` 补出来的新行）： `temp_amount=100.00`（= `order.hall_price`）、`bonus_amount=0.00`、扣点两列**回到默认**（`""` / `0.00`）——它就是「录单即建绩效」的初始形态。 ⇒ 差别落在**三处**：① `预计绩效` 那一列（100.00 vs 0.00）；② 扣点两列被丢； ③ 由此**结算列表的 `payable_net` 那一列可能变**——实测：扣点是**固定额**时，对照 A 的 `payable_net` **算不出来**（它透传 `AmountError`：扣点 5.00 > 预计绩效 0.00），而对照 B 算出 `0.00`；扣点是**比例**时两边都是 `0.00`。 ⚠️ 这**不是账务变化**：该行在 `REFUNDED` + 无主两个条件下本来就**结不了账** （结算端 `rows` 腿要求订单已完成、归属人非空），退款那一轮的结清也**不受影响** （用例的阳性对照实证：回池照样把 `refund_fee` 归零 + 写一条 `REVERSAL`）。 ⛔ **不去改 `_on_pool_entry` / `create_performance_for_order` 的 `temp_amount` 口径** （属另一条线、风险大）——本处只如实登记形状。
B1_ALWAYS_BLOCKING_STATUS = OrderStatus.REFUNDED
#: B-1 **有条件**不回退的那个态：`COMPLETED`（**仅当**该单 MAIN 绩效行**已结账** **且** `settled_by != "system"`（系统自动发薪的行不挡，见 `_b1_status_rollback_blocked` 的「⭐ P0-6 收窄」））。「无条件挡住」的那个态是上面的 `REFUNDED` ——两个常量合起来才是完整的挡住集合。
#: 来源态**不设限**（业务方裁定：「鸡蛋铺打手接了就同步我们订单到代练中」） ——本常量取代早期的 `B1_RESUMABLE_STATUSES` 来源态白名单（那版**只**推进「池内两态」`UNASSIGNED` / `WAITING_GRAB`）。 ⚠️ **修复波 1 的原始目的仍被守住**：它当初挡的是「把**已被客服改成已完成**的单拉回代练中」。那条单的钱若**已经结出去**，拉回去就是让结算取数（`settlement.settle_performances` 的
#: `rows` 腿要求 `order__status=COMPLETED`）里那张单凭空消失，而已对过的账没有凭据可依。业务方同日还有一条并行裁定：「**只有已结账的已完成不回退**」⇒ 这道门**只**对「`COMPLETED` 且已结账」保留；**未结账**的 `COMPLETED`（`PENDING_REVIEW → COMPLETED` 不要求有接单人 ⇒ 无主的已完成单是可存在的）照推 `IN_PROGRESS`。 ⚠️ 这不是「新增一条状态机边」——B-1 不改状态机，只决定**这条回拉记账**要不要顺手推进状态。
B1_BLOCKING_STATUS = OrderStatus.COMPLETED


def _b1_status_rollback_blocked(order, main) -> bool:
    """B-1 要不要**跳过**订单状态推进（`True` = 状态列一个字都不改）。挡住集合（**并集**，两条分别判）： 1. **`REFUNDED`——无条件挡住**（理由见 `B1_ALWAYS_BLOCKING_STATUS` 的注释：那一步是裸 `update()`、跑不到「回池且来源态是 `REFUNDED`」的退款结清 ⇒ 钱会算错）； 2. **`COMPLETED` **且** 该单那条 MAIN 绩效行**已结账** **且** 不是系统自动发薪** （`settled_by != "system"`）——理由见下面「已结账」与「⭐ P0-6 收窄」两节。 ## ⭐ P0-6 收窄（`D-0287`）：系统自动发薪的行**不挡** 钱包作为发薪唯一出口之后，**每一张完成单**都会被 `accounts.payout.payout_completed_order` 自动标成「已结账」并写 `settled_by = "system"` ⇒ 收窄前的判据（只判 `is_settled`）让**每一笔上家退单都被挡住**：`follow_refund` 回 `SETTLED_KEEP_REASON`（「请人工」）、状态与钱一个字都不动 ⇒ 上家跟退**事实上停机**，钱留在打手钱包里（本条的现场）。判据因此与 `order_status.settled_status_change_denial` 的「自动入账放行」收成**同一个口径**： - **「有没有结账」**用 `Performance.is_settled`（三字段并集，见下面「判据 2 的已结账」一节）； - **「钱有没有进过钱包」**用 `settled_by == "system"`； - 两件事**分开判**，⛔ 不合成一个字段：⛔ 财务手工结账的行（`settled_by` 是用户名） **照旧挡住**——那些钱从来没进过钱包，跟着扣就是凭空多扣（规格 §3）。 - 系统行放行后由退单钩子收口：`refunds.on_order_refunded` 的第 1.6 步写一条
    `−settled_amount` 的回冲并**把结账四列复位**（钱与状态一起回头）。 ⚠️ **本判据是共享的那一把尺子**（公开别名 `status_rollback_blocked`）。 ⭐ **在役的活调用点只有两处**（独立评审核实）—— `jidanpu_upstream.follow_refund`（上家跟退）与 `jidanpu_member` 的 B-1 闸；后者在 `jidanpu_member.py` 里已被「COMPLETED 一律跳过」短路 ⇒ 收窄在它那里是**空操作**。 `jidanpu_converge.py` 已删除、`jidanpu_member_openapi.py` 里那处只是文档表格的一行 ⇒ **收窄的唯一实际效果就是跟退**。原文把消费方写成四个、并称「自动发薪的完成单可能被推回池内 / 代练中」，那是**错误外推**（§5-C），已删。 ⚠️ **脏行（`settled_by == "system"` 但 `settled_at` / `settled_amount` 不齐）**：收窄后它们不再被「挡住」，而是走进回冲那一句。生产上把「已结账 + `settled_by="system"`」写得出来的只有 `payout_completed_order`，它**四列同写** ⇒ 这种行只可能是数据缺陷。 - `settled_amount` 为空 / `<= 0` 那一档：回冲函数**安静返回**（没有可回冲的金额）； - `settled_amount > 0` 而 `settled_at` 为空那一档：在回冲幂等键的格式化处**响亮报错** （`TypeError`）⇒ 整笔退单回滚。**这是要的**（E-0022：数据缺陷不许伪装成「没什么要回冲的」）。用例：`tests/test_settle_p1_peer_follow_refund.py::DirtySettledRowTests`。 ## 判据 2 的「已结账」
    用模型属性 `Performance.is_settled`（`apps/business/models.py` 的属性；三字段**并集** = `settle_status == 已结账` **或** `settled_at is not None` **或** `settled_amount is not None`）。选它的理由： - 本仓**已有七处**点名「已结账」的口径都用它（逐处：硬删端点的 `performance_writes.delete_performances`；`/adjust/`、`/adjust-amount/`、 `/performances/<pk>/delete/`；`/assign/` 的 R-2a；`/hall-price/` 的 R-6； `order_status.settled_pool_entry_denial`）⇒ 本处跟着用，避免 E-0021 同族的形状（同一事实两套判据、只用对了一套）。 - ⛔ **不只看 `settle_status`**：存量里有「已结账但没写 `settled_at`」（P4-FE-0 之前的结账）或「状态列被改回未结账却留着快照」的脏行，只看状态列会把它们当「未结账」放行，而这一放行的后果落在**钱**上（`is_settled` 的 docstring 明写这三字段并集就是为它们加的）。 ⚠️ **但它不是「已结账」的「唯一实现」**： `order_stats._unsettled_amount` 与 `settlement._reject_message` 另有**更窄**的判法（单看 `settle_status`）⇒ 对上面那种「`settle_status = 未结账` 却留着 `settled_amount`」的脏行，它们与 `is_settled` 会得出**相反**的结论（一个算进未结账净额、一个当已结账跳过）。 ⛔ 别把本属性当成全仓已统一的判据——要动那两处是在改钱的口径（另一条线）。 ⚠️ 取的是 `Performance` 表里 `emp_type = MAIN` 的那条（与上面那七处**同形**）。结账本身要求 `user__isnull=False`（`settlement.settle_performances` 第 2 步）⇒ 「有主的已结账 COMPLETED」在调到本函数**之前**就已经落 B-2 分支（`discover_remote_taken` 开头 `main.user_id is not None` ⇒ 只记一条窗口台账、什么都不改） ⇒ 本判据**实际**额外挡住的，是「无主却带着结账痕迹」的脏行（`is_settled` 承认这类行存在）。 ⚠️ **「已结账 + 无主」这个组合：读码未见可达路径（⚠️ 未实测）**——上面那条 `user__isnull=False` 与 `settled_pool_entry_denial`（已结账不许经回池清 `user`）各挡住一半，但这两条都是**读码结论**，本任务**没有**实测穷尽所有改 `user` 的写点。 ⚠️ 缺 MAIN 行（P2 遗留单）⇒ 没有「已结账」可言 ⇒ 放行（与 `order_status.settled_pool_entry_denial` 的「缺 MAIN 行 ⇒ 放行」同口径）； ⚠️ 这一支对判据 1**不适用**：`REFUNDED` 是无条件挡，**不看**有没有 MAIN 行、也不看结账状态。"""
    if order.status == B1_ALWAYS_BLOCKING_STATUS:
        return True
    if order.status != B1_BLOCKING_STATUS:
        return False
    if main is None or not main.is_settled:
        return False
    # ⭐ 结算 P0-6（`D-0287`）：**系统自动发薪**的行**不挡**。判据与 `order_status.settled_status_change_denial` 的「自动入账放行」逐字同口径： `settled_by == "system"`。钱已经在打手钱包里 ⇒ 退单钩子（`refunds.on_order_refunded` 的第 1.6 步）会把它扣回来；
    # 挡住反而让「上家退单」事实上停机、钱留在打手余额里（这是本条的现场）。 ⛔ **财务手工结账的行仍挡住**：那些钱从来没进过钱包，跟着扣就是凭空多扣（规格 §3 的「存量已结算的单不回冲」）。「有没有结账」用 `is_settled`（三字段并集），「钱进没进钱包」用 `settled_by`——两件事分开判，⛔ 不合成一个字段。
    # ⚠️ 本判据是**共享的那一把尺子**（`status_rollback_blocked`），消费方不止跟退： `jidanpu_converge` 与 `jidanpu_member` 的 B-1 回退也一起吃这条收窄 —— 同一个事实⛔ 不复制第二份判据（E-0021）；那些路径只改状态、不回冲，但它们本来也能把「未结账的已完成单」推回池内，且重做完成时 `payout_completed_order` 的补差分支（`settled_by == "system"` ⇒ 按新净额**只补不退**）不会二次发钱。
    return main.settled_by != "system"


#: 公开别名（T4 收敛线加）：B-1 与「进方向收敛」**共用同一个挡住集合判据** （D-0156：【已定】`REFUNDED` 无条件挡 ∪「`COMPLETED` 且该单 MAIN 绩效行已结账」
#: ——⛔ 后者按 `D-0287` **收窄**成「已结账 **且** `settled_by != "system"`」，系统自动发薪的行放行，理由见函数 docstring 的「⭐ P0-6 收窄」一节）。
#: ⛔ 不复制第二份判据（E-0021 的形状：同一事实两套判据、只用对了一套）—— 消费方是 `apps/business/jidanpu_converge.py`，它按同一把尺子决定「要不要回退状态」。 ⚠️ 另一个消费方 `jidanpu_upstream.follow_refund` 用同一把尺子决定「上家退单要不要跟」。
status_rollback_blocked = _b1_status_rollback_blocked


# ── 执行租约（原文 jidanpu_hall.py 第 3182-3364 行） ────────────
#: **后台轮 / 请求路径**的执行租约时长（秒）。为什么必须存在：当场那次（`attempt_event_now`，跑在 web 进程的提交后回调里）、后台轮（`jidanpu_sync --loop`，**另一个进程**）与 **`/claim` 的请求路径**（`claim_remote → _claim_upstream`）会同时看到同一条 `pending` 事件 ——`consume_outbox` 的取活判据是 `status__in=("pending","failed")`，当场那次与请求路径从 **写入那一刻**起就把事件暴露在取活集合里，直到它自己落终态。没有占位时都会执行：实测（修复前）撤下 ⇒ 上游 `SetOrderSta` **2 次** + 撤下审计 **2 条**；抛单 ⇒ `AddOrderAjax` **2 次**（`remote_oid` 是 `add_order` 返回**之后**才落库 ⇒ 窗口里两个消费者都看不到它）； `hall.assign` ⇒ `assign_order` **2 次** + 第二个执行者撞 `JidanpuOrderLink` 唯一约束 ⇒ `IntegrityError` 从 `/claim` 外逃成 **500**（实测）。 **为什么是 180 秒**（修复波 3 重算；⛔ 不是拍的）：本波给执行者加了**心跳续租**——`jidanpu.open_client` 建的 httpx 客户端挂了
#: `jidanpu.tick_lease_heartbeat`（`event_hooks={"request": …}`）⇒ **每次真发 HTTP 请求之前** 续一次自己的租约（`renew_event_lease`，条件 `lease_owner == 自己`）。于是租约只需要覆盖**单个 HTTP 请求**的最坏时长，⛔ **不必**覆盖整条链、也不必覆盖一个业务跳： · 后台 / 请求路径（SDK 默认 `timeout=30.0` / `max_retries=2`）： `Timeout(30)` 铺满 connect / write / read / **pool** **四个阶段、各 30 秒**（读超时按每次读调用计）⇒ 单请求最坏 **90 秒** （`connect + write + read`），**含 `pool` 的最宽口径 = 120 秒**。 ⇒ 取 **180 秒** = 90 秒的 **2.0 倍** / 最宽 120 秒的 **1.5 倍**。 ⚠️ `pool` 只在连接池被占满时才真会等（本仓每个 client 串行使用 ⇒ 实际最坏 ≈90 秒）；口径按**最宽**写，⛔ 不改租约数值（修复波 4 订正；见 `docs/DECISIONS.md` 的订正块）。 · 单跳（一次 `client.call`）实测最多 **16 次 HTTP 请求**（真 `JidanpuClient` + `httpx.MockTransport`：3 次尝试 + 401 递归重登 + 302 恢复，两层 `_send()` 各 `1 登录 + 3 data + 1 登录 + 3 data`）⇒ 一跳最坏 16 × 90 = **1440 秒**（最宽 16 × 120 = 1920 秒）。 ⛔ 这个数**不再**是租约的下界（心跳把它降级成「参考值」）——这正是本波与修复波 2 的关键差别。 ⚠️ **代价的另一面**（为什么不是「越长越好」）：执行者被 `SIGKILL` / 掉电时租约**不会被清**，那条事件要等租约过期才有人敢接管 ⇒ 后台 / 请求路径这条取 180 秒意味着「执行者被打死」时那条事件最多停 **3 分钟**（修复波 2 是 1 小时；正常收尾路径一律清租约， ⛔ 不引入任何常态延迟）。
OUTBOX_LEASE_SECONDS = 180


#: 当前执行者的租约上下文：`(事件 pk, 所有权令牌)`；`None` = 这次调用**不属于**任何事件执行。 ⚠️ 用 contextvar（修复波 3）：`_finish_event` / 撤下成功落库都靠它认出「我是不是持有者」；
#: 嵌套执行（测试里把后台轮重入进当场那次）退出后自动还原外层，⛔ 不用模块级变量。外部已经没有读者。`execution_lease` 仍自己 set / reset，删它就要改那个函数。
_CURRENT_LEASE: contextvars.ContextVar[tuple[int, str] | None] = contextvars.ContextVar(
    "jidanpu_hall_current_lease", default=None
)


def current_event_lease_owner(event) -> str | None:
    lease = _CURRENT_LEASE.get()
    return lease[1] if lease is not None and lease[0] == event.pk else None


def new_lease_token() -> str:
    """造一个**所有权令牌**（修复波 3，A3/B6）。 ⚠️ 只要「同一条事件上两次执行取值不同」就够，所以 `pid + uuid4`：
    ⛔ 不用 pid 单独当令牌（同一进程里的两次执行会撞）、⛔ 不用用户标识（那不是执行者身份）。
    ⚠️ 它只落 `JidanpuSyncEvent.lease_owner` 一列，⛔ 不进任何对外文案、⛔ 不进审计。"""

    return f"{os.getpid()}-{uuid.uuid4().hex}"


def claim_event_for_execution(
    event, *, now: datetime, lease_seconds: int, token: str
) -> bool:
    """赢下这条事件的**执行权**（修复波 2，C7）——当场那次、后台轮与**请求路径**共用一个入口。 `True` = 租约抢到了，调用方**才有资格**执行；`False` = 别人正在跑 ⇒ 让开。机制（硬判据，⛔ 别改成别的形状）： - **条件更新 + `rowcount == 1`**：`filter(pk=…, status__in=("pending","failed"))` **再**叠加「租约为空 **或** 已过期」；⭐ ⛔ **不许「先读后写」**——先在内存里读到「没人持有」再无条件 `update` 会在两个进程之间留出竞态窗口，而那正是本函数要堵的东西。 - **只写租约、⛔ 不动 `status`**：消费者入口的「只消费 `pending` / `failed`」守卫（`consume_launch_event` / `consume_withdraw_event` / `consume_assign_event`）照旧成立，终态口径（`done` / `skipped` / `failed` + `attempts+1`）⛔ **一个字不改**。
    - ⭐ **同时写 `lease_owner = token`**（修复波 3）：租约从此**有身份** ⇒ 续租与收尾都能只认自己（见 `renew_event_lease` / `_owned_event_filter`）， ⛔ 没持租约的调用方再也不可能清掉别人的租约。 - **可回收**：执行者崩了（`SIGKILL` / 掉电）⇒ 租约到期后另一个执行者能接管（回收判据就是 `lease_expires_at <= now`）。⭐ 租约只需覆盖**单请求**最坏时长—— 执行者在真正有进展（每次真发上游请求）时会续租（`renew_event_lease`），推算见 `OUTBOX_LEASE_SECONDS` / `IMMEDIATE_LEASE_SECONDS`。 - ⭐ **到期点用墙钟，⛔ 不用调用方传进来的 `now`**（修复波 4，C10）： `lease_expires_at = timezone.now() + lease_seconds`。为什么：`now` 是**一轮开头**取的时刻
    （`run_round` 只取一次、`consume_outbox` 整轮复用），一轮跑得比租约长（积压 / 慢跳）时，用它会写出一个**已经过去**的到期点 ⇒ 并发的另一个执行者按**墙钟**立刻就能抢（实测 `expiry=14:34:16 wall=14:36:16 born_expired=True`）⇒「只有一个人能跑」在这段窗口里失效。⚠️ 回收判据 `lease_expires_at__lte=now` **仍用调用方的 `now`** （有意保守：它只会**少**抢、不会多抢；且「同一轮里的记账共用一个 `now`」这条口径不为它分叉）。 - **收尾带所有权清租约**：`_finish_event` 与撤下成功那次落库都走 `_owned_event_filter` （`lease_owner == 我` 或 `IS NULL`）⇒ 正常的「失败 ⇒ 退避 ⇒ 下一轮」节奏**一点没变** （租约绝不叠加在退避窗上），而「别人持租约时本次改不动它的状态 / 租约」也一并成立。 ⚠️ `_finish_event` **不是**终态的全局唯一写点：撤下成功那次直接落库、`jidanpu_converge.py` 的收敛落库、`jidanpu_member.py` 的 `_finish` / `_retry` 都不经它（本轮已知的几处见 `_finish_event` 的 docstring，⛔ 那份清单不声称完整）。 ⭐ 修复波 4 起，`_finish_event` 还要求事件**仍在可写状态**（`pending` / `failed`） ⇒ 迟到的旧执行者改不动**已经收好的终态**。"""

    # ⭐ 修复波 4（C10）：**到这里才取墙钟**——它只用于租约的**到期点**（见 docstring），
    #    ⛔ 不动 `now` 在别处的用途（业务时间戳 / 回收判据仍按调用方给的 `now`）。
    expires_at = timezone.now() + timedelta(seconds=lease_seconds)
    claimed = (
        JidanpuSyncEvent.objects.filter(
            pk=event.pk, status__in=("pending", "failed")
        )
        .filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now))
        .update(
            lease_expires_at=expires_at,
            lease_owner=token,
        )
    )
    return claimed == 1


def renew_event_lease(
    event_id: int, token: str, *, lease_seconds: int, now: datetime | None = None
) -> bool:
    """**心跳续租**：把租约往后延一个 `lease_seconds`（修复波 3，B6）。判据 = `filter(pk=event_id, lease_owner=token)` ⇒ ⭐ **只延长自己的**：
    租约已被别人接管（`lease_owner` 换人）时 `rowcount == 0`、一个字都不改。返回 `True` = 这次真的续上了。 ⚠️ 调用点是 `execution_lease` 挂给 `jidanpu.upstream_heartbeat` 的那个闭包
    （每次真发上游 HTTP 请求之前一次），⛔ **不是**后台线程 / 定时器—— 续租必须绑在**真实的执行进展**上，否则「进程卡死但心跳照跳」会让租约永远不过期。"""

    now = now or timezone.now()
    renewed = JidanpuSyncEvent.objects.filter(
        pk=event_id, lease_owner=token
    ).update(lease_expires_at=now + timedelta(seconds=lease_seconds))
    return renewed == 1


def release_event_lease(event, *, token: str) -> bool:
    """把租约还回去（⛔ 只用于「抢到了却没执行完」的兜底，见 `execution_lease` 的 `finally`）。 ⚠️ 它**不是**收尾路径：正常收敛的绝大多数走 `_finish_event`（那里带所有权地清租约），
    例外是 `consume_withdraw_event` 撤下成功那次直接落库（见本文件那处的注释）。本函数只处理「抢到租约之后**没有人落终态**」这一种形状（消费者抛出去、
    当场那次遇到不认识的 kind）——不还回去，那条事件就要**空等一个租约时长** 才轮到后台重试（状态还是 `pending`，却谁都不敢碰）。 ⭐ 判据同样是 `lease_owner == token`（修复波 3）：租约已易主时**一个字都不改**。"""

    released = JidanpuSyncEvent.objects.filter(
        pk=event.pk, lease_owner=token
    ).update(lease_expires_at=None, lease_owner=None)
    return released == 1


@contextmanager
def execution_lease(
    event, *, now: datetime, lease_seconds: int
) -> Iterator[str | None]:
    """「抢租约 → 跑 → 放租约」的唯一入口（修复波 3，A3）——当场那次 / 后台轮 / 请求路径共用。产出**所有权令牌**；`None` = 抢不到（别人正在跑）⇒ 调用方**必须让开**（⛔ 不许照打上游）。三件事在这里绑成一体（⛔ 别拆开用）：
    1. **抢**：`claim_event_for_execution`（条件更新 + 令牌）； 2. **续**：把 `renew_event_lease(自己)` 挂上 `jidanpu.upstream_heartbeat` ⇒ 每次真发上游 HTTP 请求之前续一次（心跳只延长自己的）；
    3. **放**：退出时无条件尝试 `release_event_lease(自己)`—— 正常收尾已被 `_finish_event` 清掉（那次调用命不中行、是无害的 no-op）；真正兜的是「执行者抛出去 / 没落终态」的形状，那条租约**立刻**还回去（否则后台要空等一个租约时长，D13② 那种「本会成功却要等 120 秒」会重演）。"""

    token = new_lease_token()
    if not claim_event_for_execution(
        event, now=now, lease_seconds=lease_seconds, token=token
    ):
        yield None
        return
    reset = _CURRENT_LEASE.set((event.pk, token))
    try:
        with jidanpu.upstream_heartbeat(
            lambda: renew_event_lease(event.pk, token, lease_seconds=lease_seconds)
        ):
            yield token
    finally:
        _CURRENT_LEASE.reset(reset)
        release_event_lease(event, token=token)
