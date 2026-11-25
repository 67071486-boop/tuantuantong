"""闲管家「无需物流发货」的领域实现（spec `2026-10-03-xgj-auto-ship-design.md` §3.2–§3.6）。 **两个入口共用这一份实现**（⛔ 不为两个入口各写一份判据，`AGENTS.md` §4）： | 入口 | 调用方 | `operator` | |---|---|---| | 订单进入「已完成」时自动发一次 | `settlement_hooks.on_order_completed_ship`（目标态钩子） | `系统` | | 页面「重试发货」 | `api/tb_send_log_views.py` 的 `retry` 动作 | 当前登录人显示名 | ## 一次发货会写哪两处留痕（spec §0 / §3.1 / §3.6） 1. **发货记录** `TaobaoOrderSendLog`（成功与失败**都**写一行，失败原因进 `remark`，D4）； 2. **订单日志** `OperationLog`——经唯一写入出口 `apps/audit/log.py::record(...)`，正文形如 `【系统】店铺发货返回结果：成功` / `【系统】店铺发货返回结果：失败（<原因>）` （模板见 `apps/audit/actions.py::ORDER_SHIP_RESULT`：前缀 `【{actor_name}】` 是**照抄对标那行 `Note`** 的形状，
自动触发时 `actor` = `系统`、重试时 = 当前登录人显示名——⛔ 不是写死的「系统」两字）。 ⚠️ 时间的 `【MM-DD HH:MM】` 前缀由**前端渲染**（对标也是渲染出来的）⇒ 落库只存正文；订单详情页渲染出来的整行形如 `【10-03 12:55】【系统】店铺发货返回结果：成功`。两处写在**同一个事务**里（要么都有、要么都没有）：只落一半会让「有记录没日志」这种半边留痕看起来像功能没跑。 ## 三条已决口径（spec §5 结清的那三条） - **D6**：`send_way` 固定 `1`（仅更新订单状态）⇒ ⛔ **不要**把它做成可配置项：那是另一条线；真要改成 `2` 是一次独立改动（代价见 spec §7-C1）。 - **D7**：该单**已有 `result=1` 的记录** ⇒ 不调上游、**不新增记录行**，只补一条订单日志（业务方原话「如果已发货 直接也展示 发货成功」）。 - **D8**：**未预期异常一并吞掉**、只记一条发货失败——这是对本仓 `E-0010` 纪律的**有意偏离**，两条必须同做的补救写在 `_internal_error_remark` 与 `send_order_shipping` 上（spec §5-3）。 ## 发货节流队列（D-0358）
「订单进入已完成」**只把单排进本进程的一个 FIFO 队列**（`dispatch_auto_ship`）；一条**常驻** daemon worker（懒启动）逐条发货，每发完一条睡 `settings.XGJ_SHIP_MIN_INTERVAL`（默认 1.0 秒）再取下一条 ⇒ 同一进程内任意时刻 **最多一笔在飞**。批量验收 N 张不再挤同一秒打上游（2026-11-01 00:27 kexing：批量验收 59 张挤同一秒，上游放行 20、拒绝 33「请求限流」）。 - **速率账**：web 2 个 gunicorn worker 各一条队列 ⇒ 全机最坏 ≈ `2 / interval` 笔/秒（默认 2 笔/秒）。依据是**实测**「上游同一秒放行了 20 笔才拒后面的」——约 10 倍余量； ⛔ 官方限额数字未证实前**不写**「闲管家限额是 X」，拿到再校准。 - **限流自动重试**：失败记录行的 `remark` 命中 `LIMIT_MARKERS` 且尝试未达 `MAX_SHIP_ATTEMPTS`（= **总尝试次数** 3：首发 + 2 次重排）⇒ 回队尾（attempt+1）；其余一切失败不重试。每次尝试照旧各留一行发货记录 + 一条订单日志（D4 不变） ⇒ 发货记录页呈「失败→失败→成功」，是如实留痕。 - **丢失窗口（如实登记，**不是**「与今天等价」）**：队列在**进程内存**里，进程重启 / 发版时**队列内未发条目全部丢失**——单笔场景与改前相当（在飞线程随进程死）， **批量场景窗口严格变宽**：59 张按 1s 间隔排空约 1 分钟，重启落在排空窗口内丢的是队列内全部未发条目（改前 59 个线程几秒内各自结束，窗口是秒级）。兜底 = 发货记录页重试 + 该单再完成一次会重发。「重启不丢」的数据库队列表是二期。 ## 与上游的关系（如实登记的边界）发货记录是**我们侧的快照**：闲管家侧的「已发货」状态我们**不回读**（spec §6 本轮不做） ⇒ 两边不保证一致。`send_way=1` 只把闲鱼订单标成已发货，**不把卖家配的卡密/文本发给买家** （spec §7-C1，D6 的代价）。"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque

from django.conf import settings
from django.db import connection, connections, transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.models import (
    Order,
    OrderStatus,
    SendResult,
    TaobaoOrderSendLog,
    XgjShop,
)
from apps.business import split_group
from apps.business.xgj import DEFAULT_BASE_URL, XgjError, logistics_dummy_send

logger = logging.getLogger(__name__)

#: 自动触发那一路的操作人（spec §3.1 / §3.6：对标那行日志的操作人就是「系统」）。
SYSTEM_OPERATOR = "系统"

#: 订单日志的动作码（登记在 `apps/audit/actions.py`；`log_type` 从同一张登记表取，
#: ⛔ 不在这里写死 `10`——分类是登记表的事实，抄一份就会与它漂移）。
SHIP_RESULT_ACTION = "order.ship_result"

#: 上游的 `send_way`（D6）：`1` = **仅更新订单状态**。⛔ 不做成配置项（spec §5-1）。
SEND_WAY = 1

#: 单次上游调用的超时上限（秒）。spec §3.4-2 把「超时上限」留作待定项 ⇒ 这里显式写一个
#: **具名常量**并取 `xgj.call` 的默认值（⛔ 不是拍脑袋的新数字：它等于不传时的行为）。
#: ⚠️ 这是 **socket 单次读写**级：PROD-02 实测证明它**封不住总时长**，所以线程体另有下面那个「整轮加总」的看门狗预算。
SHIP_TIMEOUT = 10.0

#: 自动发货**整轮**的加总时长预算（秒）：正常一轮 = 一次上游调用（socket 单次 ≤ `SHIP_TIMEOUT`）+ 按单号重取订单 + 两笔留痕写入，预算给足一整次上游超时再加一倍余量。
#: 超过它 ⇒ 看门狗记 warning 留痕（`_warn_ship_over_budget`）；昨夜那次事故是堵满 30 秒
#: 被 worker 杀掉，预算取 2 倍单次超时（20s）意味着**在 worker 被杀之前**日志里就有迹可循。
AUTO_SHIP_TIME_BUDGET = SHIP_TIMEOUT * 2

#: 「解析不到店铺 / 应用」的统一失败原因（spec §3.3 给的**逐字**文案）。
NO_BINDING_REASON = "未绑定闲管家店铺/应用"

#: 订单没填来源单号 ⇒ 不调上游（D2）。
NO_ORDER_NO_REASON = "订单没有来源单号，无法发货"

#: 重试时系统订单已经不在了（软删 / 硬删）⇒ 没有可发的单，但仍要留一条失败。
ORDER_GONE_REASON = "系统订单不存在或已删除，无法发货"

SUCCESS_REASON = "成功"

#: 上游对「这单已经发过货」的拒绝回执（E-0237）：闲管家对这一形态回的是**失败码**，没有独立的「已发货」业务码可判 ⇒ 只能按**消息文本**认。业务方裁定：
#: 「订单已发货不支持操作」= 目标态已达成 ⇒ **记发货成功**（与 spec §3.5 D7 的我方库幂等互补：那一支兜的是「我方没发过」，这一支兜的是「对方侧已发过」——人工在闲管家
#: 发过、或从别的渠道发过，我方库没有成功行）。子串匹配、容忍前后缀；⚠️ 判据按业务方报来的原话，上游改文案 ⇒ 回到「可见的失败」（不静默，照旧记失败 + remark 原文）。
ALREADY_SHIPPED_MARKERS = ("已发货不支持操作",)

#: 上游回执视为成功时，记录 `remark` 里留的那句人话（原文仍跟在后面，可追溯）。
ALREADY_SHIPPED_REMARK = "上游回执：订单已发货，按业务方口径记为发货成功"

#: 上游「请求限流」回执的认法（D-0358）：与 `ALREADY_SHIPPED_MARKERS`（E-0237）同一形状 ——上游业务码未知（生产台账只见文案「请求限流，请稍后重试」），按**消息子串**认；上游改文案 ⇒ 退化为「可见的失败」（照记失败行，不静默，不再自动重试）。
#: ⚠️ **匹配面与 E-0237 不同，边界如实登记**：那边在 `exc.upstream_msg` 上匹配，这边在 **记录行 `remark`** 上匹配——中间隔了 `_redact_secret`（限流文案不含密钥，原样通过）
#: 与 255 列宽截断（`TaobaoOrderSendLog.remark`）；截断只切**尾部**、生产文案的 marker 在**头部** ⇒ 可判。⚠️ 因此 marker 必须命中文案**头部**才保证可判：上游若把限流文案改成「超长前缀 + 尾部请求限流」，本判据会漏认——退化为可见的失败，不会误判成功。
LIMIT_MARKERS = ("请求限流",)

#: 限流自动重试的上限。⚠️ 含义 = **总尝试次数**（首发 + 2 次重排），不是「重试 3 次」：
#: 第 3 次仍限流 ⇒ 停（库内恰 3 行失败记录，发货记录页可见），等人工重试或该单再完成重发。
MAX_SHIP_ATTEMPTS = 3

#: 自动发货队列**最小发货间隔**（秒）的设置键与兜底值（D-0358）。 worker 每发完一条睡这一档再取下一条；读法是**运行期** `getattr`（照
#: `JIDANPU_IMAGE_TIMEOUT` 先例，`override_settings` 才能生效）。配置 `≤ 0` / NaN ⇒ 回落 1.0 并记 warning——0/负值会让节流**静默失效**、批量又挤回同一秒
#: （退化成既往那次批量发货事故的形状）。`settings.py` 侧用 `env.float`：字面写坏启动即抛（与 `env.int` 同一风格），运行期回落只兜运行期才出现的坏值。
XGJ_SHIP_MIN_INTERVAL_SETTING = "XGJ_SHIP_MIN_INTERVAL"
DEFAULT_SHIP_MIN_INTERVAL = 1.0

def _is_already_shipped(exc: XgjError) -> bool:
    return any(marker in exc.upstream_msg for marker in ALREADY_SHIPPED_MARKERS)


def _snapshot_number(order, tb_order_no) -> str:
    """优先用发货方提供的单号快照，未提供才回读订单来源号。"""
    raw = order.source_order_no if tb_order_no is None else tb_order_no
    return (raw or "").strip()


def _shop_for_order(order) -> XgjShop | None:
    """按 spec §3.3 的解析链取这家店：`Order.order_source` → `XgjShop.order_source`。
    ⛔ 判据必须是 `order.order_source_id is None`（不是对象真值）：订单没选来源时
    `.order_source` 会抛 `RelatedObjectDoesNotExist`（`SET_NULL` 的可空 FK），拿它做真值判断会变成 500。"""
    if order.order_source_id is None:
        return None
    return XgjShop.objects.filter(order_source_id=order.order_source_id).first()


def _credentials_of(binding):
    """取一条闲管家绑定的上游凭据；不完整回 `None`（⛔ 判据不在这里再写一份）。判据的唯一出处是 `apps/business/api/xgj_views.py::credentials_of`（`strip()` + 非空）。
    ⚠️ 阿奇索（`agiso`）与鸡蛋铺（`jidanpu`）那两条线把它放在**领域**模块里，闲管家线当初放在了 **api** 模块 —— 本模块是领域层，所以用**函数内 import** 复用它，
    ⛔ 不在这里抄一份 `strip` 判据（同一事实两套判据必然漂移，E-0021）。"""
    from apps.business.api.xgj_views import credentials_of

    return credentials_of(binding)


def _base_url() -> str:
    """闲管家基址**只由平台配置提供**（spec 与 `xgj_views` 同口径）。
    ⚠️ 用 `or` 而不是 `getattr(..., DEFAULT_BASE_URL)` 的默认参数：`XGJ_API_BASE=` 空串是
    **合法配置**（= 用实测基址），默认参数只在属性不存在时生效。"""
    return getattr(settings, "XGJ_API_BASE", "") or DEFAULT_BASE_URL


def _clip_to_column(value, model, field_name: str) -> str:
    """把一段文本裁进 `model` 里 `field_name` 那一列的宽度（⛔ 列宽不抄数字）。口径与 `apps/business/peer_inbound.py::_clip_to_column` / `apps/audit/log.py::_clip` 同一先例：
    上游原文是**第三方文本**，长度不可控；不裁就是 PostgreSQL 的 `value too long for type character varying(64)` ⇒ 整条发货路径**响亮 500**（E-0010 的形状）。
    ⚠️ 有意截断、有意不报错（代价：超长原因在记录里只剩前缀）——`remark` 的用途是「让人看得出为什么失败」，不是「完整存档上游回包」。"""
    text = "" if value is None else str(value)
    width = model._meta.get_field(field_name).max_length
    return text if width is None else text[:width]


def _redact_secret(msg: str, secret: str) -> str:
    """落库前把上游原文里可能夹带的 **AppSecret** 抹掉（spec §9-S1 的同一条纪律）。判据不再写第二份：复用 `apps/business/api/platform_order_views.py::_redact_secret`
    （它自己说明了为什么闲管家这一侧只需要防「上游回显」这一条路径）。 ⚠️ 我们用 `or` 兜住空密钥的场景（`_redact_secret` 自身对空 `secret` 也是原样返回，
    但 `credentials_of` 保证非空——两道都留着不会改变结果）。"""
    from apps.business.api.platform_order_views import _redact_secret

    return _redact_secret(msg, secret)


def _internal_error_remark(exc: BaseException) -> str:
    """D8 的**补救之一**（spec §5-3）：`remark` 必须点名「这是我们自己的代码出错」。 ⛔ **不许**把它写成「上游拒绝」那一类文案：那会让排障的人去查闲管家，方向错。
    形状是 `内部错误：<异常类型>: <异常摘要>`（摘要压成一行、按列宽裁）。补救之二是同一次 `logger.exception(...)`（在 `send_order_shipping` 里，写进服务器日志）。"""
    detail = " ".join(str(exc).split())
    return _clip_to_column(
        f"内部错误：{type(exc).__name__}: {detail}",
        TaobaoOrderSendLog,
        "remark",
    )


def _order_log_reason(result: int, remark: str) -> str:
    """订单日志的正文（spec §3.6 的形状）。成功 ⇒ `成功`；失败 ⇒ `失败（<原因>）`——括号里就是记录行 `remark` 的原文，
    ⛔ 不在这里另写一套判断（否则同一件事会有两句可能不一致的话）。"""
    if result == SendResult.SUCCESS:
        return SUCCESS_REASON
    return f"失败（{remark}）"


def _write_order_log(*, order_no: str, source_order_no: str, result: int, remark: str,
                     operator: str, actor_id=None, already_shipped=False) -> None:
    """写那条订单日志（唯一写入出口 `record(...)`，⛔ 不手写 `OperationLog`）。"""
    record(
        log_type=ACTION_LOG_TYPES[SHIP_RESULT_ACTION],
        action=SHIP_RESULT_ACTION,
        actor=operator,
        actor_id=actor_id,
        # ⭐ `actor_id` 默认 `None`（自动发货那条路 `operator=SYSTEM_OPERATOR`
        # 就是这一档）⇒ **值级**的机器来源必须显式声明，⛔ 不静默通过（见 `log.py`）。
        system=actor_id is None,
        target_kind="order",
        target_id=order_no,
        order_no=order_no,
        source_order_no=source_order_no,
        reason="已完成，本次跳过重复发货" if already_shipped else _order_log_reason(result, remark),
    )


def _write_attempt(*, order_no: str, tb_order_no: str, send_params: str, store_name: str,
                   result: int, remark: str, operator: str, actor_id=None):
    """落一条发货记录 **+** 它对应的那条订单日志（同一个事务，见模块 docstring）。
    ⛔ 块内**只有写入、没有 `return`**（E-0022：`atomic()` 是事务不是回滚点，
    块内正常 `return` 会提交前面几笔写入、留下「一半成功」的脏行）。"""
    with transaction.atomic():
        row = TaobaoOrderSendLog.objects.create(
            order_no=order_no,
            tb_order_no=_clip_to_column(tb_order_no, TaobaoOrderSendLog, "tb_order_no"),
            send_params=send_params,
            store_name=_clip_to_column(store_name, TaobaoOrderSendLog, "store_name"),
            result=result,
            sent_at=timezone.now(),
            operator=_clip_to_column(operator, TaobaoOrderSendLog, "operator"),
            remark=_clip_to_column(remark, TaobaoOrderSendLog, "remark"),
        )
        _write_order_log(
            order_no=order_no,
            source_order_no=tb_order_no,
            result=result,
            remark=row.remark,
            operator=operator,
            actor_id=actor_id,
        )
    return row


def _record_failure(*, order_no: str, tb_order_no: str, store_name: str, remark: str,
                    operator: str, send_params: str = "", actor_id=None):
    return _write_attempt(
        order_no=order_no, tb_order_no=tb_order_no, send_params=send_params,
        store_name=store_name, result=SendResult.FAILED,
        remark=_clip_to_column(remark, TaobaoOrderSendLog, "remark"),
        operator=operator, actor_id=actor_id,
    )


def send_order_shipping(order, *, operator: str, actor_id=None, tb_order_no=None, routing_order=None):
    """对一张订单发一次货；返回**本次新建**的发货记录行（幂等跳过时回 `None`）。 ⛔ **本函数不抛业务异常**：一切失败都在这里翻译成「记一条失败 + 写一条日志」
    （spec §3.4 硬要求 1：发货失败不得让订单状态回滚）。D8 之下**未预期异常也吞**。 ⚠️ 兜底写入自身失败时异常会**冒泡**（那一刻订单状态早已提交 ⇒ 不会回滚，
    只是这次请求 500）——那是**有意**的：连「记一条失败」都写不进去说明库出了问题，监控不该失明（D8 吞的是**业务路径**上的未预期异常，不是「连留痕都写不进去」）。"""
    order_no = order.order_no
    number = _snapshot_number(order, tb_order_no)
    store_name = ""
    send_params = ""
    try:
        # ── D7 幂等：已有成功行 ⇒ 不调上游、不新增记录行，只补一条订单日志 ──────────
        if TaobaoOrderSendLog.objects.filter(
            order_no=order_no, result=SendResult.SUCCESS
        ).exists():
            _write_order_log(
                order_no=order_no, source_order_no=number, result=SendResult.SUCCESS,
                remark="", operator=operator, actor_id=actor_id, already_shipped=True,
            )
            return None
        if not number:
            return _record_failure(
                order_no=order_no, tb_order_no="", store_name="", remark=NO_ORDER_NO_REASON,
                operator=operator, actor_id=actor_id,
            )
        # ── 解析链（spec §3.3）──────────────────────────────────────────────
        shop = _shop_for_order(order if routing_order is None else routing_order)
        if shop is not None:
            store_name = shop.shop_name or ""
        binding = shop.binding if shop is not None else None
        credentials = _credentials_of(binding)
        if credentials is None:
            return _record_failure(
                order_no=order_no, tb_order_no=number, store_name=store_name,
                remark=NO_BINDING_REASON, operator=operator, actor_id=actor_id,
            )
        app_key, app_secret = credentials
        send_params = json.dumps(
            {"order_no": number, "send_way": SEND_WAY}, ensure_ascii=False
        )
        # ── 上游调用（唯一的 HTTP 出口；网络/业务失败都在 xgj.py 里翻成 XgjError）──
        try:
            logistics_dummy_send(
                number,
                app_key=app_key,
                app_secret=app_secret,
                send_way=SEND_WAY,
                base_url=_base_url(),
                timeout=SHIP_TIMEOUT,
            )
        except XgjError as exc:
            if _is_already_shipped(exc):
                # E-0237：「已发货」回执 = 目标态已达成 ⇒ 记**成功**（remark 留上游原文可追溯）。
                # 写成功行（而非只写订单日志）才能让 D7 幂等在下次重试时认出「已发过」。
                return _write_attempt(
                    order_no=order_no, tb_order_no=number, send_params=send_params,
                    store_name=store_name, result=SendResult.SUCCESS,
                    remark=_clip_to_column(
                        f"{ALREADY_SHIPPED_REMARK}（上游原文：{_redact_secret(exc.upstream_msg, app_secret)}）",
                        TaobaoOrderSendLog, "remark",
                    ),
                    operator=operator, actor_id=actor_id,
                )
            return _record_failure(
                order_no=order_no, tb_order_no=number, store_name=store_name,
                remark=_redact_secret(exc.upstream_msg, app_secret),
                operator=operator, send_params=send_params, actor_id=actor_id,
            )
    except Exception as exc:  # noqa: BLE001 —— D8 的**有意偏离**，见模块 docstring
        # 补救之一：服务器日志里留下完整栈（`journalctl -u tuantuantong` 看得见）。
        logger.exception(
            "闲管家发货内部错误 order_no=%s operator=%s", order_no, operator
        )
        # 补救之二：记录里点名这是**内部错误**（⛔ 不伪装成上游拒绝）。
        return _record_failure(
            order_no=order_no, tb_order_no=number, store_name=store_name,
            remark=_internal_error_remark(exc),
            operator=operator, send_params=send_params, actor_id=actor_id,
        )
    return _write_attempt(
        order_no=order_no, tb_order_no=number, send_params=send_params,
        store_name=store_name, result=SendResult.SUCCESS, remark="",
        operator=operator, actor_id=actor_id,
    )


def schedule_auto_ship(order) -> None:
    """把「订单完成后自动发一次货」排到**最外层事务提交之后**（spec §3.4 硬要求 2）。为什么必须 `transaction.on_commit`：发货要打一个外部 HTTP，在 `atomic()` 里同步调会让**订单行锁**被持有到上游返回/超时。排到提交之后 ⇒ 那一刻订单状态已落库、行锁已释放，上游慢或挂都不占任何数据库锁。 ⚠️ 提交之后的那一步（`dispatch_auto_ship`）**只入队、不出网**：真正的上游 HTTP
    由本进程的常驻队列 worker 逐条发（D-0358；改前是每单一个短命 daemon 线程， PROD-02 起请求路径就不等上游——再往前 on_commit 同步等上游，实测被拖满 30 秒杀成 worker timeout）。请求路径**不等**任何 HTTP 的口径不变。 ⚠️ 事务**回滚**时回调被丢弃 ⇒ 不会出现「订单没改成已完成、货却发了」。 ⚠️ **测试坑**：`TenantTestCase` 把每条用例包在一层事务里 ⇒ 回调**默认不执行**，
    要驱动它必须用 `self.captureOnCommitCallbacks(execute=True)`（先例见 `apps/business/jidanpu_hall.py::schedule_immediate_attempt` 的同一段说明）；且执行点会**真入队**——留痕类既有网把 `dispatch_auto_ship` 替成「线程体同步直调」替身（见 `tests/test_xgj_auto_ship.py::_ShipFixture.complete`），队列网把 worker 懒启动替成空操作后直驱队列缝（见 `tests/test_xgj_ship_queue.py`）。"""
    order_no = order.order_no
    transaction.on_commit(lambda: dispatch_auto_ship(order_no))


def _order_from_upstream(order) -> bool:
    """这单是不是「上家来源」（业务方 2026-11-09 拍板：上家来源单不走闲管家自动发货）。
    两条上家链路任一命中即真：上家平台线（`JidanpuUpstreamOrderLink`）与团团通上家线
    （`PeerOrderLink` 带上游绑定）。⚠️ 两条 EXISTS 并成**一条** Order 侧 OR-join（闸挂在
    全仓最热的完成路径上）：⛔ 不能改成 PeerOrderLink 侧 OR-join——只有上家平台链路、
    没有 peer 行的单会让那张基表零行 ⇒ 漏判成「非上家来源」而误发货。页面手动「重试发货」
    不经本判据（人主动点，保持现状）。查询计数判据：`test_jidanpu_upstream_triggers`
    的 `test_zero_binding_costs_zero_queries`（钉住「闸恰好一次查询」）。"""
    return Order.all_objects.filter(
        Q(jidanpu_upstream_links__isnull=False)
        | Q(peer_links__upstream_binding__isnull=False),
        pk=order.pk,
    ).exists()


def _group_shipping_succeeded(main_no, active_member_nos) -> bool:
    historical_member_nos = split_group.shipping_lineage_order_nos(main_no)
    return TaobaoOrderSendLog.objects.filter(
        Q(order_no=main_no)
        | Q(order_no__in=active_member_nos)
        | Q(order_no__in=historical_member_nos),
        result=SendResult.SUCCESS,
    ).exists()


def schedule_auto_ship_gated(order) -> None:
    """**D-0357（G6/G7）发货闸**：拆单组要「全组完成」才排发货；非拆单单属性短路照旧。 ## 判据（spec §3.2，逐字） - 本单 `split_id` 为空 ⇒ **零查询**照旧直排（普通单行为一个字节不变，G5 的另一面）； - 非空 ⇒ 组内判「**除本单外**全部 `COMPLETED`」：未齐 ⇒ 写一条**人可见**订单日志 `【系统】拆单组未全部完成（X/N），暂不发货` （`X/N` = 组内已完成数/活跃成员数，含本单自己）并**跳过**；齐 ⇒ 照旧 `schedule_auto_ship`（D-0358 队列对闸**透明**：闸=「这单要不要排」，队列=「怎么发」）。 - **G7 组幂等**（判据从「本单已有 SUCCESS 行」扩为组维度）：组内**任一**成员已有 `result=SUCCESS` 发货行 ⇒ 只补一条与 D7 同形的「成功」订单日志、**不重发** ——堵「组内 A 发过货、B 重开重完成对同一上游单真发第二次」。 ⚠️ 只挂在**自动口**：页面「重试发货」（`retry_row` → `send_order_shipping`）
    **不经本函数**，D7 的按单幂等原样保留（边界，spec §6）。 ## 为什么锁的是「组内 `id` 最小的成员行」（组互斥锁）而不是锁全组 spec G6 要求 `select_for_update` 锁组成员行、防「末两单并发完成双双跳发」； ⚠️ 但本函数跑在 `apply_transition` 的调用方事务里，而那个事务**已经持有本单自己的行锁**（`order_status.py` 迁移前 `select_for_update`）。若这里再按 `id` 序锁**全组**，「并发完成末两单」的两个事务各持自己那行、再去按同序要对方的行 ⇒ **AB-BA 死锁** （Postgres 1 秒后杀掉其中一个，用户看到一次响亮 500——闸的目的是收敛语义，不是给正常操作加一次重试）。⇒ 锁**一行**作组互斥：组内 `id` 最小的**活跃**成员行（主单存在时就是主单；主单缺行的残留组退而锁当前最小者——同一组永远锁同一行）。锁下用 READ COMMITTED 的新快照读全组成员：后到的事务必然看到先到事务**已提交**的
    `COMPLETED` ⇒ 「双双跳发」（双方都看到对方没完成）与「双双跳过」（谁都不发）在这个锁下**结构性不可能**；每个事务**至多**新增这一把锁、持锁者不再等别人 ⇒ 无环。 ⚠️ 顺序换气窗口如实登记：A 完成时 B 未完成 ⇒ A 留「暂不发货」日志；B 提交后那条日志**不会撤回**——它如实记录了「A 完成那一刻确实没齐」，是历史不是错误。 G8 出组收口会补上真正的发货。 ⚠️ **闸 vs 删除路径的 AB-BA 窗口（S1 登记；删那一侧的镜像见 `recycle.soft_delete_order`）**：本闸锁「组内 id 最小成员**一行**」时，调用方事务**已持有本单自己的行锁**（完成路径锁序 = 本单 → 最小成员）；而 `recycle.soft_delete_order` **从零开始**按 id 升序锁**全组**成员行（锁序 = 最小成员 → 其余）。两组锁形**各自内部**无环，**跨族**却有毫秒级窗口：同组「一单验收（非最小成员：持本单、等闸的最小成员行）＋另一单（最小成员）删除（持最小、再等那张本单）」几乎同时（批量删除跨组反序是同族形状）⇒ AB-BA 成环， Postgres 约 1 秒杀掉其中一个 ⇒ 用户看到一次响亮 500。**接受**：两个事务都干净回滚、状态与钱账零损伤，重试即成；要消掉它得统一两条路径的锁序（牵动删除「枚举并改写全体成员」的形状），修复复杂度与这个窗口的代价不成比例。 ⚠️ **调用方必须在 `transaction.atomic()` 里**（`select_for_update` 同 `lock_order_row` 的契约）：两个调用方（`settlement_hooks.on_order_completed_ship` 钩子与 `recycle` 的 G8 收口）都在调用方事务内。"""
    # 上家来源单不发货（业务方 2026-11-09 拍板）：零痕迹跳过——不排队、不写发货记录
    # 与订单日志；手动「重试发货」不经本闸、仍按既有口径留痕。判据见 `_order_from_upstream`。
    # 位置在拆单闸之前 ⇒ 拆单成员是上家来源单时同样跳过。
    if order.penalty_approval_id is not None:
        return
    refunded = order.status == OrderStatus.REFUNDED
    if not refunded and _order_from_upstream(order):
        return
    if order.split_id == "":
        # 非拆单：属性短路、零查询（spec G6 的字面口径；普通单零行为变化）。
        if not refunded:
            schedule_auto_ship(order)
        return
    # 组互斥锁（见 docstring「为什么锁一行」）：先取组内最小 `id`，再锁那一行。
    mutex_id = (
        Order.objects.filter(split_id=order.split_id)
        .order_by("id")
        .values_list("id", flat=True)
        .first()
    )
    if mutex_id is None:
        # 组在「完成判定」与「锁」之间全部出组（并发删除的竞态窗口，残留登记 R6 同族）
        # ⇒ 没有组闸可判，按普通单处理（与改动前同形：有单号就排）。
        if not refunded:
            schedule_auto_ship(order)
        return
    Order.objects.select_for_update().filter(pk=mutex_id).first()
    members = list(split_group.active_members(order.split_id))
    waiting_members = split_group.shipping_members(members)
    if not waiting_members:
        return
    anchor = members[0]
    main = split_group.shipping_main_order(anchor)
    if main is None or ((main.pk != order.pk or refunded) and _order_from_upstream(main)):
        return
    member_nos = [m.order_no for m in members]
    # ── G7 组幂等：组内任一成员已有 SUCCESS 行 ⇒ 只补成功日志、不重发 ─────────────
    #    含本单自己（本单发过、重开重完成 ⇒ 与 D7 同答案，只是判据收口在组维度）。
    if _group_shipping_succeeded(main.order_no, member_nos):
        _write_order_log(
            order_no=order.order_no,
            source_order_no=_snapshot_number(main, None),
            result=SendResult.SUCCESS,
            remark="",
            operator=SYSTEM_OPERATOR,
            already_shipped=True,
        )
        return
    completed = sum(
        1 for m in waiting_members if m.status == OrderStatus.COMPLETED
    )
    total = len(waiting_members)
    if completed < total:
        # 「暂不发货」留痕：人可见的订单日志（正文 `【系统】拆单组未全部完成（X/N），
        # 暂不发货`；spec G6 的逐字口径）。动作走 `apps/audit` 唯一出口。
        record(
            log_type=ACTION_LOG_TYPES["order.split_ship_hold"],
            action="order.split_ship_hold",
            actor=SYSTEM_OPERATOR,
            # 自动触发的机器行：显式声明（`record()` 的 fail-closed 判据）。
            system=True,
            target_kind="order",
            target_id=order.order_no,
            order_no=order.order_no,
            reason=f"{completed}/{total}",
        )
        return
    # 同一组按最早正常订单进队，退单只保留原店铺和来源号的路由角色。
    schedule_auto_ship(waiting_members[0])


# ── 发货节流队列（D-0358）：进程内单例 ────────────────────────────────────────
#: 队列元素 = `(order_no, schema_name, attempt)`；`attempt` **1 基**（首发 = 1）。
_ship_queue: deque = deque()
#: 「待发 / 在飞」去重集（按单号）：入队时加入，**每条终态**（成功 / 失败不重试 /
#: 限流耗尽 / 未预期异常）清出。它同时挡「同一单排两条」与「在飞时再排」。
_ship_pending: set[str] = set()
_ship_lock = threading.Lock()
_ship_worker_thread: threading.Thread | None = None

#: worker 空转时的轮询间隔（秒）。⛔ 不改用 `Condition.wait()` 阻塞等：
#: `_ship_worker_step` 要作**可同步直调**的确定性缝（测试直驱），空队列时阻塞
#: 会让直驱挂死；0.1s 轮询的代价只是空转唤醒，且首单最多多等这 0.1s。
_SHIP_IDLE_POLL_SECONDS = 0.1


def _ensure_ship_worker_locked() -> None:
    """懒启动**单条**常驻 worker（调用方必须已持有 `_ship_lock`）。首次入队时启动，进程存活期内不退出（`_ship_queue_loop` 是 `while True`）；
    万一它真的死了（防不住的 BaseException），下一次入队按 `is_alive()` 重新拉起。
    daemon = 进程退出不等它（丢失窗口见模块 docstring「发货节流队列」一节）。"""
    global _ship_worker_thread
    if _ship_worker_thread is not None and _ship_worker_thread.is_alive():
        return
    _ship_worker_thread = threading.Thread(
        target=_ship_queue_loop, name="xgj-ship-queue-worker", daemon=True
    )
    _ship_worker_thread.start()


def dispatch_auto_ship(order_no: str) -> None:
    """`schedule_auto_ship` 的 on_commit 回调：把这一单**排进本进程的发货队列**（D-0358）。本函数在**请求线程**里跑，入队即返回——上游慢 / 挂 / 卡死从此不占请求的响应时间（PROD-02 口径不变）；真正的上游 HTTP 由常驻 worker 逐条发，同一进程内任意时刻
    最多一笔在飞，批量验收不再挤同一秒打上游（2026-11-01 kexing 事故的根因修复）。 ⚠️ schema 是**连接级**状态（django-tenants）：worker 线程拿到的是**另一条**连接，没有请求线程设过的租户 schema ⇒ 把 `connection.schema_name` 当**普通值**在
    **入队时**捕获、随队列元素带过去（⛔ 不能在 worker 里再查），每条处理时才落 schema（`order_push._send_in_thread` 的同一条硬约束）。 ⚠️ 同一单已在队列或在飞 ⇒ 本次丢弃：完成态钩子可能对同一单短时间内再触发（完成 → 退回 → 再完成），在飞的这一次 + D7 幂等已兜住语义，不需要排两条。"""
    schema_name = connection.schema_name
    with _ship_lock:
        if order_no in _ship_pending:
            logger.info(
                "闲管家自动发货：该单已在队列或在飞，丢弃本次入队 order_no=%s", order_no
            )
            return
        _ship_pending.add(order_no)
        _ship_queue.append((order_no, schema_name, 1))
        _ensure_ship_worker_locked()


def _ship_queue_loop() -> None:
    """常驻 worker 的**循环壳**：无限地「取一条 → 处理」（计划 §2.1 的确定性缝）。
    全部业务逻辑都在可同步直调的 `_ship_worker_step` / `_process_ship_item` 里；
    本壳只做两件事：转圈，以及兜底——单步意外抛错记完整栈后**继续**（worker 一亡，队列里后面的单全部滞留，比丢一条严重得多）。"""
    while True:
        try:
            _ship_worker_step()
        except Exception:  # noqa: BLE001 —— 兜底；能到这里的都不是业务失败
            logger.exception("闲管家发货队列 worker 单步内部错误（线程继续）")


def _ship_worker_step() -> None:
    """worker 的一步：取一条（空则小睡轮询）→ 处理 → 按配置睡一个最小间隔。 ⚠️ 「睡」在**发完之后**且**每条都睡**：同一进程内相邻两笔上游调用至少隔
    `settings.XGJ_SHIP_MIN_INTERVAL`（运行期现读，见 `_ship_min_interval`）——
    这就是节流本体。间隔睡完队列已空 ⇒ 下一轮走空转分支，无副作用。"""
    with _ship_lock:
        item = _ship_queue.popleft() if _ship_queue else None
    if item is None:
        time.sleep(_SHIP_IDLE_POLL_SECONDS)
        return
    order_no, schema_name, attempt = item
    _process_ship_item(order_no, schema_name, attempt)
    time.sleep(_ship_min_interval())


def _ship_min_interval() -> float:
    """最小发货间隔（秒）：设置缺失 / ≤ 0 / NaN ⇒ 回落 1.0 并记 warning（D-0358）。
    ⚠️ **运行期**现读（`getattr`），`override_settings` 才生效（照 `jidanpu_upstream_pics.pic_timeout_seconds` 先例）；⛔ 不静默按 0——
    0/负值会让节流失效，批量发货又挤回同一秒（那次事故里 事故形状）。"""
    raw = getattr(settings, XGJ_SHIP_MIN_INTERVAL_SETTING, DEFAULT_SHIP_MIN_INTERVAL)
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        interval = float("nan")
    if math.isnan(interval) or interval <= 0:
        logger.warning(
            "%s 配置非法（%r）：回落 %.1f 秒（节流不许静默失效）",
            XGJ_SHIP_MIN_INTERVAL_SETTING, raw, DEFAULT_SHIP_MIN_INTERVAL,
        )
        return DEFAULT_SHIP_MIN_INTERVAL
    return interval


def _is_limited_row(row) -> bool:
    """这条发货记录是不是「上游限流」回执（D-0358 重试判据的**唯一出处**）。判据 = `result == FAILED` **且** `remark` 命中 `LIMIT_MARKERS`（子串）。
    匹配面边界（remark 已过脱敏与 255 截断、截断只切尾、marker 须在文案头部）见 `LIMIT_MARKERS` 的注释，⛔ 不在这里再写一份。"""
    return row.result == SendResult.FAILED and any(
        marker in row.remark for marker in LIMIT_MARKERS
    )


def _requeue_tail(order_no: str, schema_name: str, attempt: int) -> None:
    """限流重排：回**队尾**（`attempt` 已由调用方 +1）；该单**留在** pending 集
    （它仍在队列里，不算「已出队」）。
    """
    with _ship_lock:
        _ship_queue.append((order_no, schema_name, attempt))


def _clear_pending(order_no: str) -> None:
    """把该单从去重集清掉——**每条终态都必须走到**（成功 / 失败不重试 / 限流耗尽 /
    未预期异常）。 ⚠️ 漏清的后果不是小事：去重集会把这个单永远当「在飞」，该单之后**再完成
    也入不了队**（实测抓过的洞）。"""
    with _ship_lock:
        _ship_pending.discard(order_no)


def _process_ship_item(order_no: str, schema_name: str, attempt: int) -> None:
    """队列的「每条函数」（**可同步直调**的确定性缝，计划 §2.1）：处理一条队列元素。发货本体（看门狗 / 异常兜底 / 关连接）在 `_auto_ship_in_thread`，本函数只做 **重试判据**： - 记录行是限流回执（`_is_limited_row`）且 `attempt < MAX_SHIP_ATTEMPTS`
    ⇒ 回队尾（attempt+1），该单留在 pending 集（还在队列里）； - 限流但尝试已打满 ⇒ 停（库内恰 `MAX_SHIP_ATTEMPTS` 行失败记录），**清 pending**； - D7 幂等跳过回 `None`、订单不存在返回失败行（`ORDER_GONE` 支写留痕并 return 该行）⇒ 都不重试；
    - 其余一切失败（未绑定 / 无来源单号 / 非限流 XgjError / 内部错误）⇒ 不重试。 ⚠️ 单条全程兜底：未预期异常 `logger.exception` 后**清 pending 返回**——线程不许亡，且不许让异常把该单永久钉死在去重集里。"""
    try:
        row = _auto_ship_in_thread(order_no=order_no, schema_name=schema_name)
    except Exception:  # noqa: BLE001 —— 单条全程的兜底，见 docstring
        logger.exception(
            "闲管家发货队列条目未预期异常 order_no=%s attempt=%s", order_no, attempt
        )
        _clear_pending(order_no)
        return
    if row is not None and _is_limited_row(row):
        if attempt < MAX_SHIP_ATTEMPTS:
            _requeue_tail(order_no, schema_name, attempt + 1)
            return
        logger.warning(
            "闲管家发货限流重试耗尽（共 %s 次尝试，不再自动重试）order_no=%s",
            attempt, order_no,
        )
    _clear_pending(order_no)


def _warn_ship_over_budget(order_no: str) -> None:
    """看门狗到点：整轮发货超过 `AUTO_SHIP_TIME_BUDGET` 还没结束 ⇒ 只记 warning 留痕。
    ⛔ 它**杀不掉**堵住的线程——socket 超时是单次读写级，「总时长堵不住」是**如实登记** 的边界（计划 §2.3 的口径：不发明复杂的跨轮补偿）。它的价值是让「疑似卡死」在
    服务器日志里**可见**（`journalctl -u tuantuantong` 找这条），而不是无声无息。"""
    logger.warning(
        "闲管家自动发货超过 %.0f 秒仍未结束（疑似上游卡死；socket 超时封不住总时长，"
        "线程是 daemon、进程重启即丢）order_no=%s",
        AUTO_SHIP_TIME_BUDGET,
        order_no,
    )


def run_auto_ship(*, order_no: str, schema_name: str | None) -> TaobaoOrderSendLog | None:
    """发货本体（**可直调缝隙**）：按需落 schema 后复用既有的 `auto_ship`。 - `schema_name` 非空 ⇒ `connection.set_schema(...)`（它只设置**连接级**状态，真正的 `SET search_path` 在下一个游标上才发生——django_tenants 后端
    `_handle_search_path`）；落的就是请求线程当时那个租户 schema。 - `schema_name=None` ⇒ 不动 schema（既有测试在测试线程里**同步直调**时就是这一档：测试线程的 schema 已由 `TenantTestCase` 设好）。
    成功写留痕、失败写失败留痕的判据全在 `auto_ship → send_order_shipping` 里，本函数不新增判据，只把 `auto_ship` 透传上来的**本次记录行**原样返回（D-0358：队列的重试判据靠它，见 `_process_ship_item`）。"""
    if schema_name:
        connection.set_schema(schema_name)
    return auto_ship(order_no)


def _auto_ship_in_thread(
    *, order_no: str, schema_name: str
) -> TaobaoOrderSendLog | None:
    """**每条处理体**（D-0358 起由队列 worker 逐条调用）：看门狗 → 发货本体 → 关连接。返回 `run_auto_ship` 透传上来的**本次记录行**；D7 幂等跳过时是 `None` （业务失败一律在 `send_order_shipping` 里翻成失败行，不抛）。队列的重试判据就吃这个返回值（`_process_ship_item`）。
    ⚠️ **丢失窗口（D-0358 改准，不再声称「与改前等同」）**：单笔场景与改前相当（本函数跑在 daemon 线程上，进程重启时在飞的这一笔随进程死）；**批量场景的窗口在队列那头、严格变宽为分钟级**——逐条的口径登记在模块 docstring 「发货节流队列」一节。兜底也不变：页面「重试发货」、D-0147 下再完成一次会再排一次。 ⚠️ **失败留痕不丢**：成功与失败都在 `auto_ship → send_order_shipping` 里**写库**
    （同一份实现、同一个事务，见模块 docstring），不是只写日志。业务失败本来就由 `send_order_shipping` 吞掉（D8）；能逃到本层的只剩「连留痕都写不进去」这类库级故障 ⇒ `logger.exception` 记完整栈。改前它会让那次请求在提交后 500，今天请求已经先返回了，这条日志就是唯一的响亮出口（监控不该失明，同 D8 的补救口径）。 ⚠️ **必须关连接**：后台线程不在「请求结束回收」的覆盖范围内（`CONN_MAX_AGE` 的回收挂在请求信号上），不关就是每发一单漏一条连接。"""
    watchdog = threading.Timer(
        AUTO_SHIP_TIME_BUDGET, _warn_ship_over_budget, args=(order_no,)
    )
    watchdog.daemon = True
    watchdog.start()
    try:
        return run_auto_ship(order_no=order_no, schema_name=schema_name)
    except Exception:  # noqa: BLE001 —— 能逃到这里的都不是业务失败（见 docstring）
        logger.exception("闲管家自动发货线程内部错误 order_no=%s", order_no)
        return None
    finally:
        watchdog.cancel()
        connections.close_all()


def auto_ship(order_no: str) -> TaobaoOrderSendLog | None:
    """发货本体第一步：按单号重取订单再发一次（**业务失败一律不抛异常**）。返回**本次新建**的发货记录行；订单已经不在了 ⇒ 返回那条点名的失败行； `send_order_shipping` 的 D7 幂等跳过 ⇒ `None`（调用方不得把它当重试信号）。
    D-0358 起这个返回值是队列重试判据的输入（经 `run_auto_ship` 透传）。 ⚠️ 传**单号**而不是订单对象：这一步跑在提交之后（今天起由队列 worker 逐条调，经 `run_auto_ship`），拿库里的**当前行**才有意义
    （先例 `jidanpu_hall.attempt_event_now(event_id)`）。 ⚠️ 订单在提交与回调之间变得不可见（软删）⇒ 仍写一条失败并点名， ⛔ 不静默返回：静默会让「订单日志里什么都没发生」看起来像功能没跑。 ⚠️ 与 `send_order_shipping` 同一条边界：**只有**「连留痕都写不进去」这类库级故障会冒泡——由 `_process_ship_item` 的单条兜底接住记完整栈（线程不许亡）。"""
    order = Order.objects.filter(order_no=order_no).first()
    if order is None:
        return _record_failure(
            order_no=order_no, tb_order_no="", store_name="", remark=ORDER_GONE_REASON,
            operator=SYSTEM_OPERATOR,
        )
    if order.split_ship_main_order_no is None and not split_group.is_split_member(order):
        return send_order_shipping(order, operator=SYSTEM_OPERATOR)

    members = list(split_group.active_members(order.split_id)) if order.split_id else [order]
    waiting_members = split_group.shipping_members(members)
    if not waiting_members or any(member.status != OrderStatus.COMPLETED for member in waiting_members):
        return None
    main = split_group.shipping_main_order(members[0])
    if main is None:
        return _record_failure(
            order_no=order_no, tb_order_no="", store_name="", remark=ORDER_GONE_REASON,
            operator=SYSTEM_OPERATOR,
        )
    if _order_from_upstream(main):
        return None
    if _group_shipping_succeeded(main.order_no, [member.order_no for member in members]):
        _write_order_log(
            order_no=order_no, source_order_no=_snapshot_number(main, None),
            result=SendResult.SUCCESS, remark="", operator=SYSTEM_OPERATOR,
        )
        return None
    return send_order_shipping(
        order, operator=SYSTEM_OPERATOR, tb_order_no=_snapshot_number(main, None), routing_order=main,
    )


def retry_row(row, *, operator: str, actor_id=None) -> dict:
    """页面「重试发货」的**一条**（spec §3.7 第二行）：拿**这一行**的快照重发。入参是 `TaobaoOrderSendLog` 的**一行**（由视图按请求里的记录 id 回查得到）—— 重发用的单号取那一行的 `tb_order_no` 快照（spec §3.5），⛔ 不是订单的当前值、 ⛔ 也不是请求里传的值。返回逐条结果（视图直接拼进响应）：
    | 键 | 含义 | |---|---| | `id` | 这一行的记录 id（前端勾选的那个 id，原样回） | | `order_no` / `tb_order_no` | 本次实际用的单号（`tb_order_no` 是快照） | | `result` | `1` 成功 / `0` 失败（与发货记录同一枚举） | | `remark` | 失败原因（成功时空串） |
    | `record_id` | 本次**新建**的记录行 id；D7 幂等跳过（该单已有成功行）时为 `None` | ⚠️ **D7 的「已发货」当成功报**（业务方口径「如果已发货 直接也展示 发货成功」）：那一档既不调上游、也不新增记录行，`result` 报 `1`、`record_id` 报 `None`—— 「它已经是成功的」与「这次又发了一次」由 `record_id` 区分，⛔ 不报失败。 ⚠️ 系统订单已经不在了（软删 / 硬删）⇒ 没有可发的单，仍写一条失败并点名（`ORDER_GONE_REASON`），⛔ 不静默跳过。"""
    order = Order.objects.filter(order_no=row.order_no).first()
    routing_order = split_group.shipping_main_order(order) if order is not None else None
    if routing_order is None:
        new_row = _record_failure(
            order_no=row.order_no, tb_order_no=row.tb_order_no, store_name=row.store_name,
            remark=ORDER_GONE_REASON, operator=operator, actor_id=actor_id,
        )
    else:
        new_row = send_order_shipping(
            order, operator=operator, actor_id=actor_id, tb_order_no=row.tb_order_no,
            routing_order=routing_order,
        )
    if new_row is None:
        return {
            "id": row.pk, "order_no": row.order_no, "tb_order_no": row.tb_order_no,
            "result": int(SendResult.SUCCESS), "remark": "", "record_id": None,
        }
    return {
        "id": row.pk, "order_no": row.order_no, "tb_order_no": new_row.tb_order_no,
        "result": int(new_row.result), "remark": new_row.remark, "record_id": new_row.pk,
    }
