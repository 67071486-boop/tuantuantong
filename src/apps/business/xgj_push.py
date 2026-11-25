"""验签并处理闲管家推送。交易成功记收款，退款或关闭按租户设置处理。
签名用收到的原始请求字节，避免重新序列化改变报文。"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from datetime import datetime, timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal, DecimalException

from django.db import connection, connections, transaction
from django.db.models import F, Func, Q, Value
from django.utils import timezone

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.dailiantong_actions import withdraw_unaccepted_for_platform_refund
from apps.business.dailiantong_push import (
    DEFAULT_PLATFORM,
    TERMINAL_REMOTE_CANCEL_STATUSES,
    TERMINAL_REMOTE_STATUSES,
    DailiantongPushError,
)
from apps.business.models import (
    DailiantongPush,
    Order,
    OrderStatus,
    PaymentStatus,
    XgjReceiptRecord,
    XgjShop,
    XgjShopBinding,
)
from apps.business.order_status import (
    TRANSITIONS,
    IllegalTransition,
    apply_transition,
    settled_status_change_denial,
)
from apps.business.order_writes import WriteContext, change_payment_status, lock_order_row
from apps.business.system_switches import AUTO_REFUND_PLATFORM_ORDER, switch_enabled
from apps.business.xgj import XgjError, order_detail

logger = logging.getLogger(__name__)

#: 日志 / 审计里的操作人（后台同步没有请求身份）。spec §D4 逐字要求 `actor = "系统"`；
#: 同值先例见 `apps/business/xgj_ship.py::SYSTEM_OPERATOR`（同为「系统」这台机器）。
SYSTEM_ACTOR = "系统"

# ── 响应契约（spec §4 的表）────────────────────────────────────────────────
#: ⚠️ 这是**第三方规定**的形状：`{"result": …, "msg": …}`，**不是**本仓统一信封（spec §D8；该例外显式登记在 `tests/test_no_route_500.py` 的 `VENDOR_SHAPED_ROUTES`）。
#: 两句「成功 / 失败」的字面值与四条 `msg` 都在这一处，视图只做搬运。
RESULT_SUCCESS = "success"
RESULT_FAIL = "fail"
MSG_OK = "接收成功"
MSG_MISSING_SIGN = "缺少签名参数"
MSG_SIGN_FAILED = "签名失败"
MSG_BAD_BODY = "报文格式错误"
MSG_PROCESS_FAILED = "处理失败"

#: 触发自动退单的两档 `order_status`（spec §2.1：23 已退款 / 24 已关闭；业务方 R4）。
#: ⚠️ 值一律是**字符串**：请求体里这个键可能是 JSON 数字，收口在视图的序列化器里
#: （`CharField` 会把 `23` 转成 `"23"`，⛔ 不在本模块按 `int` / `str` 两种形状各判一次）。
REFUND_PUSH_STATUSES = frozenset({"23", "24"})

#: 「交易成功」那一档 `order_status`（收款明细线 spec §5 第 1 步；文档依据 = 厂商《订单推送通知》：
#: `22` = 已完成，即买家侧「交易成功」）。挂载点 = **验签通过后、23/24 过滤之前**（挂载点口径）。
RECEIPT_PUSH_STATUS = "22"

#: 上面两档 → 订单日志里那句人话的原因（spec §5 第 6.c 步的 `reason="退款"|"关闭"`）。
PUSH_STATUS_REASONS = {"23": "退款", "24": "关闭"}

#: 两个**新动作码**（spec §D5 第 ② 层 / §D6）。登记表在 `apps/audit/actions.py`。
REFUND_ACTION = "order.xgj_push_refund"
SETTLED_ACTION = "order.xgj_push_refund_settled"

#: 收款分支的**新动作码**（收款明细线 spec §4-D3）：机器把收款状态改成「已结款」那条
#: 带原因的人话，独立动作码 ⇒ 系统操作日志可单筛（与 `REFUND_ACTION` 同一形状）。
RECEIPT_ACTION = "order.xgj_push_receipt"

#: 匹配前要从**库里那一列**上削掉的首尾空白 —— **与 Python `str.strip()` 恰好同一集合**。 spec §D3 的口径是「首尾空白**不算差异**」：推送来的值由 `handle_order_push` 里的 `(order_no or "").strip()` 削（= Python `str.strip()` 的全集），而 `Order.source_order_no` 是客服录单时**手填的自由文本**（先例 `xgj_ship._snapshot_number` 也是先把这一列 strip 再发上游） ⇒ 库里那一位也得削、而且**必须削同一套字符**：本集合少一个字符，就是「库里有单却匹配不到」，
#: 而它**不报错、不写 warning**、接口照回 `success`——**静默少退单**（F1 的起因）。集合的出处 = CPython 里 `str.isspace()` 为真的**全部 29 个码位**（`str.strip()` 不带参数时用的是同一个判定 `Py_UNICODE_ISSPACE`）。逐字枚举如下，⛔ 改动前先读下面那条机械判据： - ASCII 空白 5 个：`\t` U+0009 · `\n` U+000A · `\v` U+000B · `\f` U+000C · `\r` U+000D - 普通空格 1 个：U+0020 - 文件 / 组 / 记录 / 单元分隔符 4 个：U+001C · U+001D · U+001E · U+001F
#: - 拉丁 1 号补充 2 个：U+0085（NEL）· U+00A0（不换行空格） - 欧甘文空格 1 个：U+1680 - 通用标点 11 个：U+2000–U+200A（含 en / em 空格、窄空格等） - 行 / 段分隔符 2 个：U+2028 · U+2029 - 窄不换行空格 1 个：U+202F - 中数学空格 1 个：U+205F - 表意文字空格 1 个：U+3000（**全角空格**，中文界面复制粘贴最常见的那一个） ⚠️ **机械判据**（写注释与写集合这两处不许各自漂移）在 `tests/test_xgj_push_inbound.py` 的 `test_the_trim_character_set_is_exactly_python_str_strip`：它当场用 `{chr(cp) for cp in range(0x11000) if chr(cp).isspace()}` 算期望值，断言两边**恰好**相等。 ⚠️ 本行上面的原句「= `str.strip()` 认的那一组字符」在 F1 之前**不成立**（当时只有头 6 个 ASCII 空白），被那条用例当场证伪；改正后这一行**由那条用例钉住**，⛔ 改它之前先读那条用例。
_TRIM_CHARS = (
    "\t\n\x0b\x0c\r"                        # U+0009 · U+000A · U+000B · U+000C · U+000D
    "\x1c\x1d\x1e\x1f"                      # U+001C · U+001D · U+001E · U+001F
    " "                                     # U+0020
    "\x85\xa0"                              # U+0085 · U+00A0
    "\u1680"                                # U+1680
    "\u2000\u2001\u2002\u2003\u2004\u2005"  # U+2000–U+2005
    "\u2006\u2007\u2008\u2009\u200a"        # U+2006–U+200A
    "\u2028\u2029"                          # U+2028 · U+2029
    "\u202f\u205f"                          # U+202F · U+205F
    "\u3000"                                # U+3000
)


def body_md5(raw_body: bytes) -> str:
    """`bodyMd5 = md5(<POST 原文字符串>)`——对**收到的原始字节**求 MD5（spec §2.2）。"""
    return hashlib.md5(raw_body).hexdigest()


def sign_for(*, app_key: str, body_md5_hex: str, timestamp: str, app_secret: str) -> str:
    """`sign = md5("{appKey},{bodyMd5},{timestamp},{appSecret}")`（spec §2.2 逐字）。"""
    raw = f"{app_key},{body_md5_hex},{timestamp},{app_secret}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def verify_sign(*, app_key: str, app_secret: str, raw_body: bytes, timestamp: str,
                given: str) -> bool:
    """核对推送方的 `sign`；不匹配回 `False`（调用方一律回 `fail 签名失败`）。三点口径（spec §5 第 4 步）： - **常量时间比较**（`hmac.compare_digest`）：这是个公开面，比较时间不该泄漏前缀信息；
    - **大小写归一**：两侧都 `.lower()`——十六进制摘要的大小写不是语义差异； - ⚠️ 比较对象是 **bytes** 而不是 `str`：`compare_digest` 对**含非 ASCII 的 `str`**
    会抛 `TypeError`（框架事实），而 `sign` 是请求来的值 ⇒ 拿 `str` 比会把一个畸形参数变成未预期异常。两侧各自编码后比较，行为是**恒定的 False**。"""
    computed = sign_for(
        app_key=app_key, body_md5_hex=body_md5(raw_body), timestamp=timestamp,
        app_secret=app_secret,
    )
    return hmac.compare_digest(
        computed.encode("ascii"), str(given or "").strip().lower().encode("utf-8")
    )


def _credentials_of(binding: XgjShopBinding):
    """取一条绑定的上游凭据（不完整回 `None`）。判据的唯一出处是 `apps/business/api/xgj_views.py::credentials_of`（`strip()` + 非空）。
    ⚠️ **函数内 import**：本模块是领域层、那个判据当初落在了 api 层（同款先例与理由见 `xgj_ship._credentials_of`）⇒ 用函数内 import 复用它，
    ⛔ 不在这里抄一份 `strip` 判据（同一事实两套判据必然漂移，E-0021）。"""
    from apps.business.api.xgj_views import credentials_of

    return credentials_of(binding)


def _binding_for(appid: str) -> XgjShopBinding | None:
    """本租户里「归一化后的 `app_key` == 归一化后的 `appid`」的第一行（spec §5 第 3 步）。 ⚠️ **归一化与出站同一套**（F5）：判据的唯一出处是 `apps/business/api/xgj_views.py::credentials_of` 的 `(binding.app_key or "").strip()` —— 租户的 AppKey 是**复制粘贴**进来的，库里那一列带首尾空格 / 换行很常见（`credentials_of`
    的 docstring 明说这一步是为「历史数据 / 直接从 shell 写库」兜底）。入站若按字面 `filter` 原样比，同一份绑定会「出站能用、入站持续 `签名失败`」，而那个症状与「验签公式抄错」 **一模一样**（spec §6-1 登记的症状正是持续「签名失败」）⇒ 会把排障引向错方向。 ⚠️ **为什么不把 `strip` 下推到库侧、走索引**：一是 `app_key` **没有唯一约束** （`models.py` 的 `XgjShopBinding`），二是本表每个租户只有几行（一个自研应用一行）
    ⇒ Python 侧遍历的代价可以接受；而在库里 `BTRIM` 等于把 `credentials_of` 那份判据再抄一份，两份判据必然漂移（E-0021）。 ⚠️ `.order_by("id")` 是**判据的一部分**：`app_key` 没有唯一约束 ⇒ 「同 key 多行」时必须定死取 `id` 最小的那一行（否则次序不保证，同一笔请求可能换一行凭据）。 ⚠️ 租户上下文只来自 schema（域名 / 连接），⛔ 不接受任何请求参数传入的租户标识（`AGENTS.md` §4）——本查询的 queryset 是当前 schema 的默认 manager。"""
    wanted = (appid or "").strip()
    for binding in XgjShopBinding.objects.order_by("id"):
        if (binding.app_key or "").strip() == wanted:
            return binding
    return None


def _binding_signature_ok(*, appid: str, raw_body: bytes, timestamp: str, sign: str) -> bool:
    """「本租户里按 `appid` 找到的那条绑定的凭据，能验过这次签名吗」。顺序照 spec §5：先找绑定（3）再核签名（4）——**两条不通过回同一句 `msg`**（§4 的表），
    所以「appid 不存在」与「sign 不对」对外不可分辨（不给拿租户域名扫 appid 的人一个反馈面）。
    ⚠️ 凭据不完整（`app_secret` 为空等）走的是同一支：没有密钥就无从验签 ⇒ `fail 签名失败`。"""
    binding = _binding_for(appid)
    if binding is None:
        return False
    credentials = _credentials_of(binding)
    if credentials is None:
        return False
    app_key, app_secret = credentials
    return verify_sign(
        app_key=app_key, app_secret=app_secret, raw_body=raw_body,
        timestamp=timestamp, given=sign,
    )


def _status_label(value) -> str:
    """状态码 → 中文标签（审计的 `from_value` / `to_value` 与 `/transition/` 同口径）。
    认不出的码**原样返回**（同 `order_writes._status_text` / `jidanpu_converge._status_label`
    的口径：读的是同一份 `OrderStatus.choices`，⛔ 不新造映射表）。"""
    return dict(OrderStatus.choices).get(str(value), str(value))


def _nodes_for(current, target) -> frozenset:
    """这次后台迁移「持有的节点集」= **这条边自己的节点集**（spec §D4）。 ⚠️ 这里**不是**用户授权：闲管家推送是后台同步（没有请求身份、没有登录用户）⇒
    取边表里这条边的节点集，语义是「**这条边存在**就允许系统去走它」。先例与逐字理由见 `jidanpu_converge._nodes_for`（⛔ 不把某个具体角色的节点搬到后台）。"""
    return frozenset(TRANSITIONS.get((str(current), str(target)), frozenset()))


def _matching_orders(order_no: str):
    """按 `source_order_no` 找**全部**命中行（spec §D3：不唯一、全部都要退），按 `id` 升序。 ⚠️ **比较在库侧做 `BTRIM`**（削掉 `_TRIM_CHARS` 那组首尾空白）而不是 `filter(列=值)`： §D3 的口径是「首尾空白不算差异」，而空白可能在**库里那一列**上（手填的自由文本）。 **代价（如实登记）**：带注解的过滤用不上 `source_order_no` 的索引（顺序扫描）——
    换来的是「明明有单却匹配不到」不再发生；本端点只在退款 / 关闭推送时被调一次。 ⚠️ `Order.objects` 是**默认 manager** ⇒ 已软删的单不在命中集合里（D-0144）：那类单改不了状态（唯一写入口的加锁读也走默认 manager），列进来只会白跑一趟。 ⚠️ **排除隐藏单**（`hall_card` / `upstream_hall_card` 非空的两族派生单，照 `xgj_receipt_sync.candidates_qs` 同款两句；登记 `HALL_SHADOW_EXCLUDED_SITES`）：
    隐藏单的 `source_order_no` 是**另一个系统的内部单号**（peer 卡 = 我方大厅单、上家镜像卡 = 上家鸡蛋铺内部单号，`upstream_hall_shadow._projection_fields`），与闲管家推送携带的**闲管家单号**本属两个 ID 空间；真撞号时 23/24 会把隐藏单翻成 `REFUNDED`——而隐藏单生命周期的唯一收口人（`upstream_hall_shadow.refund_for_card`）没被调 ⇒ 已抛给下家的副本不撤（M-01，第五轮审计）。22 收款分支同理：不排除就是给「不是本租户生意」的载体单记一笔假收款。两族都非空的单（数据被手改）两句话各挡各的。"""
    trimmed = Func(F("source_order_no"), Value(_TRIM_CHARS), function="BTRIM")
    return (
        Order.objects.annotate(_push_key=trimmed)
        .filter(_push_key=order_no)
        .exclude(hall_card__isnull=False)
        .exclude(upstream_hall_card__isnull=False)
        .order_by("id")
    )


def _write_transition_log(order, from_status) -> None:
    """状态事实那一条（spec §D5 第 ① 层）：**复用**既有动作码 `order.transition`。
    与 `/transition/`、`jidanpu_converge._run_actions` 同款列（`field_name="status"` +
    中文前后值），⛔ 不为这条路径另造动作码。"""
    record(
        log_type=ACTION_LOG_TYPES["order.transition"],
        action="order.transition",
        actor=SYSTEM_ACTOR,
        # ⭐ 闲管家推送驱动的**机器**来源 ⇒ 显式声明（见 `log.py`）。
        system=True,
        target_kind="order",
        target_id=order.order_no,
        order_no=order.order_no,
        field_name="status",
        from_value=_status_label(from_status),
        to_value=_status_label(order.status),
    )


def _write_refund_log(order, reason: str) -> None:
    """业务方 R5 要的那条「带原因的已退单」人话（spec §D5 第 ② 层）。"""
    record(
        log_type=ACTION_LOG_TYPES[REFUND_ACTION],
        action=REFUND_ACTION,
        actor=SYSTEM_ACTOR,
        # ⭐ 机器来源 ⇒ 显式声明。
        system=True,
        target_kind="order",
        target_id=order.order_no,
        order_no=order.order_no,
        reason=reason,
    )


def _write_settled_log(order, reason: str) -> None:
    """已结账那一档的**独立动作码**日志（spec §D6 / R9：单独显示、留给人工）。
    ⚠️ 这一档**只写日志**：状态一个字都不改（业务方 R7）。"""
    record(
        log_type=ACTION_LOG_TYPES[SETTLED_ACTION],
        action=SETTLED_ACTION,
        actor=SYSTEM_ACTOR,
        # ⭐ 机器来源 ⇒ 显式声明。
        system=True,
        target_kind="order",
        target_id=order.order_no,
        order_no=order.order_no,
        reason=reason,
    )


def _refund_one(order, reason: str) -> None:
    """把一张单改成「已退单」并写两条日志；`IllegalTransition` 由调用方接住（跳过）。 ⚠️ **调用方必须已经开好 `transaction.atomic()`**：唯一写入口的状态复读用
    `select_for_update`，autocommit 下会响亮抛 `TransactionManagementError`（spec §5 的 ⚠️）。
    ⚠️ 本函数块内 ⛔ **没有 `return`**（E-0022：`atomic()` 是事务不是回滚点）。"""
    from_status = order.status
    apply_transition(
        order, OrderStatus.REFUNDED,
        nodes=_nodes_for(from_status, OrderStatus.REFUNDED), actor=SYSTEM_ACTOR,
        notice_reason=reason,
    )
    _write_transition_log(order, from_status)
    _write_refund_log(order, reason)


@transaction.atomic
def _route_refund(order, reason: str) -> None:
    from apps.business.approvals import create_refund_approval, has_refund_approval

    # 同一张单的退款重推、代练通发单占位都拿这把锁。
    locked = lock_order_row(order)
    if (
        locked is None
        or locked.source_order_no.strip() != order.source_order_no.strip()
        or locked.status == OrderStatus.REFUNDED
        or has_refund_approval(locked)
    ):
        return
    order = locked
    # 重发时，旧推送可在远端撤销结束前被本地标为已关闭。
    closed_needs_review = Q(state=DailiantongPush.State.CLOSED) & (
        Q(remote_status__in=TERMINAL_REMOTE_STATUSES)
        | (
            Q(cancel_requested_at__isnull=False)
            & ~Q(remote_cancel_status__in=TERMINAL_REMOTE_CANCEL_STATUSES)
        )
    )
    pushes = list(
        DailiantongPush.objects.filter(order=order, platform=DEFAULT_PLATFORM)
        .filter(Q(state__in=DailiantongPush.ACTIVE_STATES) | closed_needs_review)
        .order_by("-id")[:2]
    )

    def send_for_review(details: str) -> None:
        create_refund_approval(order, reason=f"平台订单{reason}，{details}")

    settled_denial = settled_status_change_denial(order, OrderStatus.REFUNDED)
    if settled_denial is not None:
        _write_settled_log(order, reason)
        if pushes:
            send_for_review(settled_denial)
        return
    if len(pushes) > 1:
        send_for_review("这张单有多条仍需核对的代练通推送，请确认各笔处理结果")
        return
    if pushes:
        if pushes[0].state == DailiantongPush.State.CLOSED:
            details = (
                "代练通订单已结算，请人工核对"
                if pushes[0].remote_status in TERMINAL_REMOTE_STATUSES
                else "代练通旧推送未确认撤销完成，请人工核对"
            )
            send_for_review(details)
            return
        try:
            withdraw_unaccepted_for_platform_refund(pushes[0])
        except DailiantongPushError as exc:
            send_for_review(str(exc))
            return

    try:
        # 本地改状态被拒时，仍保留已核实的外部撤回结果。
        with transaction.atomic():
            _refund_one(order, reason)
    except IllegalTransition as exc:
        if pushes:
            send_for_review(f"代练通已撤回，团团通退单受阻（{exc}），请人工处理")
        logger.warning("闲管家推送：订单 %s 跳过（%s）", order.order_no, exc)


# ── 收款分支（order_status = 22；收款明细线 spec §5 第 1 步）──────────────────

#: 金额回查的总时长预算（秒）：超了只 `logger.warning` 留痕（看门狗**杀不掉**堵住的线程，
#: 与 `xgj_ship.AUTO_SHIP_TIME_BUDGET` 同一条如实登记的边界）。
RECEIPT_FETCH_TIME_BUDGET = 30.0

#: `change_payment_status` 的**机器**上下文（先例 = `apps/openapi/api/order_write_views.py:266`
#: 的 `_write_context`：`log_actor_id=None` ⇒ 留痕落**系统行**，⛔ 本模块不往 `record()` 传
#: `system=`——那是 `order_writes` 的事）。`change_payment_status` 不读 `nodes` （那道闸在视图层），`frozenset()` 即「机器路径没有权限节点」的既有口径。
_MACHINE_CTX = WriteContext(
    actor=SYSTEM_ACTOR, log_actor=SYSTEM_ACTOR, log_actor_id=None, nodes=frozenset()
)


def _write_receipt_log(order) -> None:
    """收款分支那条「带原因的人话」（spec §4-D3 的独立动作码；与 `_write_refund_log` 同形）。
    ⚠️ `change_payment_status` 在值真变时**自己**还会写一条 `order.change_payment_status`
    ⇒ 一次推送一张单 = 两行日志（与退款线「transition+refund」两行同形，评审已核非事故）。"""
    record(
        log_type=ACTION_LOG_TYPES[RECEIPT_ACTION],
        action=RECEIPT_ACTION,
        actor=SYSTEM_ACTOR,
        # 机器来源 ⇒ 显式声明（同 `_write_refund_log`）。
        system=True,
        target_kind="order",
        target_id=order.order_no,
        order_no=order.order_no,
        reason="交易成功",
    )


def _success_time_from_modify(raw) -> datetime | None:
    """推送的 `modify_time`（秒级时间戳）→ **aware UTC** 的 `datetime`；认不出 ⇒ `None`。 ⚠️ 数值判据复用 `xgj_shop._xgj_seconds`（**唯一出处**，函数内 import 的同款先例见
    本模块 `_credentials_of`；⛔ 不在这里抄第二份「int 转换 + 负数拒收」的判据）。 ⚠️ 必须转 **aware UTC** 再塞 `DateTimeField`（`USE_TZ=True`）：naive 值会按默认时区
    解释、统计桶漂 8 小时（`apps/openapi/rate_limit.py:141-146` 的专门警告）。统计桶的「本地日」换算在读层做（`timezone.localtime(...)`，spec §4-D6）。"""
    from apps.business.xgj_shop import _xgj_seconds

    seconds = _xgj_seconds(raw)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=dt_timezone.utc)
    except (OverflowError, OSError, ValueError):
        # 超出平台表示范围的秒数（文档标 int32，但报文是别人发的）：不猜，按「没给」处理。
        return None


def _snapshot_shop_name(seller_id: str) -> str:
    """记账时能拿到的**辅助**店名：`seller_id` 与 `XgjShop.user_identity` 同构（spec §2.2
    的证据等级 = 读文档 + 形状推断，已降为辅助）⇒ 命中就用店名快照，命中不到留空串。**主键**解析在回查线程里（响应的 `seller_eid`，见
    `fetch_receipt_amount`），两半都失败还有这一列的旧快照 / 原始 `seller_id` 兜底。"""
    if not seller_id:
        return ""
    shop = XgjShop.objects.filter(user_identity=seller_id).only("shop_name").first()
    return shop.shop_name if shop is not None else ""


def _parse_fen_amount(raw) -> Decimal | None:
    """详情订单对象里**以「分」计**的金额键（`pay_amount` / `refund_amount`）→ `Decimal`；认不出 ⇒ `None`。
    ⚠️ 上游没承诺这些键的类型（官方文档没写、2026-10-22 生产探针实测两型都有）⇒ 字符串 / 数字都收，转不成 / 非有限值一律 `None`（缺失是**可见**的，猜一个数才是事故）。
    ⚠️ 本函数**不量化**——量化在 `_detail_amount` 的除法之后做一次（先除后量化， ⛔ 不对分值先量化再除，那会把 `999.5` 分错误地板成 999 分）。"""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw).strip())
    except (DecimalException, ValueError):
        return None
    if not value.is_finite():
        return None
    return value


def _detail_amount(detail: dict) -> Decimal | None:
    """详情订单对象 → 实收金额 = **(买家实付 − 已退) / 100**，元、两位小数（spec §12 F1-D）。 - **口径依据**（2026-10-22 生产探针实锤，F-1）：`xyb_seller_amount` 对普通单**恒为 0** （首日 36 行金额全是 0.00 的根因）；真正的钱 = `pay_amount`（买家实付，分）
    减 `refund_amount`（已退，分）= 卖家实际到手。⚠️ 待业务方对一单核实（探针单后台应显 ¥10.00；若显 ¥5 则改 `total_amount`，只动本函数一处）。 - `refund_amount` 缺失 / 认不出 ⇒ 按 **0**（没退就是 0，不是缺失）； - `pay_amount` 缺失 / 认不出 ⇒ **`None`**（金额缺失，⛔ 不猜 0）；
    - 负差（已退 > 实付）**照存**（本仓金额可为负，D-0015 口径）。 - 量化用 **`ROUND_HALF_UP`**（本仓金额口径的唯一先例： `settlement_amount.py` / `performance_amount.py`——与 PostgreSQL `numeric` 舍入一致）， ⛔ 不用 `Decimal` 默认的 banker's rounding。"""
    pay = _parse_fen_amount(detail.get("pay_amount"))
    if pay is None:
        return None
    refund = _parse_fen_amount(detail.get("refund_amount"))
    if refund is None:
        refund = Decimal("0")
    return ((pay - refund) / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _handle_receipt_push(*, appid: str, raw_body: bytes, order_no: str, seller_id: str,
                         user_name: str, modify_time: str) -> tuple[str, str]:
    """22 分支：**记账一行**（去重键 `app_key+order_no`）→ 找单改收款状态 → 提交后回查金额。流程 = 收款明细线 spec §5 第 1.a–1.e 步。**应答始终尽快 `success`**（厂商 3 秒超时 / 3 次重试）；金额回查在 `on_commit` 之后的 daemon 线程做，⛔ 不堵应答。
    - `order_no` 为空 ⇒ 照旧 ack、零写入（与 23/24 的空值闸同形；空串会命中「来源单号为空」的行，那不是这一笔推送指的单）。 - 绑定与凭据由本分支**自己**取（`_binding_signature_ok` 只回 bool、把 binding 丢了，⛔ 为复用去改 23/24 共用的函数；租户内几行的 Python 遍历，二次查代价可忽略）。
    - 匹配复用 `_matching_orders`（与退款同一套 BTRIM 归一）；命中多张 ⇒ 全部处理（§4-D5）。 - **每一张单各自 `atomic()`**：一张拒不连坐。预期内的跳过（已 SETTLED / 已 REFUNDED）连 save 都免；`change_payment_status` 没有领域级拒绝码（状态值恒合法），真炸的只可能是未预期异常 ⇒ 不接，让它响亮地走到视图的 fail 兜底（E-0022）。"""
    key = (order_no or "").strip()
    if not key:
        return RESULT_SUCCESS, MSG_OK
    binding = _binding_for(appid.strip())
    app_key = ""
    if binding is not None:
        credentials = _credentials_of(binding)
        if credentials is not None:
            app_key = credentials[0]
    try:
        raw_push = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raw_push = {}
    if not isinstance(raw_push, dict):
        raw_push = {}
    seller_id_text = str(seller_id or "").strip()
    # 记账一行（§5 第 1.c 步）：自己的 atomic——它与逐单改状态互不连坐。
    with transaction.atomic():
        defaults = {
            "binding": binding,
            "seller_id": seller_id_text,
            "user_name": str(user_name or "").strip(),
            "shop_name_snapshot": _snapshot_shop_name(seller_id_text),
            # 重复推送 = 重算本轮匹配结果（先置 0，循环后回填），⛔ 不累计旧值。
            "matched_count": 0,
            "match_result": [],
            "raw_push": raw_push,
        }
        # ⚠️ **交易成功时间只在「这次解析得出来」时才写**：重推报文的 `modify_time` 认不出 ⇒ `None` 进 defaults 会把**已存**的 `success_time`
        # 覆盖掉，那一行从此掉出**所有**统计窗口（钱记了账却查不到）；留旧值比清成空安全。首次创建时这个键缺席 ⇒ 列取模型默认 `NULL`，行为不变。
        new_success_time = _success_time_from_modify(modify_time)
        if new_success_time is not None:
            defaults["success_time"] = new_success_time
        record_row, _ = XgjReceiptRecord.objects.update_or_create(
            # ⚠️ 唯一键是 **app_key 快照 + order_no**：app_key 跨「删绑定再重建」
            #    稳定 ⇒ 重建后同单重推命中的仍是**旧行**（更新、不双算）。
            app_key=app_key,
            order_no=key,
            defaults=defaults,
        )
    results: list[dict] = []
    for order in _matching_orders(key):
        if str(order.payment_status) == str(PaymentStatus.SETTLED):
            # 幂等（§3-D）：已是「已结款」⇒ 跳过（连 save 都免）。
            results.append({"order_no": order.order_no, "result": "settled_skip"})
            continue
        if str(order.status) == str(OrderStatus.REFUNDED):
            # 已退单 ⇒ 跳过（§3-D：退款跟退由既有的 23/24 线负责，两条线互不掺和）。
            results.append({"order_no": order.order_no, "result": "refunded_skip"})
            continue
        # ⚠️ **每张单各自 `atomic()`**（§5 第 1.d 步）：一张拒不连坐。`change_payment_status`
        #    没有领域级拒绝码（收款三档恒合法）⇒ 这里不接异常，真炸的只可能是未预期异常，
        #    让它响亮地走到视图的 fail 兜底（E-0022；已改过的前几张单已各自提交，不回滚）。
        with transaction.atomic():
            change_payment_status(
                order, payment_status=PaymentStatus.SETTLED,
                ctx=_MACHINE_CTX, dry_run=False,
            )
            _write_receipt_log(order)
        results.append({"order_no": order.order_no, "result": "updated"})
    with transaction.atomic():
        record_row.matched_count = len(results)
        record_row.match_result = results
        record_row.save(update_fields=["matched_count", "match_result", "updated_at"])
    # 回查金额排在**最外层事务提交之后**（D4）。视图不包事务（autocommit）⇒ 此刻已在
    # 事务外，`on_commit` 立即排程；万一将来调用方包了事务，它也会等提交后才跑。
    transaction.on_commit(lambda: dispatch_receipt_amount_fetch(record_row.pk))
    return RESULT_SUCCESS, MSG_OK


def dispatch_receipt_amount_fetch(record_id: int) -> None:
    """把「回查一行收款记录的金额」排到**事务提交之后**的 daemon 线程（spec §4-D4）。 ⚠️ **逐行照抄先例 `xgj_ship.dispatch_auto_ship`**：schema 是**连接级**状态
    （django-tenants），新线程拿到的是**另一条**新连接，没有请求线程设过的租户 schema ⇒ 把 `connection.schema_name` 当**普通值**随参数带过去（⛔ 不能在线程里再查），
    线程体落 schema 后才碰库。每条推送一个短命线程，上游慢 / 挂都不占请求响应时间。"""
    schema_name = connection.schema_name
    threading.Thread(
        target=_receipt_amount_in_thread,
        kwargs={"record_id": record_id, "schema_name": schema_name},
        name=f"xgj-receipt-amount-{record_id}",
        daemon=True,
    ).start()


def _warn_receipt_fetch_over_budget(record_id: int) -> None:
    """看门狗到点：回查超过 `RECEIPT_FETCH_TIME_BUDGET` 还没结束 ⇒ 只记 warning 留痕
    （⛔ 杀不掉堵住的线程；价值是让「疑似上游卡死」在服务器日志里**可见**，
    同 `xgj_ship._warn_ship_over_budget` 的口径）。"""
    logger.warning(
        "闲管家收款金额回查超过 %.0f 秒仍未结束（疑似上游卡死）record_id=%s",
        RECEIPT_FETCH_TIME_BUDGET,
        record_id,
    )


def run_receipt_amount_fetch(*, record_id: int, schema_name: str | None) -> None:
    """回查本体（**可直调缝隙**）：按需落 schema 后复用 `fetch_receipt_amount`。 - `schema_name` 非空 ⇒ `connection.set_schema(...)`（只设**连接级**状态，
    真正的 `SET search_path` 在下一个游标上才发生）；落的就是请求线程当时那个租户 schema。 - `schema_name=None` ⇒ 不动 schema（测试线程**同步直调**时就是这一档）。"""
    if schema_name:
        connection.set_schema(schema_name)
    fetch_receipt_amount(record_id)


def _receipt_amount_in_thread(*, record_id: int, schema_name: str) -> None:
    """daemon 线程体：看门狗 → 回查本体 → 关掉这条线程自己开的数据库连接。 ⚠️ **逐行照抄先例 `xgj_ship._auto_ship_in_thread`**（368-447 行那套模式）：
    看门狗 Timer、`finally` 里 `watchdog.cancel()` + `connections.close_all()`
    （后台线程不在「请求结束回收」的覆盖范围内，不关就是每条推送漏一条连接）；能逃到本层的只剩「连警告都写不进去」这类故障 ⇒ `logger.exception` 记完整栈。"""
    watchdog = threading.Timer(
        RECEIPT_FETCH_TIME_BUDGET, _warn_receipt_fetch_over_budget, args=(record_id,)
    )
    watchdog.daemon = True
    watchdog.start()
    try:
        run_receipt_amount_fetch(record_id=record_id, schema_name=schema_name)
    except Exception:  # noqa: BLE001 —— 能逃到这里的都不是业务失败（见 docstring）
        logger.exception("闲管家收款金额回查线程内部错误 record_id=%s", record_id)
    finally:
        watchdog.cancel()
        connections.close_all()


def fetch_receipt_amount(record_id: int) -> bool:
    """回查**一行**收款记录的实收金额（同步、可直调；线程体与管理命令共用）。步骤（spec §5 第 1.e 步 + §12 F1-D / F2-D）： 1. 重取那一行（提交后跑，拿库里的**当前行**才有意义；行没了 ⇒ 直接返回）； 2. 绑定还在且凭据完整才打得出站 `order/detail`（`SET_NULL` ⇒ 删绑定后回查不了，
    那一行留空金额等手工 / 重建绑定后再补查）； 3. 金额 = `(pay_amount − refund_amount) / 100`（F1-D；`pay_amount` 认不出 ⇒ 金额留空 = **缺失**，⛔ 不猜 0）+ `amount_fetched_at`； 4. **店铺标识与店名解析主键** = 响应的 `seller_eid` → `XgjShop.user_identity`
    （生产探针 F-2：与店铺列表同源、真命中）：`seller_eid` 非空才写 `shop_identity`（统计分组键，spec §12 F2-D），命中 `XgjShop` 才刷店名快照； 5. 上游失败（`XgjError`）**不抛**：留空金额 + `logger.warning` 留上游码（不回显上游原文——它可能回显凭据，同 `xgj_shop.source_for_order` 的口径），管理命令 `backfill_xgj_receipt_amounts` 可重试。回 `True` = 这次真的写进了金额。"""
    record_row = XgjReceiptRecord.objects.filter(pk=record_id).first()
    if record_row is None:
        return False
    binding = record_row.binding
    if binding is None:
        logger.warning(
            "闲管家收款金额回查：绑定已删、取不到密钥（等重建绑定后补查）record_id=%s",
            record_row.pk,
        )
        return False
    credentials = _credentials_of(binding)
    if credentials is None:
        logger.warning(
            "闲管家收款金额回查：绑定凭据不完整 record_id=%s binding=%s",
            record_row.pk, binding.pk,
        )
        return False
    app_key, app_secret = credentials
    try:
        detail = order_detail(
            record_row.order_no, app_key=app_key, app_secret=app_secret,
        )
    except XgjError as exc:
        logger.warning(
            "闲管家收款金额回查失败（留空金额，可用 backfill_xgj_receipt_amounts 重试）"
            "record_id=%s order_no=%s upstream_code=%s",
            record_row.pk, record_row.order_no, exc.upstream_code,
        )
        return False
    amount = _detail_amount(detail)
    updates: list[str] = []
    if amount is not None:
        record_row.amount = amount
        record_row.amount_fetched_at = timezone.now()
        updates += ["amount", "amount_fetched_at"]
    identity = str(detail.get("seller_eid") or "").strip()
    if identity:
        # ⚠️ `seller_eid` 非空就写 `shop_identity`（F2-D：分组键；金额认不出也要写——
        #    店铺归属与金额是两件独立的事，别让金额缺失把店铺归属一起吞掉）。
        record_row.shop_identity = identity
        updates += ["shop_identity"]
        shop = XgjShop.objects.filter(user_identity=identity).only("shop_name").first()
        if shop is not None and shop.shop_name:
            record_row.shop_name_snapshot = shop.shop_name
            updates += ["shop_name_snapshot"]
    if updates:
        record_row.save(update_fields=sorted(set(updates + ["updated_at"])))
    return amount is not None


def handle_order_push(*, raw_body: bytes, appid: str, timestamp: str, sign: str,
                      order_no: str = "", order_status: str = "",
                      seller_id: str = "", user_name: str = "",
                      modify_time: str = "") -> tuple[str, str]:
    """验签后，交易成功走收款；已退款或已关闭按开关逐张处理匹配的订单。"""
    if not _binding_signature_ok(
        appid=appid.strip(), raw_body=raw_body, timestamp=timestamp, sign=sign
    ):
        return RESULT_FAIL, MSG_SIGN_FAILED
    status = str(order_status)
    if status == RECEIPT_PUSH_STATUS:
        return _handle_receipt_push(
            appid=appid.strip(), raw_body=raw_body, order_no=order_no,
            seller_id=seller_id, user_name=user_name, modify_time=modify_time,
        )
    if status not in REFUND_PUSH_STATUSES or not switch_enabled(AUTO_REFUND_PLATFORM_ORDER):
        return RESULT_SUCCESS, MSG_OK
    reason = PUSH_STATUS_REASONS[status]
    key = (order_no or "").strip()
    if not key:
        return RESULT_SUCCESS, MSG_OK
    for order in _matching_orders(key):
        _route_refund(order, reason)
    return RESULT_SUCCESS, MSG_OK


def handle_product_push(*, raw_body: bytes, appid: str, timestamp: str,
                        sign: str) -> tuple[str, str]:
    """商品推送：**只验签 + ack**，一个字节都不写（spec §D9 / 业务方 R2 / R3）。 ⚠️ 本函数**不解析** body（它只参与签名）：解析一个不会产生任何业务后果的报文
    只会多一条能 500 的路径。⚠️ 它也不写操作日志——端点登记在 `tests/test_operation_log_ledger.py` 的 `DELIBERATELY_NOT_LOGGED`。"""
    if not _binding_signature_ok(
        appid=appid.strip(), raw_body=raw_body, timestamp=timestamp, sign=sign
    ):
        return RESULT_FAIL, MSG_SIGN_FAILED
    return RESULT_SUCCESS, MSG_OK
