"""代练通**平台动作**（T10b）：撤回 / 改价 / 补时 / 留言。本模块回答「这张已经抛到代练通的单，能不能再做某个平台动作、做完怎么算数」；协议层在 `dailiantong_client.py`（签名/信封/重试红线），抛单与回读在 `dailiantong_push.py`（配置 → 客户端 → 共享的 `DailiantongPush` 行），本模块**不重写**它们（客户端从 `dailiantong_push._open_client` 的同一个构造点来，凭据读取只在那里）。**四个动作的真机事实**（`docs/DAILIANTONG-API.md` §6.2/§9，照做别推翻）：撤回未接手=`LevelOrderDelSelf`（前提 `Status=11`；判据=详情**查不到** ⇒ 本地行 `state=closed`）；撤回已接手=`LevelOrderCancel(flag=0)`（前提 `Status=12/13/14/15`——14 已真机验证、15 按业务方口径 §6.11——且 `CancelStatus != 11`；判据=`CancelStatus != 10` 申请已登记）；改价=`LevelOrderModify_Price`（**只有** `Status=11`，实测 `-1 只有未接手状态才能修改价格`；判据=详情 `Price` == 新价）；补时=`LevelOrderModify_Special(kind=1)`（无状态前提，被接手后也能补；判据=详情 `TimeLimit` == 原值+小时数）；留言=`LevelOrderMessageAdd`（无；上游成功码即可——留言回调有延迟，⛔ 不强求回读）。
口径：**未接手**可直接删除并**即时退款**；一旦被接手（12/13）或处于异常/锁定（14/15）只能走**协商撤销**（`cancel_order(flag=0)` 要 `PayPass`，`PayLevelBal`/`RepEnsureBal` 取详情现值；**详情为空时按 0 提交**并把这件事写进审计——真机 `参数错误` 的修法）；**已在协商撤销中**（`CancelStatus=11`）⇒ ⛔ 不重复申请，提示改用「同意撤销/取消撤销」；**已终态**（`Status=17` 已结算 / `CancelStatus ∈ {12,15,16}`）⇒ ⛔ 不能再撤回，给中文业务错误；补时**不要**支付密码（只有补款 `kind=0` 才要）；⚠️ `LevelOrderProgressAdd`/`ShareOrder` 会「假成功」⇒ **写动作成败一律回读**，本模块四个动作全部按「回读成功才算成功」实现（留言按任务约定例外）。
**失败分层（⛔ 不要 500）**：本地前置闸（没有推送行/没有上游单号/状态不允许/入参越界）⇒ 抛 `DailiantongPushError` **零上游写调用**；上游**明确**回业务失败 ⇒ 抛，文案含上游 `Err` **原文**；超时/连接失败 ⇒ 抛，按「**未知**」说清楚，⛔ 不重试写动作；写成功但**回读失败**或**回读值不符** ⇒ 抛，文案是「**已提交但没核验到**」（值不符把回读值写进文案），⛔ 不报成功、⛔ 不改本地行。**凭据红线**（`AGENTS.md` §4）：审计只写单号/金额/状态摘要；`app_secret`/`pay_password_md5`/派生的 `PayPass` 由客户端在发送时现算，本模块**从不**接触；上游回包的 `GamePass` 不进审计/日志/异常文案——本模块只从详情取 `Status`/`CancelStatus`/`Price`/`TimeLimit`/`PayLevelBal`/`RepEnsureBal` 这几个标量。"""

from __future__ import annotations

import threading
import time
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError
from django.db import connection as db_connection
from django.utils import timezone

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import dailiantong_push as push_module
from apps.business.dailiantong_client import (
    CANCEL_FLAG_APPLY,
    SPECIAL_KIND_TIME,
    DailiantongError,
    DailiantongTransportError,
)
from apps.business.dailiantong_push import (
    DEFAULT_PLATFORM,
    MIN_PRICE,
    REMOTE_STATUS_NOT_ACCEPTED,
    SYNC_ACTOR,
    TERMINAL_REMOTE_CANCEL_STATUSES,
    TERMINAL_REMOTE_STATUSES,
    DailiantongPushError,
    _as_int,
    _check_active_balance,
    _fmt_money,
    _to_decimal,
)
from apps.business.models import DailiantongPush, OrderStatus, PlatformIntegration

# ── 审计动作码（登记在 `apps/audit/actions.py`；⛔ 不带凭据）────────────────

AUDIT_WITHDRAW = "order.dailiantong_withdraw"
AUDIT_PRICE = "order.dailiantong_price"
AUDIT_TIME = "order.dailiantong_time"
AUDIT_MESSAGE = "order.dailiantong_message"
#: ⭐ C1（第三方平台订单管理页）：确认完单 / 手动同步交易状态。
AUDIT_ACCEPT = "order.dailiantong_accept"
AUDIT_SYNC = "order.dailiantong_sync"
#: ⭐ P7-详情：把一条失败 / 已关闭的推送**重发**（新链路行）。
AUDIT_REPUSH = "order.dailiantong_repush"

#: 审计的 `target_kind`：与抛单线同值（列宽 16，必须**恰好** ≤16）。
AUDIT_TARGET_KIND = "dailiantong_push"

# ── 业务常量 ────────────────────────────────────────────────────────────

#: 撤回时没给 `Comment` 的默认事由（给上游打手 / 客服看的）。
DEFAULT_WITHDRAW_COMMENT = "已在本系统撤回"

#: 留言长度上限（任务口径；上游文档没有写上限，本地先挡）。
MESSAGE_MAX_LENGTH = 512

#: 撤回事由长度上限（本地先挡；上游 `Comment` 列宽未知）。
WITHDRAW_COMMENT_MAX_LENGTH = 200

#: 补时的小时数边界（任务口径：1–720）。
TIME_MIN_HOURS = 1
TIME_MAX_HOURS = 720

#: 远端 `Status` 码（`docs/DAILIANTONG-API.md` §5）。
REMOTE_STATUS_UNACCEPTED = 11
REMOTE_STATUS_IN_PROGRESS = 12
REMOTE_STATUS_WAITING_ACCEPTANCE = 13
#: `Status=14` **订单异常** / `Status=15` **已锁定**（上游码表，`docs/DAILIANTONG-API.md` §5）。
#: ⭐ 真机（§6.11，业务方：「订单异常更应该能撤销」）：14 已实测允许协商撤销；**15 按业务方 口径同 14 放行**（单独真机样本仍缺）
#: ⇒ ⛔ 不再用本地白名单挡死。
REMOTE_STATUS_ABNORMAL = 14
REMOTE_STATUS_LOCKED = 15
REMOTE_STATUS_CANCELLING = 16
#: `Status=17` 已结算：**确认完单**（`LevelOrderOver`）成功后的回读判据。
REMOTE_STATUS_SETTLED = 17
#: `Status=18` **已下架**（上游码表没列 18，这一档是**真机观测**得来的）。真机：这张单被下架后，代练通回 `Status=18`
#: （我方轮询同步正常、`remote_status` 确实写成了 18），再用 `LevelOrderOffSell(Flag=1)`
#: 能把它上架回 `Status=11` ⇒ 18 是「已下架」这一事实态，不是异常码。 ⚠️ 因此「上架」写前闸**必须**放行 18，否则下架后永远上不了架（业务方实测被卡）。
REMOTE_STATUS_OFF_SHELF = 18

#: 远端 `CancelStatus` 码（同上）。
CANCEL_STATUS_NOT_CANCELLED = 10
#: `11` 协商撤销中。⭐ 这一档有**两种发起方**——我方发起（本地
#: `cancel_requested_at` 非空，可「取消撤销」）/ 打手发起（可「同意撤销」）。
#: 撤回自己**不再**对这一档发第二次写请求（见 `_withdraw_once` / `_cancel_in_flight_denial`）。
CANCEL_STATUS_NEGOTIATING = 11

#: 中文状态名（只服务错误文案；⛔ 不拿它做任何分支判据）。
REMOTE_STATUS_LABELS = {
    10: "临时订单",
    11: "未接手",
    12: "正在代练",
    13: "等待验收",
    14: "订单异常",
    # ⭐ 真机核实：`Status=15` **不是「帐号被平台锁」**，而是**被我们锁定**—— `LevelOrderLock(Locked=1)` 在 `Status=14` 上执行后回 `Status=15`、`OrderLocked=true`；
    # `Locked=0` 解锁后**回到 14**（12 / 13 上锁同理）。⇒ 中文叫「已锁定」。 ⛔ 前端 `src/frontend/src/layui-island/platformPush.ts::REMOTE_STATUS_LABELS` 必须同值。
    15: "已锁定",
    16: "撤销中",
    17: "已结算",
    # ⭐ 真机：上游码表（`docs/DAILIANTONG-API.md` §5）只有 10–17，但下架后
    # 代练通回 `Status=18`；用 `LevelOrderOffSell(Flag=1)` 能上架回 11 ⇒ 18 = 已下架。
    # ⛔ 前端 `src/frontend/src/layui-island/platformPush.ts::REMOTE_STATUS_LABELS` 必须同值。
    REMOTE_STATUS_OFF_SHELF: "已下架",
}

#: 走**协商撤销**的远端状态（未接手 11 除外，它走删除）。 ⭐ 真机（§6.11，业务方口径）：`12 / 13 / 14 / 15` 都允许申请撤销—— 12 正在代练 / 13 等待验收 / 14 订单异常 / 15 已锁定，**14 已实测允许；15 按业务方口径同 14 放行**（不是「交上游判」）。
#: ⛔ `16 撤销中` **不在内**：撤销流程已经启动（`CancelStatus=11` 时提示用「同意 / 取消撤销」），再申请一次是重复动作，上游回的是看不懂的 `参数错误`；`Status=16` 由 `_withdraw_once`
#: 明确拒绝（不接受新的撤销申请）。 ⚠️ 10 临时订单 / 18 已下架 / 解析不出的新码仍在本地下挡（不替上游挑动作）。
REMOTE_CANCELABLE_STATUSES = frozenset(
    {
        REMOTE_STATUS_IN_PROGRESS,
        REMOTE_STATUS_WAITING_ACCEPTANCE,
        REMOTE_STATUS_ABNORMAL,
        REMOTE_STATUS_LOCKED,
    }
)

#: 允许改价的远端状态：**只有未接手**（实测 `-1 只有未接手状态才能修改价格`）。
REMOTE_PRICE_EDITABLE_STATUSES = frozenset({REMOTE_STATUS_UNACCEPTED})

# ── ⭐ H3-1：撞单 0 撤 ──────────────────────────────────────

#: 「撞单 0 撤」的撤回事由（业务方原话，**逐字**）：
#: 「若发生撞单（多个平台同时接手）时，系统会保留最早接单的那个，其它所有平台全部申请 0 撤」。
CONFLICT_ZERO_REFUND_COMMENT = "系统自动：撞单保留最早接单，其余 0 撤"

#: 「撞单 0 撤」适用的远端状态：**已接手**（正在代练 12 / 等待验收 13）。
#: ⚠️ 只认这两档：11 还没人接（撞单无从谈起）、16 撤销中 / 17 已结算都是**不需要也不能**
#: 再申请撤销的档（终态闸会拒）。
CONFLICT_ZERO_REFUND_STATUSES = frozenset(
    {REMOTE_STATUS_IN_PROGRESS, REMOTE_STATUS_WAITING_ACCEPTANCE}
)

#: 「0 撤」的两个金额：**都填 0**（我们不向打手要代练费、也不要保证金）。 ⚠️ 判据必须是 `is not None`（D-0015）：`0` 在这里是**有意义的真实取值**，
#: 用真值判断会让 `_withdraw_by_cancel` 把它当成「没填」从而退回详情原值 —— 那就不是 0 撤了。 ⭐ 真机已验证（`docs/DAILIANTONG-API.md` §6.9）：`Flag=0, PayLevelBal=0,
#: RepEnsureBal=0` 上游回「撤销成功」并把单推进 `Status=16 / CancelStatus=11`。用 `Decimal("0")`（线上形态就是字面 `0`，与那次真机调用一致），⛔ 不写成 `0.00` 之外的猜测值。
CONFLICT_ZERO_REFUND_AMOUNT = Decimal("0")

# ── 小工具 ──────────────────────────────────────────────────────────────


def _status_label(status: int | None) -> str:
    """状态码的人话（解析不了就给原值，⛔ 不编一个不存在的状态名）。"""
    if status is None:
        return "未知"
    return REMOTE_STATUS_LABELS.get(status, f"未知（{status}）")


def _require_operator(operator) -> None:
    """编程错误（⛔ 不是业务失败）：接口层永远传认证态，静默当机器行会丢操作人。"""
    if operator is None or getattr(operator, "pk", None) is None:
        raise ValueError("代练通平台动作需要已认证的操作人（operator.pk）")


def _active_integration(platform: int):
    """本租户启用且默认的代练通账号（与抛单线同一个前置闸 / 同一份中文文案）。"""
    return push_module._active_integration(platform)


def _open_client(integration):
    """测试替换点：**委托**抛单模块的同一个客户端构造点（⛔ 不在这里复制凭据读取）。"""
    return push_module._open_client(integration)


def _close(client) -> None:
    push_module._close(client)


def _latest_active_push(order, platform: int, *, allow_settled_closed=False) -> DailiantongPush:
    """平台动作只选最新有效推送；评价还允许最新一笔已经结算关闭。"""
    rows = DailiantongPush.objects.filter(order=order, platform=platform)
    if allow_settled_closed:
        push = push_module._latest_unsuperseded_push(order, platform)
        if push is not None and push.state == DailiantongPush.State.CLOSED:
            if push.remote_status not in TERMINAL_REMOTE_STATUSES:
                push = None
    else:
        push = rows.filter(state__in=DailiantongPush.ACTIVE_STATES).order_by("-id").first()
    if push is None:
        raise DailiantongPushError(
            "这张单还没有抛到代练通（没有有效的推送记录），不能做平台操作。"
        )
    if not (push.serial_no or "").strip():
        raise DailiantongPushError(
            "这条代练通推送还没有上游订单号（可能仍在发送中或状态未知），"
            "请先刷新状态、或到代练通后台核对后再操作。"
        )
    return push


def _read_detail(client, push: DailiantongPush, *, action_name: str) -> dict:
    """**写之前**的回读：拿不到详情就不动手（零写调用）。"""
    try:
        detail = client.order_detail(push.serial_no)
    except DailiantongTransportError as exc:
        raise DailiantongPushError(
            f"拿不到代练通订单详情，没有执行{action_name}（{exc.message}）。请稍后重试。"
        ) from exc
    except DailiantongError as exc:
        raise DailiantongPushError(
            f"拿不到代练通订单详情，没有执行{action_name}（上游提示：{exc.message}）。"
        ) from exc
    if not isinstance(detail, dict):  # pragma: no cover - 客户端已保证 dict
        raise DailiantongPushError(
            f"代练通返回的订单详情格式不对，没有执行{action_name}。请稍后重试。"
        )
    return detail


def _readback_detail(client, push: DailiantongPush, *, action_name: str) -> dict:
    """**写之后**的回读：拿不到详情 ⇒ 一律「已提交但没核验到」（⛔ 不报成功）。"""
    try:
        detail = client.order_detail(push.serial_no)
    except (DailiantongTransportError, DailiantongError) as exc:
        raise DailiantongPushError(
            f"已提交{action_name}，但没能核验到结果（{exc.message}）。"
            "请稍后刷新状态、或到代练通后台核对。"
        ) from exc
    if not isinstance(detail, dict):  # pragma: no cover - 客户端已保证 dict
        raise DailiantongPushError(
            f"已提交{action_name}，但代练通返回的详情格式不对，没能核验到结果。"
            "请稍后刷新状态、或到代练通后台核对。"
        )
    return detail


def _guard_terminal(detail: dict, *, action_name: str) -> None:
    """写前统一终态闸（P2-8 + h1）：已结算（`Status=17`）/ **撤销已完成** （`CancelStatus ∈ {12,15,16}`）⇒ ⛔ 一个写动作都不发。
    ⭐ h1：终态从「只认 16 强制撤销」放宽到 12/15/16 —— 协商已处理 / 客服已处理同样是撤销完成，再补时 / 留言只会打扰对方、还容易被上游回一个看不懂的错误。
    补时 / 留言此前没有这道闸：远端已结算仍会真发出去。撤回 / 改价走各自的状态分支。"""
    status = _as_int(detail.get("Status"))
    cancel_status = _as_int(detail.get("CancelStatus"))
    if (
        status in TERMINAL_REMOTE_STATUSES
        or cancel_status in TERMINAL_REMOTE_CANCEL_STATUSES
    ):
        raise DailiantongPushError(
            f"这张单在代练通已结束（已结算或撤销已完成），不能再{action_name}。"
        )


def _money_equal(actual, expected: Decimal) -> bool:
    """上游金额与本地期望值是否相等（`is not None` 判据，⛔ 不用真值）。"""
    if actual is None:
        return False
    try:
        return _to_decimal(actual) == expected
    except (InvalidOperation, TypeError, ValueError):
        return False


def _record_action(
    push: DailiantongPush,
    order,
    operator,
    *,
    action: str,
    reason: str,
    from_value: str = "",
    to_value: str = "",
    system: bool = False,
) -> None:
    """写一条平台动作审计（`target` 指向推送行；⛔ 不含任何凭据）。 `reason` 只允许是**中文说明**或**上游 `Err` 原文**——本模块的调用点不把上游详情
    （里面有明文 `GamePass`）整体塞进来（`AGENTS.md` §4）。 ⚠️ `operator=None` + `system=True` = **机器来源**（A4 的「内部打手先接 ⇒ 撤回代练通」
    由同步命令驱动，⛔ 不是任何人类账号点的）。`record()` 的 fail-closed 判据要求 `actor_id=None` 必须显式声明机器来源，所以这里按同一条件把 `system` 传下去。"""
    actor_pk = getattr(operator, "pk", None)
    record(
        log_type=ACTION_LOG_TYPES[action],
        action=action,
        actor=(getattr(operator, "username", "") or "").strip() or SYNC_ACTOR,
        actor_id=actor_pk,
        system=bool(system or actor_pk is None),
        target_kind=AUDIT_TARGET_KIND,
        target_id=str(push.pk),
        target_name=order.order_no,
        order_no=order.order_no,
        source_order_no=order.source_order_no,
        game_name=(order.game.name or "") if order.game_id else "",
        from_value=from_value,
        to_value=to_value,
        reason=reason,
    )


def _save_remote_snapshot(
    push: DailiantongPush,
    detail: dict,
    *,
    extra_fields: tuple[str, ...] = (),
) -> None:
    """保存平台详情。已结算的推送等本地完单落库后再关闭轮询。"""
    status = _as_int(detail.get("Status"))
    cancel_status = _as_int(detail.get("CancelStatus"))
    changed = push_module.store_remote_fields(
        push, detail, status=status, cancel_status=cancel_status
    )
    # ⭐ dlt-d：接单打手 UID 是「拉黑打手」的唯一来源；
    # 详情里有值时补存（`store_remote_fields` 已存过的同名值不重复写）。
    accept_uid = str(detail.get("AcceptUID") or "").strip()
    if accept_uid and "accept_uid" not in changed:
        push.accept_uid = accept_uid
        changed = [*changed, "accept_uid"]
    if (
        push.remote_cancel_status in TERMINAL_REMOTE_CANCEL_STATUSES
        and push.remote_status not in TERMINAL_REMOTE_STATUSES
    ):
        push.is_active = False
        changed = [*changed, "is_active"]
    push.last_synced_at = timezone.now()
    push.last_error = ""
    try:
        push.save(
            update_fields=[
                *changed,
                "last_synced_at",
                "last_error",
                "updated_at",
                *extra_fields,
            ]
        )
    except IntegrityError as exc:
        # ⭐ H2：同 `refresh_push_status` 那条 UPDATE——`store_remote_fields`
        # 清掉 `cancel_requested_at` 时可能撞 `uniq_dailiantong_push_active`（D8 重发留下的
        # NULL 在途行）。结构修复在 `_clear_stale_cancel_fields`；这里把**并发窗口**翻成可读中文（⛔ 不让它变成 500 / 假装成功）。
        if "uniq_dailiantong_push_active" not in str(exc):
            raise
        raise DailiantongPushError(
            "回读显示撤销申请已被取消，但同一订单在同一平台已有一条在途推送，"
            "这条旧推送的本地撤销标记不能清（否则会撞唯一索引）；"
            "下一轮同步会把它收口，页面稍后刷新即可。"
        ) from exc


# ── 撤回 ────────────────────────────────────────────────────────────────


def _withdraw_by_delete(client, push: DailiantongPush) -> str:
    """未接手（`Status=11`）：`LevelOrderDelSelf` 删除并**即时退款**，再回读确认查不到。"""
    try:
        client.delete_order(push.serial_no)
    except DailiantongTransportError as exc:
        # ⛔ 不知道删没删掉 ⇒ 按「未知」处理，**不重试**。
        raise DailiantongPushError(
            f"删除代练通订单的结果未知（{exc.message}）。"
            "请到代练通后台核对这张单还在不在，再决定是否重试。"
        ) from exc
    except DailiantongError as exc:
        raise DailiantongPushError(
            f"代练通没有接受这次删除（上游提示：{exc.message}）。"
        ) from exc

    # 回读：删除成功后详情应当**查不到**（实测：再查状态得到空）。
    try:
        detail = client.order_detail(push.serial_no)
    except DailiantongTransportError as exc:
        raise DailiantongPushError(
            f"已提交删除，但没能核验到结果（{exc.message}）。"
            "请稍后刷新状态、或到代练通后台核对。"
        ) from exc
    except DailiantongError as exc:
        # 上游明确回业务失败 **并且** 文案像「这张单没了」⇒ 删除已生效；
        # ⚠️ 其它业务失败（`-106 处理数据异常` 之类）**不能**当成已删除，
        # 否则会把「没核验到」说成成功。
        message = (exc.message or "").strip()
        if push_module.remote_absent_evidence(message):
            evidence = message
        else:
            raise DailiantongPushError(
                f"已提交删除，但回读代练通详情时上游回了业务失败（{message}），"
                "没能确认这张单已经查不到。请到代练通后台核对。"
            ) from exc
    else:
        if not detail:
            evidence = "详情已为空"
        else:
            raise DailiantongPushError(
                "已提交删除，但回读仍能查到这张代练通订单"
                f"（Status={_status_label(_as_int(detail.get('Status')))}），没能核验到删除。"
                "请到代练通后台核对。"
            )

    push.state = DailiantongPush.State.CLOSED
    push.is_active = False
    push.last_synced_at = timezone.now()
    push.last_error = ""
    push.save(
        update_fields=[
            "state",
            "is_active",
            "last_synced_at",
            "last_error",
            "updated_at",
        ]
    )
    return f"未接手订单已删除（即时退款），回读确认查不到（{evidence}）"


def withdraw_unaccepted_for_platform_refund(push: DailiantongPush) -> None:
    """平台退款只允许删除明确未接手的单。写后必须回读确认已删除。"""
    if push.state != DailiantongPush.State.SENT or not (push.serial_no or "").strip():
        raise DailiantongPushError("代练通发单结果未确认，请核对平台订单")

    client = _open_client(_active_integration(push.platform))
    try:
        detail = _read_detail(client, push, action_name="退款自动撤回")
        status = _as_int(detail.get("Status"))
        cancel_status = _as_int(detail.get("CancelStatus"))
        accept_uid = str(detail.get("AcceptUID") or "").strip()
        if (
            status not in REMOTE_STATUS_NOT_ACCEPTED
            or cancel_status not in (None, CANCEL_STATUS_NOT_CANCELLED)
            or accept_uid not in ("", "0")
        ):
            raise DailiantongPushError(
                f"代练通订单当前为「{_status_label(status)}」，无法确认未接手，请人工核对"
            )
        evidence = _withdraw_by_delete(client, push)
    finally:
        _close(client)
    _record_action(
        push, push.order, None, action=AUDIT_WITHDRAW,
        reason=f"平台订单退款，自动撤回代练通：{evidence}", system=True,
    )


#: 上游「参数错误」类原文（`LevelOrderCancel` 在**状态不允许**时回的就是它，实测）。
#: ⚠️ 这是**翻译层**的关键词，不是状态判据：状态判据在 `_withdraw_once`（写前回读）。
INVALID_PARAM_HINTS = ("参数错误", "无效的参数", "无效参数", "参数有误")


def _withdraw_state_refusal(message: str) -> str | None:
    """把上游「参数错误」翻译成业务话术；其它原文 ⇒ `None`（调用方原样带出）。 ⭐ 真机：对 `Status=12` 的单申请撤销回 `参数错误.` —— 根因是我们没带齐
    两个金额 / 事由（见 `_withdraw_by_cancel`）。但同一句原文也可能来自「状态本来就不允许」，
    对客服来说「参数错误」等于没说 ⇒ 统一翻成「该订单当前状态不允许撤回（代练通回复：<原文>）」。"""
    text = (message or "").strip()
    if not text:
        return None
    if any(hint in text for hint in INVALID_PARAM_HINTS):
        return f"该订单当前状态不允许撤回（代练通回复：{text}）"
    return None


def _detail_withdraw_amount(value) -> tuple[Decimal, bool]:
    """详情里的撤销金额：**空 / None 才按 0**；有值但解析不出 ⇒ 中文拒绝（⭐ I2）。返回 `(金额, 是否按 0 兜底)`。⭐ 真机：详情里 `PayLevelBal` / `RepEnsureBal`
    可能是**空串**，而 `Flag=0` 要求它们**非空** ⇒ 空值按 0 提交。 ⚠️ 但脏值（`"1,5"` / `"1.5元"`）**不能**按 0——那等于替客服决定「0 元撤销」（真钱风险）；
    直接中文拒绝，让客服到代练通后台处理。 ⚠️ 页面显式填了值时不走本函数（页面值由 `_optional_money` 校验，一字不改交给上游）。"""
    if value is None:
        return Decimal("0"), True
    if isinstance(value, str) and not value.strip():
        return Decimal("0"), True
    try:
        return _to_decimal(value), False
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DailiantongPushError(
            "代练通返回的撤销金额无法识别"
            f"（原值：{value!r}），请到代练通后台处理。"
        ) from exc


def _withdraw_by_cancel(
    client,
    push: DailiantongPush,
    detail: dict,
    *,
    comment: str | None,
    pay_level_bal=None,
    rep_ensure_bal=None,
    auto_arbitrate: bool = False,
) -> str:
    """已接手（`Status=12/13/14/15`；14 已真机验证、15 按业务方口径，§6.11）：协商撤销 `LevelOrderCancel(flag=0)`，再回读 `CancelStatus`。 ⭐ P7（业务方纠正「撤回大厅」vs「申请撤销」）：两个金额（**支付代练费** `PayLevelBal` / **赔保证金** `RepEnsureBal`）改成**页面可填**—— 传了就用页面填的值（一字不改地交给上游），没传才用详情里的现值。
    旧实现是**静默从详情取**，操作人看不到也改不了，与对标的撤销弹窗（三格）不符。 ⭐ 真机（`参数错误.` 的修法）：详情里的两个金额可能是**空串**，而 `Flag=0` 要求它们非空 ⇒ 页面没填、详情又为空 / 解析不出时，**按 `Decimal("0")` 提交**，并把「详情未给值，按 0 提交」写进审计 `reason`（⛔ 不静默吞掉这个事实）。 `Comment` 为空时本来就有 `DEFAULT_WITHDRAW_COMMENT`（非空），本次只补测试钉住。
    ⭐ 成功提交（回读 `CancelStatus != 10`）时记 `cancel_requested_at` 与业务开关 `auto_arbitrate`——定时任务据此在 **24 小时**后自动申请客服介入（`dailiantong_extra_actions.auto_arbitrate_pending_pushes`），满 1 小时才允许调上游。 ⚠️ 开关**默认关**（业务方口径）：不勾选时**代练通侧 24 小时自己会转客服介入**，我们再去申请就是重复申请 ⇒ 只有客服主动勾选才代发。 ⚠️ 一次新的协商撤销 = 自动仲裁重新计时（次数 / 退避 / 已申请标记一起清空）。"""
    comment = (comment or "").strip() or DEFAULT_WITHDRAW_COMMENT
    if len(comment) > WITHDRAW_COMMENT_MAX_LENGTH:
        raise DailiantongPushError(
            f"撤回事由不能超过 {WITHDRAW_COMMENT_MAX_LENGTH} 个字符。"
        )

    # 页面没填 ⇒ 用详情现值；详情为空 / 解析不出 ⇒ 按 0 提交（见 docstring）。
    detail_note = ""
    zero_filled: list[str] = []
    if pay_level_bal is None:
        pay_level_bal, pay_empty = _detail_withdraw_amount(detail.get("PayLevelBal"))
        if pay_empty:
            zero_filled.append("支付代练费")
    if rep_ensure_bal is None:
        rep_ensure_bal, rep_empty = _detail_withdraw_amount(detail.get("RepEnsureBal"))
        if rep_empty:
            zero_filled.append("赔偿保证金")
    if zero_filled:
        detail_note = (
            "；详情未给值，按 0 提交（" + " / ".join(zero_filled) + "）"
        )

    try:
        client.cancel_order(
            push.serial_no,
            CANCEL_FLAG_APPLY,
            pay_level_bal=pay_level_bal,
            rep_ensure_bal=rep_ensure_bal,
            comment=comment,
        )
    except DailiantongTransportError as exc:
        raise DailiantongPushError(
            f"提交协商撤销的结果未知（{exc.message}）。"
            "请到代练通后台核对这张单的撤销状态，再决定是否重试。"
        ) from exc
    except DailiantongError as exc:
        # ⭐ 「参数错误」类原文翻成业务话术（带原文），其它原文照旧透出。
        # h11/h14 口径：14 已真机验证、15 按业务方口径放行；任何上游拒绝都把**原话**留给客服。
        mapped = _withdraw_state_refusal(exc.message)
        if mapped:
            raise DailiantongPushError(f"{mapped}。") from exc
        raise DailiantongPushError(
            f"代练通没有接受这次撤销申请（代练通回复：{exc.message}）。"
        ) from exc

    detail_after = _readback_detail(client, push, action_name="撤销申请")
    cancel_status = _as_int(detail_after.get("CancelStatus"))
    if cancel_status is None or cancel_status == CANCEL_STATUS_NOT_CANCELLED:
        raise DailiantongPushError(
            "已提交撤销申请，但回读代练通的撤销状态仍是「未撤销」，没能核验到。"
            "请稍后刷新状态、或到代练通后台核对。"
        )

    # ⭐ D8：本地记下「撤销申请已提交」——重发（新推送行）的唯一本地依据。 ⭐ 申请撤销成功 ⇒ 记时刻 + 业务开关（自动仲裁从这一刻起算）。
    # ⚠️ 位置在**回读核验之后**：没核验到的「可能已提交」不记，避免自动仲裁拿一个我们都不确定存在的撤销去申请客服介入。
    # ⚠️ 一次新的协商撤销 = 自动仲裁**重新计时**（次数 / 退避 / 已申请标记一起清空）。
    push.cancel_requested_at = timezone.now()
    push.auto_arbitrate = bool(auto_arbitrate)
    push.auto_arbitrate_attempts = 0
    push.auto_arbitrate_next_at = None
    push.auto_arbitrated_at = None
    push.auto_arbitrate_note = ""
    _save_remote_snapshot(
        push,
        detail_after,
        extra_fields=(
            "cancel_requested_at",
            "auto_arbitrate",
            "auto_arbitrate_attempts",
            "auto_arbitrate_next_at",
            "auto_arbitrated_at",
            "auto_arbitrate_note",
        ),
    )
    return (
        "已接手订单已提交协商撤销"
        f"（回读 CancelStatus={cancel_status}），等待打手 / 客服处理{detail_note}"
    )


def _withdraw_once(
    push: DailiantongPush,
    *,
    comment: str | None,
    pay_level_bal=None,
    rep_ensure_bal=None,
    auto_arbitrate: bool = False,
    allow_unaccepted_delete: bool = False,
) -> str:
    """对一条推送执行一次撤回（分支 + 回读核验），成功返回中文结论。 ⚠️ 审计**不在**本函数里：调用方知道这次是「人点的」还是「同步命令驱动」的（`withdraw_push` / `withdraw_push_for_machine`），各自写同一份 `order.dailiantong_withdraw`。判据（与 `withdraw_push` 的 docstring 同一份）：
    - `11` 未接手 ⇒ 删除（即时退款）→ 回读确认查不到； - `12 / 13 / 14 / 15` ⇒ 协商撤销（`flag=0`）→ 回读 `CancelStatus != 10`； ⭐ 14 订单异常已真机验证允许（§6.11）；15 按业务方口径同 14，不是「交上游判」。 - `17 / CancelStatus=12/15/16`（撤销已完成）⇒ 终态，⛔ 不发任何写动作；
    - `Status=16`（撤销中，且没有 `CancelStatus=11`）⇒ **终态分支：不接受新的撤销申请**； - `CancelStatus=11`（协商撤销已在流程里）⇒ **⛔ 不再提交第二次撤回**：这一档的正确动作是「取消撤销」（我方发起）或「同意撤销」（打手发起），由**别的动作线**实现；本函数零写调用、按发起方给中文原因（`_cancel_in_flight_denial`）； - 其它（10 临时 / 18 已下架 / 解析不出）⇒ 本系统不代发，交人工。"""
    integration = _active_integration(push.platform)
    client = _open_client(integration)
    try:
        detail = _read_detail(client, push, action_name="撤回")
        status = _as_int(detail.get("Status"))
        cancel_status = _as_int(detail.get("CancelStatus"))

        if (
            status in TERMINAL_REMOTE_STATUSES
            or cancel_status in TERMINAL_REMOTE_CANCEL_STATUSES
        ):
            raise DailiantongPushError(
                "这张单在代练通已结束（已结算或撤销已完成），不能再撤回。"
            )

        if cancel_status == CANCEL_STATUS_NEGOTIATING:
            # ⭐ 真机口径：`CancelStatus=11`（协商撤销中）有两种发起方。我方发起（本地 `cancel_requested_at` 非空）⇒ 可「取消撤销」；
            # 打手发起 ⇒ 可「同意撤销」。**都不该再走一次「撤回」**——再来一次 `LevelOrderCancel(Flag=0)` 就是重复提交协商撤销（真钱链路，零写调用最安全）。
            raise DailiantongPushError(_cancel_in_flight_denial(push))

        if status == REMOTE_STATUS_UNACCEPTED:
            return _withdraw_by_delete(client, push)
        # ⭐ 即时指派撤回要求「空 / 10 / 18」也走 DelSelf（不进协商撤销）；
        # 只由内部打手接单的即时路径打开，人工撤回仍维持原来的「10/18 交人工」。
        if allow_unaccepted_delete and (status is None or status in (10, 18)):
            return _withdraw_by_delete(client, push)
        if status in REMOTE_CANCELABLE_STATUSES:
            return _withdraw_by_cancel(
                client,
                push,
                detail,
                comment=comment,
                pay_level_bal=pay_level_bal,
                rep_ensure_bal=rep_ensure_bal,
                auto_arbitrate=auto_arbitrate,
            )
        if status == REMOTE_STATUS_CANCELLING:
            # ⭐（终态口径）：`Status=16` 撤销中（且没有 `CancelStatus=11`）
            # ⇒ 撤销流程已经启动，**不接受新的撤销申请**（再申请只会拿到上游的参数错误）。
            raise DailiantongPushError(
                "这张单在代练通已处于「撤销中」（Status=16），不接受新的撤销申请；"
                "请以代练通后台为准。"
            )
        # 10 临时 / 18 已下架 / 解析不出的码：本地不替它挑动作（⛔ 不猜）。 ⚠️ 14 / 15 已在上面走协商撤销分支（14 真机验证、15 按业务方口径，§6.11）。
        # ⚠️ 本地下挡 = **零上游写调用** ⇒ 没有「上游原文」可附；把读到的原始 Status 一并给出，
        # 措辞只说「本系统不代发」，⛔ 不再替上游断言「不支持撤回」。
        status_text = "未返回" if status is None else str(status)
        raise DailiantongPushError(
            f"代练通这张单当前状态是「{_status_label(status)}」（Status={status_text}），"
            "本系统不代发撤回；请以代练通后台为准。"
        )
    finally:
        _close(client)


def _cancel_in_flight_denial(push: DailiantongPush) -> str:
    """协商撤销已在流程里时，为什么不能再「撤回」+ 应该走哪个动作（⭐ 业务方口径）。 `CancelStatus=11` 的发起方有两档（真机 / 业务口径）：
    - **我方发起**：本地有 `cancel_requested_at`（只有我们提交撤回时才写）⇒ 想终止就用「取消撤销」（`LevelOrderCancel(Flag=1)`，无金额无密码）；
    - **打手发起**：本地没有那个时刻 ⇒ 想结束就只能「同意撤销」（由 h13 那条线实现）。 ⚠️ 历史行 / 本地没记到时刻的，可能其实是我方发起 ⇒ 两种话术都以「可做什么」为主，不替用户断言「是谁点的」。"""
    if push.cancel_requested_at is not None:
        return (
            "该订单已在协商撤销中（CancelStatus=11），不能再提交撤回；"
            "如需终止这次撤销，请用「取消撤销」（对方也可能「同意撤销」）。"
        )
    return (
        "该订单已在协商撤销中（CancelStatus=11），不能再提交撤回；"
        "这是打手发起的撤销，请用「同意撤销」处理（对方也可「取消撤销」），"
        "或等打手 / 客服处理。"
    )


def _optional_money(value, *, label: str):
    """页面传来的两个撤销金额：`None` = 没填（用详情现值）；填了必须是 ≥ 0 的金额。"""
    if value is None:
        return None
    try:
        amount = _to_decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DailiantongPushError(f"{label}不是有效数字。") from exc
    if amount < 0:
        raise DailiantongPushError(f"{label}不能是负数。")
    return amount


def withdraw_push(
    order,
    *,
    operator,
    comment: str | None = None,
    platform: int = DEFAULT_PLATFORM,
    pay_level_bal=None,
    rep_ensure_bal=None,
    auto_arbitrate: bool = False,
) -> DailiantongPush:
    """撤回一张已抛到代练通的单（`POST …/platform-push/withdraw/` 的服务层）。分支判据**只看远端 `Status`**（不信本地状态；本地 `state` 只是链路行状态）： - `11` 未接手 ⇒ 删除（即时退款）→ 回读确认查不到 ⇒ 本地行 `state=closed`； - `12` / `13` / `14` / `15` ⇒ 协商撤销（`flag=0`，带支付密码 + 两项金额） → 回读 `CancelStatus`（`!= 10` 才算提交成功）； ⭐ `14` 订单异常已真机验证允许（§6.11）；`15` 按业务方口径同 14，不是「交上游判」；
    `CancelStatus=11` 时按发起方提示「取消撤销 / 同意撤销」，⛔ 不重复申请； - `Status=16` 撤销中（且无 `CancelStatus=11`）⇒ 终态分支：不接受新的撤销申请； - `17` / `CancelStatus=12/15/16` ⇒ 中文业务错误（已结束 / 撤销已完成，不能再撤回）； - 其它状态 ⇒ 中文业务错误（当前状态不支持撤回；14/15 已放行交上游判，见 h11 口径）。 ## ⭐ P7：两个金额由**页面**给（对标撤销弹窗的三格） - `pay_level_bal`（支付代练费）/ `rep_ensure_bal`（赔保证金）：`None` = 没填 ⇒ 用详情现值；
    详情的值为空 / 解析不出 ⇒ **按 0 提交**并把「详情未给值，按 0 提交」写进审计（真机：空值直接上送会被上游回 `参数错误.`）；页面填了就用页面那个值（一字不改交给上游）； - `comment`（撤销理由）：空 ⇒ `DEFAULT_WITHDRAW_COMMENT`； - 支付密码 `PayPass` 永远由客户端现算（⛔ 不上送、⛔ 不进审计）。 - ⭐ `auto_arbitrate`（**默认 `False`**，业务方口径）：这次协商撤销成功后，定时任务在 **24 小时**仍未处理时自动申请客服介入； **只有客服在撤销弹窗主动勾选**才代发——不勾选时代练通侧 24 小时自己会转客服介入，我们再去申请就是重复申请。落点是推送行上的 `auto_arbitrate` + `cancel_requested_at`（见 `_withdraw_by_cancel`）。成功返回更新后的推送行；任何失败抛 `DailiantongPushError`（中文、可展示，⛔ 不 500）。"""
    _require_operator(operator)
    pay_level_bal = _optional_money(pay_level_bal, label="支付代练费")
    rep_ensure_bal = _optional_money(rep_ensure_bal, label="赔偿保证金")
    push = _latest_active_push(order, platform)
    try:
        reason = _withdraw_once(
            push,
            comment=comment,
            pay_level_bal=pay_level_bal,
            rep_ensure_bal=rep_ensure_bal,
            auto_arbitrate=auto_arbitrate,
        )
    except DailiantongPushError as exc:
        # 失败也留痕（含「已提交但没核验到」「结果未知」这两种最需要人来看的）。
        _record_action(push, order, operator, action=AUDIT_WITHDRAW, reason=str(exc))
        raise
    _record_action(push, order, operator, action=AUDIT_WITHDRAW, reason=reason)
    return push


def withdraw_push_for_machine(
    push: DailiantongPush,
    *,
    comment: str | None = None,
    allow_unaccepted_delete: bool = False,
) -> str:
    """**机器路径**撤回：A4 的「内部打手先接 ⇒ 及时把代练通订单撤掉」（Rev.9 §4 第 2 条）。与人工撤回共用**同一份**撤回原语 `_withdraw_once`（⛔ 不另写一套）：
    未接手删除（即时退款）/ 已接手申请协商撤销 / 回读核验。 ⚠️ 审计操作人 = `代练通同步`（机器来源：`actor_id=None` + `system=True`）。
    ⚠️ 失败抛 `DailiantongPushError`（调用方 `reconcile_internal_claims` 做有界重试 + 留痕）。"""
    try:
        reason = _withdraw_once(
            push,
            comment=comment,
            allow_unaccepted_delete=allow_unaccepted_delete,
        )
    except DailiantongPushError as exc:
        _record_action(
            push,
            push.order,
            None,
            action=AUDIT_WITHDRAW,
            reason=f"内部打手先接，自动撤回代练通失败：{exc}",
            system=True,
        )
        raise
    _record_action(
        push,
        push.order,
        None,
        action=AUDIT_WITHDRAW,
        reason=f"内部打手先接，自动撤回代练通：{reason}",
        system=True,
    )
    return reason


#: 内部打手先接 ⇒ 立刻撤回代练通时的统一事由（也进审计 reason 的上游备注）。
INTERNAL_TAKER_WITHDRAW_COMMENT = "内部打手已接单"
#: 撤回失败时给客服的话术（业务方原话结构）。
INTERNAL_WITHDRAW_FAILED_HINT = (
    "代练通订单未能撤回（原话：{error}），"
    "请先到「第三方平台订单管理」撤回代练通平台订单，再指派打手。"
)


def _note_internal_withdraw_failure(push, reason: str) -> str:
    """**即时撤回失败也要留痕**（M3）：写 `remote_note` 后返回客服话术。为什么必须落库：即时路径（指派 / 抢单成功后的那次同步撤回）失败时原来只在响应 `msg` 里说一句——客服关掉弹窗后，页面上**看不到任何『为什么没撤成』**的痕迹。
    写入 `remote_note`（可读中文、按列宽截断）之后： - 页面「最近同步结论」立刻可见； - **兜底能接手**：不写 `claim_withdraw_at` / 不扣 `claim_withdraw_attempts`，
    下一轮 `reconcile_internal_claims` 仍会把这条行当作待撤对象重试； - 不碰 `state` / `is_active`：撤回没成功，平台单还在，判据不能假装它结束了。"""
    text = (
        f"内部打手已接单，但代练通订单撤回失败：{reason}；"
        "已留给同步任务自动重试，也可到「第三方平台订单管理」人工撤回"
    )
    push.remote_note = push_module._clip_note(text)
    push.save(update_fields=["remote_note", "updated_at"])
    return INTERNAL_WITHDRAW_FAILED_HINT.format(error=reason)


def withdraw_for_internal_claim(
    order, *, platform: int = DEFAULT_PLATFORM
) -> tuple[bool, str]:
    """⭐ 指派 / 抢单成功后**立刻**撤掉该单的代练通订单；返回 `(有推送, 提示)`。与同步兜底 `reconcile_internal_claims` 同一份撤回原语，区别是**同步返回结果**： - 没有有效推送 ⇒ `(False, "")`，**零上游调用**； - 远端未接手（空 / 10 / 11 / **18**）⇒ `LevelOrderDelSelf`（删除退款）；
    - 远端已接手（12 / 13 / 14 / 15）⇒ 协商撤销（`flag=0`，金额按详情现值 / 空按 0，事由「内部打手已接单」）； - 远端已结束（17 / CancelStatus 12/15/16）⇒ 不重复撤，返回一句已结束； - 上游拒绝 / 结果未知 ⇒ `(True, 中文提示含上游原话)`，⛔ 不假装撤回成功。 ⚠️ 本函数在**指派事务之外**调用（真实上游写请求不进事务）；失败只返回提示，
    本地指派结果仍然有效——调用方必须把这句话随响应回给客服。 ⚠️ **H1 修复——「本地真有真人接手」是动手的前提**： `normal_taker_of(order) is None`（还没人接 / 只有占位账号「代练通」）⇒ **零上游调用、直接返回**。这道闸是结构性的：编辑订单（`partial_update`）曾经也调本函数（挂错了位置），而那里不可能产生真人接手人 ⇒ 一次「改备注」会把真钱平台单删掉。昨天把那个挂点摘掉了，但**闸必须留在本函数里**：它回答的是「这次动作的业务前提成不成立」，不靠调用方自觉。"""
    # ⭐ H1：没有**真人**接手人 ⇒ 不撤（占位账号「代练通」不算真人，见 `normal_taker_of`）。
    if push_module.normal_taker_of(order) is None:
        return False, ""
    push = (
        DailiantongPush.objects.filter(
            order=order,
            platform=platform,
            state=DailiantongPush.State.SENT,
            is_active=True,
        )
        .exclude(serial_no__isnull=True)
        .exclude(serial_no="")
        .order_by("-id")
        .first()
    )
    if push is None:
        return False, ""
    if (
        push.remote_status in TERMINAL_REMOTE_STATUSES
        or push.remote_cancel_status in TERMINAL_REMOTE_CANCEL_STATUSES
    ):
        return True, "代练通这张单已经结束，无需撤回"
    try:
        reason = withdraw_push_for_machine(
            push,
            comment=INTERNAL_TAKER_WITHDRAW_COMMENT,
            # 空 / 10 / 18 也按「未接手」删除（业务方即时撤回口径）。
            allow_unaccepted_delete=True,
        )
    except DailiantongPushError as exc:
        return True, _note_internal_withdraw_failure(push, str(exc))
    except Exception as exc:  # noqa: BLE001 —— 未预期失败也只回提示，不影响本地指派
        return True, _note_internal_withdraw_failure(
            push, str(exc) or type(exc).__name__
        )
    return True, f"已撤回代练通订单：{reason}"


def zero_refund_push_for_machine(push: DailiantongPush) -> tuple[bool, str]:
    """**撞单 0 撤**（H3-1）：对一条「其它平台也接了单」的推送发起 0 撤。业务方口径（原话）：「若发生撞单（多个平台同时接手）时，系统会保留**最早接单**的那个，其它所有平台全部**申请 0 撤**」。**保留谁**由调用方（`dailiantong_push.zero_refund_conflicting_pushes`）按接单时刻决定，本函数只负责「把这一条撤掉」这一个原子动作。做法与分支（与人工撤回同一套写前 / 写后回读口径，⛔ 不拿「上游回了成功码」当成功）： 1. **写前回读** `LevelOrderDetail`：确认这张单现在确实处于 12 / 13，且撤销流程还没启动；
    2. `LevelOrderCancel(Flag=0, PayLevelBal=0, RepEnsureBal=0, Comment=CONFLICT_ZERO_REFUND_COMMENT)` —— **只调一次**（写动作不重试，真钱红线）； 3. **写后回读**：`CancelStatus != 10`（申请已登记）才算成功。返回 `(did_apply, 中文结论)`： - `(True, note)`：真的发起了 0 撤且回读核验通过 ⇒ 调用方记 `conflict_zero_refund_at`；
    - `(False, note)`：**已经不需要 / 不能再撤**（远端已结算 / 已强制撤销 / 撤销流程本身已经在走 / 状态不是 12·13）⇒ 调用方按「已收口」记一次，⛔ 不重试、⛔ 不算失败； - 抛 `DailiantongPushError`：真失败（上游明确拒 / 超时未知 / 回读没核验到），由调用方做有界退避。 ⚠️ **审计是机器来源**（`actor_id=None` + `system=True`，操作人写「代练通同步」）：这条 0 撤不是任何人类点的，⛔ 不冒充客服账号。 ⚠️ **不写 `cancel_requested_at`**：那是 D8「人工发起协商撤销 ⇒ 允许重发」的依据；撞单 0 撤是系统决定，⛔ 不该顺手把这张单放行到「可重发」（幂等键是本行自己的 `conflict_zero_refund_at`）。"""
    integration = _active_integration(push.platform)
    client = _open_client(integration)
    try:
        try:
            detail = _read_detail(client, push, action_name="撞单 0 撤")  # 写前回读
            status = _as_int(detail.get("Status"))
            cancel_status = _as_int(detail.get("CancelStatus"))

            if (
                status in TERMINAL_REMOTE_STATUSES
                or cancel_status in TERMINAL_REMOTE_CANCEL_STATUSES
            ):
                return False, (
                    "代练通这张单已经是终态"
                    f"（状态「{_status_label(status)}」/ 撤销码「{_status_label(cancel_status)}」），"
                    "不需要再申请 0 撤"
                )
            if cancel_status is not None and cancel_status != CANCEL_STATUS_NOT_CANCELLED:
                # 撤销流程已经在走（协商撤销中 / 已处理 / 客服介入中…）⇒ 再申请是重复动作。
                return False, (
                    f"代练通这张单的撤销流程已经在走（CancelStatus={cancel_status}），"
                    "不重复申请 0 撤"
                )
            if status not in CONFLICT_ZERO_REFUND_STATUSES:
                return False, (
                    f"代练通这张单当前状态是「{_status_label(status)}」，"
                    "不是「正在代练 / 等待验收」，不需要 0 撤"
                )

            try:
                client.cancel_order(
                    push.serial_no,
                    CANCEL_FLAG_APPLY,
                    pay_level_bal=CONFLICT_ZERO_REFUND_AMOUNT,
                    rep_ensure_bal=CONFLICT_ZERO_REFUND_AMOUNT,
                    comment=CONFLICT_ZERO_REFUND_COMMENT,
                )
            except DailiantongTransportError as exc:
                raise DailiantongPushError(
                    f"撞单 0 撤的结果未知（{exc.message}）。"
                    "请到代练通后台核对这张单的撤销状态，再决定是否重试。"
                ) from exc
            except DailiantongError as exc:
                raise DailiantongPushError(
                    f"代练通没有接受这次撞单 0 撤申请（上游提示：{exc.message}）。"
                ) from exc

            detail_after = _readback_detail(client, push, action_name="撞单 0 撤")
            cancel_after = _as_int(detail_after.get("CancelStatus"))
            if cancel_after is None or cancel_after == CANCEL_STATUS_NOT_CANCELLED:
                raise DailiantongPushError(
                    "已提交撞单 0 撤，但回读代练通的撤销状态仍是「未撤销」，没能核验到。"
                    "请稍后刷新状态、或到代练通后台核对。"
                )
            _save_remote_snapshot(push, detail_after)
        except DailiantongPushError as exc:
            # 失败也留痕（与人工撤回 / A4 机器撤回同一条口径）：页面与事后追查都靠它。
            _record_action(
                push,
                push.order,
                None,
                action=AUDIT_WITHDRAW,
                reason=f"撞单 0 撤失败：{exc}",
                system=True,
            )
            raise
    finally:
        _close(client)

    note = (
        "撞单 0 撤已发起并回读核验"
        f"（CancelStatus={cancel_after}，订单状态「{_status_label(status)}」→"
        f"「{_status_label(_as_int(detail_after.get('Status')))}」）"
    )
    _record_action(
        push,
        push.order,
        None,
        action=AUDIT_WITHDRAW,
        reason=f"{note}；事由：{CONFLICT_ZERO_REFUND_COMMENT}",
        system=True,
    )
    return True, note


# ── 改价 ────────────────────────────────────────────────────────────────


def modify_push_price(
    order,
    *,
    operator,
    price,
    platform: int = DEFAULT_PLATFORM,
) -> DailiantongPush:
    """改代练通订单价格（`POST …/platform-push/price/` 的服务层）。 ⛔ **只有未接手（`Status=11`）能改**：实测已接手的单回
    `-1 只有未接手状态才能修改价格`，所以本地先挡（零写调用），把上游文案留给真异常。成败判据 = 回读 `Price` **必须等于新价**；核验不到 / 不相等 ⇒ 中文错误、⛔ 不改本地行。
    核验通过后同步本地推送行的 `price`（页面读的就是这一列）。"""
    _require_operator(operator)
    try:
        price = _to_decimal(price)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DailiantongPushError("改价金额不是有效数字。") from exc
    if price < MIN_PRICE:
        # ⭐ P2-9：与发单同一条下限。⚠️ 实测：有的区服最低 5 元，但球球大作战 /
        # 无限暖暖 2 元可发 ⇒ 全局下限仍是 2 元，区服粒度交给上游回中文原文，⛔ 不本地一律抬到 5。
        raise DailiantongPushError(
            f"改价不能低于 {_fmt_money(MIN_PRICE)} 元（代练通实测最低 2 元），"
            f"当前填的是 {_fmt_money(price)} 元。"
        )

    push = _latest_active_push(order, platform)
    integration = _active_integration(platform)
    client = _open_client(integration)
    old_price_text = _fmt_money(push.price)
    try:
        detail = _read_detail(client, push, action_name="改价")
        status = _as_int(detail.get("Status"))
        if status not in REMOTE_PRICE_EDITABLE_STATUSES:
            raise DailiantongPushError(
                "只有未接手的代练通订单才能改价，"
                f"这张单当前状态是「{_status_label(status)}」。"
            )

        # ⭐ P2-9：改价也要过余额闸（复用发单那一套，提示带差额）。
        # ⭐ A1（计划 Rev.9 §1，实测）：改价后的冻结额**只有代练费**（两份保证金由打手侧
        #    出、不占我方余额）⇒ `need` 只等于新价，⛔ 不再加 `ensure1 / ensure2`。
        _check_active_balance(client, need=price)

        old_price = detail.get("Price")
        if old_price is not None:
            try:
                old_price_text = _fmt_money(old_price)
            except (InvalidOperation, TypeError, ValueError):
                # 详情里的旧价解析不了时，审计退回本地行价格（⛔ 不编一个数）。
                pass

        try:
            client.modify_price(push.serial_no, price)
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"改价结果未知（{exc.message}）。"
                "请到代练通后台核对这张单的价格，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次改价（上游提示：{exc.message}）。"
            ) from exc

        detail_after = _readback_detail(client, push, action_name="改价")
        actual = detail_after.get("Price")
        if not _money_equal(actual, price):
            raise DailiantongPushError(
                "已提交改价，但回读代练通的价格是"
                f"「{actual if actual is not None else '空'}」，"
                f"与新价「{_fmt_money(price)}」不一致，没能核验到。请人工核对。"
            )

        # 回读通过后才同步本地行（页面读的就是这一列）。
        push.price = price
        _save_remote_snapshot(push, detail_after, extra_fields=("price",))
    except DailiantongPushError as exc:
        _record_action(
            push,
            order,
            operator,
            action=AUDIT_PRICE,
            reason=str(exc),
            from_value=old_price_text,
            to_value=_fmt_money(price),
        )
        raise
    finally:
        _close(client)

    _record_action(
        push,
        order,
        operator,
        action=AUDIT_PRICE,
        reason="回读 Price 已等于新价",
        from_value=old_price_text,
        to_value=_fmt_money(price),
    )
    return push


# ── 补时 ────────────────────────────────────────────────────────────────


def adjust_push_time(
    order,
    *,
    operator,
    hours,
    platform: int = DEFAULT_PLATFORM,
) -> DailiantongPush:
    """给代练通订单补时（`POST …/platform-push/time/` 的服务层）。 `hours` 是**增量**（实测「补时 +2 小时」把 `TimeLimit` 从 24 变成 26），
    所以回读判据是 `新 TimeLimit == 原 TimeLimit + hours`。 ⚠️ 补时**不要支付密码**（只有补款 `kind=0` 才要）；实测补时**不推回调**
    ⇒ 必须回读详情才算数。"""
    _require_operator(operator)
    try:
        hours = int(hours)
    except (TypeError, ValueError) as exc:
        raise DailiantongPushError("补时小时数必须是整数。") from exc
    if hours < TIME_MIN_HOURS or hours > TIME_MAX_HOURS:
        raise DailiantongPushError(
            f"补时小时数必须在 {TIME_MIN_HOURS}–{TIME_MAX_HOURS} 之间。"
        )

    push = _latest_active_push(order, platform)
    integration = _active_integration(platform)
    client = _open_client(integration)
    old_limit = None
    try:
        detail = _read_detail(client, push, action_name="补时")
        # ⭐ P2-8：写前统一终态闸（已结算 / 已强制撤销 ⇒ 零写调用）。
        _guard_terminal(detail, action_name="补时")
        old_limit = _as_int(detail.get("TimeLimit"))
        if old_limit is None:
            raise DailiantongPushError(
                "代练通详情里没有当前时限，不能补时。请稍后重试、或到代练通后台核对。"
            )

        try:
            client.modify_special(
                push.serial_no, kind=SPECIAL_KIND_TIME, val=hours
            )
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"补时结果未知（{exc.message}）。"
                "请到代练通后台核对这张单的时限，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次补时（上游提示：{exc.message}）。"
            ) from exc

        detail_after = _readback_detail(client, push, action_name="补时")
        new_limit = _as_int(detail_after.get("TimeLimit"))
        expected = old_limit + hours
        if new_limit != expected:
            raise DailiantongPushError(
                "已提交补时，但回读代练通的时限是"
                f"「{new_limit if new_limit is not None else '空'}」小时，"
                f"预期「{expected}」小时，没能核验到。请人工核对。"
            )

        # 回读通过后才同步本地行（页面读的就是这一列）。
        push.time_limit_hours = expected
        _save_remote_snapshot(
            push, detail_after, extra_fields=("time_limit_hours",)
        )
    except DailiantongPushError as exc:
        _record_action(
            push,
            order,
            operator,
            action=AUDIT_TIME,
            reason=str(exc),
            from_value="" if old_limit is None else str(old_limit),
            to_value="" if old_limit is None else str(old_limit + hours),
        )
        raise
    finally:
        _close(client)

    _record_action(
        push,
        order,
        operator,
        action=AUDIT_TIME,
        reason="回读 TimeLimit 已等于原时限加补时小时数",
        from_value=str(old_limit),
        to_value=str(old_limit + hours),
    )
    return push


# ── 留言 ────────────────────────────────────────────────────────────────


def add_push_message(
    order,
    *,
    operator,
    msg: str,
    platform: int = DEFAULT_PLATFORM,
) -> DailiantongPush:
    """给代练通订单发一条留言（`POST …/platform-push/message/` 的服务层）。
    判据 = 上游**成功码**（本任务明确：留言有延迟，⛔ 不强求回读）。 ⚠️ 它的回调 `Status=0`、不带订单状态（§6.2 结论 2）⇒ 本动作**不碰**本地状态。"""
    _require_operator(operator)
    text = (msg or "").strip()
    if not text:
        raise DailiantongPushError("留言内容不能为空。")
    if len(text) > MESSAGE_MAX_LENGTH:
        raise DailiantongPushError(
            f"留言内容不能超过 {MESSAGE_MAX_LENGTH} 个字符。"
        )

    push = _latest_active_push(order, platform)
    integration = _active_integration(platform)
    client = _open_client(integration)
    try:
        # ⭐ P2-8：留言此前没有写前回读；已结算 / 已强制撤销的单仍会真发出去。
        detail = _read_detail(client, push, action_name="留言")
        _guard_terminal(detail, action_name="留言")
        try:
            client.message_add(push.serial_no, text)
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"发留言的结果未知（{exc.message}）。"
                "请稍后到代练通后台核对这条留言是否已发出。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这条留言（上游提示：{exc.message}）。"
            ) from exc
    except DailiantongPushError as exc:
        _record_action(push, order, operator, action=AUDIT_MESSAGE, reason=str(exc))
        raise
    finally:
        _close(client)

    _record_action(
        push,
        order,
        operator,
        action=AUDIT_MESSAGE,
        reason="上游回成功码（留言有延迟，不强制回读）",
    )
    return push


# ── 确认完单（C1）────────────────────────────────────────────


def accept_push(
    order,
    *,
    operator,
    platform: int = DEFAULT_PLATFORM,
) -> DailiantongPush:
    """平台确认结算后，同步订单完成及打手结账状态。回写失败由轮询补齐。"""
    _require_operator(operator)
    push = _latest_active_push(order, platform)
    integration = _active_integration(platform)
    client = _open_client(integration)
    try:
        # 写前回读：既确认状态，也避免把「已经结算过」的单再完单一次。
        detail = _read_detail(client, push, action_name="确认完单")
        _guard_terminal(detail, action_name="确认完单")
        status = _as_int(detail.get("Status"))
        if status != REMOTE_STATUS_WAITING_ACCEPTANCE:
            raise DailiantongPushError(
                "只有「等待验收」的代练通订单才能确认完单，"
                f"当前远端状态：{_status_label(status)}。"
            )
        try:
            client.finish_order(push.serial_no)
        except DailiantongTransportError as exc:
            # ⛔ 不知道对方收没收到 ⇒ 按「未知」说清楚，不重试（重试可能扣两次钱）。
            raise DailiantongPushError(
                f"确认完单的结果未知（{exc.message}）。"
                "请先「同步交易状态」、或到代练通后台核对是否已完单，再决定要不要重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受确认完单（上游提示：{exc.message}）。"
            ) from exc
        after = _readback_detail(client, push, action_name="确认完单")
        after_status = _as_int(after.get("Status"))
        if after_status != REMOTE_STATUS_SETTLED:
            raise DailiantongPushError(
                "已提交确认完单，但没能核验到结果"
                f"（回读到的远端状态：{_status_label(after_status)}）。"
                "请稍后「同步交易状态」、或到代练通后台核对。"
            )
        _save_remote_snapshot(push, after)
        sync_note = push_module.apply_remote_status_after_readback(push, finalize_terminal=True)
    except DailiantongPushError as exc:
        _record_action(push, order, operator, action=AUDIT_ACCEPT, reason=str(exc))
        raise
    finally:
        _close(client)

    order.refresh_from_db(fields=["status"])
    if push.last_error or sync_note:
        reason = f"代练通已结算，团团通订单待同步（{push.last_error or sync_note}）"
    elif order.status == OrderStatus.COMPLETED:
        reason = "代练通已结算，团团通订单已完成"
    else:
        reason = "代练通已结算"
    _record_action(
        push,
        order,
        operator,
        action=AUDIT_ACCEPT,
        reason=reason,
        to_value=_fmt_money(push.price),
    )
    return push


# ── 手动同步交易状态（C1）────────────────────────────────────


def sync_push(
    order,
    *,
    operator,
    platform: int = DEFAULT_PLATFORM,
) -> DailiantongPush:
    """手动同步一张单的远端交易状态（第三方平台订单管理页的「同步交易状态」）。实现 = **直接复用** `dailiantong_push.refresh_push_status`（与兜底轮询、回调强制回读
    同一个出口）⇒ 写回 `remote_status` / `remote_cancel_status` / `last_synced_at` / `last_error` / `is_active` 的口径只有一份。本函数只多做两件事：
    ① 没有有效推送行 / 没有上游单号 ⇒ 中文业务错误（⛔ 不去上游白打一发）； ② 写一条「谁在什么时候手动同步了、状态从什么变成什么」的审计（轮询那条路不记审计，手动这类**人工纠正**必须能查）。 ⚠️ 回读失败**不报成功**（异常原样冒泡），与 `refresh_push_status` 自己的口径一致。"""
    _require_operator(operator)
    push = _latest_active_push(order, platform)
    before = push.remote_status
    before_label = _status_label(before)
    # `refresh_push_status` 自己处理配置 / 回读 / 落库；失败时已把上游原文写进 `last_error` 并抛出 ⇒ 这里只负责翻成页面能读的中文。
    # ⚠️ **客户端从本模块的构造点开**（`_open_client`）：同一份只读能力在这里多一次「取配置」不划算？——不，这是**唯一能被测试打桩的构造点**（`_patched_client` 打在
    # `dailiantong_actions._open_client`）；不注入它，`refresh_push_status` 会自己另开一个真客户端（测试会真打上游，违反「测试不访问真实网络」的红线）。
    integration = _active_integration(platform)
    client = _open_client(integration)
    try:
        try:
            push = push_module.refresh_push_status(push, client=client)
        except DailiantongPushError as exc:
            _record_action(
                push,
                order,
                operator,
                action=AUDIT_SYNC,
                reason=f"同步失败：{exc}",
                from_value=before_label,
                to_value=before_label,
            )
            raise
        except DailiantongError as exc:
            _record_action(
                push,
                order,
                operator,
                action=AUDIT_SYNC,
                reason=f"同步失败（上游提示：{exc.message}）",
                from_value=before_label,
                to_value=before_label,
            )
            raise DailiantongPushError(
                f"同步交易状态失败（上游提示：{exc.message}）。请稍后重试。"
            ) from exc
    finally:
        _close(client)

    after_label = _status_label(push.remote_status)
    _record_action(
        push,
        order,
        operator,
        action=AUDIT_SYNC,
        reason="手动同步（回读成功）",
        from_value=before_label,
        to_value=after_label,
    )
    return push


# ── 「已抛单禁止手改系统状态」的闸（C1）──────────────────────


def manual_status_denial(order, *, platform: int = DEFAULT_PLATFORM) -> str | None:
    """**手改系统状态**的闸：已抛到代练通且远端未结束 ⇒ 返回中文拒绝原因（`None` = 放行）。口径（计划 Rev.9 §2「状态必须与代练通一致」「已抛到代练通的单，禁止手工改状态」）： - **没有有效推送行**（没抛过 / 已关闭 / 发送失败）⇒ 放行； - 有有效推送行、且远端已到终态（`Status=17` 已结算 / `CancelStatus` 12/15/16 撤销已完成）⇒ 放行
    （链路已经结束，本地怎么收尾不再与远端冲突）； - **其余一律拒绝**——包括「还没回读到远端状态」这一档：拿不出「远端已结束」的证据时，手改就可能造成两边不一致（`AGENTS.md` §5-E：否定性结论必须有两路证据，这里没有）。 ⚠️ **落地位置是「本页的批量入口」**，不是状态机核心 `apply_transition`：A 线（状态回写）
    合入后这道闸应当**搬进 `apply_transition`**，让所有路径共用一份判据（届时本函数改调那边、⛔ 不要两处各写一份）。今天若只挂在本页，其它页面的手改入口仍会绕过它 —— 如实登记为已知边界（见 `.superpowers/reports/rev9-c.md`）。"""
    push = (
        DailiantongPush.objects.filter(
            order=order,
            platform=platform,
            state__in=DailiantongPush.ACTIVE_STATES,
        )
        .order_by("-id")
        .first()
    )
    if push is None:
        return None
    if (
        push.remote_status in TERMINAL_REMOTE_STATUSES
        or push.remote_cancel_status in TERMINAL_REMOTE_CANCEL_STATUSES
    ):
        return None
    return (
        "这张单已抛到代练通且远端尚未结束"
        f"（当前远端状态：{_status_label(push.remote_status)}），不能手工改系统状态；"
        "请先用平台动作撤回 / 确认完单，或「同步交易状态」确认远端结果后再改。"
    )


# ── P3：从**所有平台**撤回（管理订单页行内那颗按钮）─────────────


def platform_label(platform: int) -> str:
    """平台 id 的中文名（解析不了就给 `平台 <id>`，⛔ 不抛异常）。"""
    try:
        return str(PlatformIntegration.Platform(int(platform)).label)
    except (ValueError, TypeError):
        return f"平台 {platform}"


def _latest_push_with_serial(order, platform: int) -> DailiantongPush:
    """该单在该平台**最新一条有上游单号的推送行**（⛔ 不限 `ACTIVE_STATES`：只读用）。"""
    push = (
        DailiantongPush.objects.filter(order=order, platform=platform)
        .exclude(serial_no__isnull=True)
        .exclude(serial_no="")
        .order_by("-id")
        .first()
    )
    if push is None:
        raise DailiantongPushError(
            "这张单还没有抛到代练通（没有有效的推送记录），不能做平台读取。"
        )
    return push


def withdraw_all_platforms(
    order,
    *,
    operator,
    comment: str | None = None,
    pay_level_bal=None,
    rep_ensure_bal=None,
) -> dict:
    """P3：「从所有平台撤回」——一次性把该单在**每个平台**的推送撤掉。 ## 返回（键名即前端契约） ```json {"results": [{"platform": 10, "platform_label": "代练通", "serial_no": "…", "ok": true, "state": "closed", "message": "未接手订单已删除（即时退款）"}],
    "succeeded": 1, "failed": 0, "total": 1} ``` ## 口径（逐条都是业务方 的要求） - **没有推送** ⇒ 抛 `DailiantongPushError("这张单没有抛到任何平台…")`（⛔ 不假装成功）； - **逐平台回结果**：一个平台失败不影响其它平台，`ok` / `message` 如实写；
    - 每个平台走的都是**同一份**撤回原语（`withdraw_push`）：未接手 ⇒ 删除并即时退款；已接手 ⇒ 申请协商撤销（三格由页面给，见 `withdraw_push` 的 docstring）； - **部分成功**也返回 `code=0`（`succeeded/failed` 如实计数），由调用方决定怎么展示； ⛔ 不把「一个成功一个失败」说成整单成功； - 本版只有代练通有客户端：其它平台的推送**逐条**回中文原因（⛔ 不静默跳过、 ⛔ 也不拿代练通凭据去操作别的平台）。"""
    _require_operator(operator)
    pay_level_bal = _optional_money(pay_level_bal, label="支付代练费")
    rep_ensure_bal = _optional_money(rep_ensure_bal, label="赔偿保证金")

    # 每个平台取**最新一条有效推送**（部分唯一索引保证同平台至多一条，这里仍按最新取，
    # 只为在约束上线前的存量数据上也确定）。
    targets: dict[int, DailiantongPush] = {}
    pushes = (
        DailiantongPush.objects.filter(
            order=order, state__in=DailiantongPush.ACTIVE_STATES
        )
        .order_by("platform", "-id")
    )
    for push in pushes:
        targets.setdefault(int(push.platform), push)
    if not targets:
        raise DailiantongPushError("这张单没有抛到任何平台，没有可撤回的推送。")

    results: list[dict] = []
    for platform in sorted(targets):
        push = targets[platform]
        entry = {
            "platform": platform,
            "platform_label": platform_label(platform),
            "serial_no": (push.serial_no or "").strip(),
            "ok": False,
            "state": push.state,
            "message": "",
        }
        if platform != DEFAULT_PLATFORM:
            entry["message"] = (
                f"本系统目前只对接了代练通，平台「{entry['platform_label']}」的推送"
                "暂时不能从这里撤回，请到该平台后台手工处理。"
            )
            results.append(entry)
            continue
        try:
            updated = withdraw_push(
                order,
                operator=operator,
                comment=comment,
                platform=platform,
                pay_level_bal=pay_level_bal,
                rep_ensure_bal=rep_ensure_bal,
            )
        except DailiantongPushError as exc:
            entry["message"] = str(exc)
        else:
            entry["ok"] = True
            entry["state"] = updated.state
            entry["serial_no"] = (updated.serial_no or "").strip() or entry["serial_no"]
            entry["message"] = (
                "撤回已完成：订单已删除并即时退款"
                if updated.state == DailiantongPush.State.CLOSED
                else "已提交协商撤销，等待打手 / 客服处理"
            )
        results.append(entry)

    succeeded = sum(1 for row in results if row["ok"])
    return {
        "results": results,
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "total": len(results),
    }


# ── 只读：平台留言 / 进度图（详情页）──────────────────────────


#: 一次读取多少条留言（上游 `Max`；文档上限 500 ——
#: `docs/references/dailiantong/open-api-v3.4.md`「获取订单留言」明写「不能超过500」）。
#: 业务方：详情页要看到**全部**留言 ⇒ 取文档上限。
MESSAGE_LIST_MAX = 500
#: 一次读取多少条进度图（同上）。
PROGRESS_LIST_MAX = 100

#: 留言里「系统自动回复」的标记片段（与 `dailiantong_qr` 发出去的标记同一族）。
_SYSTEM_MESSAGE_MARKERS = ("（系统自动回复·订单 ", "（系统自动跟进·订单 ")

#: 上游 `MsgType` 的取值（`docs/references/dailiantong/open-api-v3.4.md`「获取订单留言」）：
#: 10 = 系统消息、11 = 用户消息。⚠️ 我们自己的「系统自动回复」是经**用户消息通道** 发出去的（`MsgType=11`）⇒ 判 `is_system` 时两个判据取**或**，⛔ 只看 `MsgType`
#: 会把我们发出去的自动回复在页面上标成「对方说的」。
_MSG_TYPE_SYSTEM = 10


def _row_value(row: dict, *names: str) -> str:
    """在留言行里按候选键名取值（大小写不保证；取不到给空串）。"""
    wanted = {name.strip().lower() for name in names}
    for key, value in row.items():
        if str(key).strip().lower() in wanted:
            return str(value or "").strip()
    return ""


def _row_text(row: dict) -> str:
    """留言 / 进度行里的**正文**（键名大小写不保证；取不到给空串）。"""
    for key, value in row.items():
        if str(key).strip().lower() in ("msg", "message", "content", "text"):
            return str(value or "")
    return ""


def _row_sender(row: dict) -> str:
    """留言的**发布方**：`OperationID`（接单者 / 发单者 / 客服）优先，其次昵称、UID。
    ⚠️ 原样返回上游原文，⛔ 不翻译、⛔ 不猜（上游没给就给空串）。"""
    return (
        _row_value(row, "OperationID")
        or _row_value(row, "NickName")
        or _row_value(row, "UID")
    )


def _row_is_system(row: dict, text: str) -> bool:
    """这条留言是不是「系统侧」的（`MsgType=10` **或** 正文带我们自己的自动回复标记）。"""
    raw_type = _row_value(row, "MsgType")
    if raw_type:
        try:
            if int(raw_type) == _MSG_TYPE_SYSTEM:
                return True
        except ValueError:      # 上游给了非数字 ⇒ 交给标记那一路判，⛔ 不据此判系统
            pass
    return any(marker in text for marker in _SYSTEM_MESSAGE_MARKERS)


def _row_time(row: dict) -> str:
    """留言 / 进度行里的**时间**（上游列的键名不统一；取不到给空串，⛔ 不编一个）。"""
    for key, value in row.items():
        if str(key).strip().lower() in (
            "createdate",
            "adddate",
            "addtime",
            "createdtime",
            "time",
            "datetime",
        ):
            return str(value or "")
    return ""


def _row_image(row: dict) -> str:
    """进度行里的图片值（上游可能给对象名，也可能给完整 URL）。"""
    for key, value in row.items():
        if str(key).strip().lower() in ("img", "image", "pic", "url"):
            return str(value or "")
    return ""


def _image_url(value: str) -> str:
    """把上游图片值变成可点的 URL：**已经是 http(s) 就原样**，否则给空串。 ⚠️ 上游 `Img` 可能只是 OSS 对象名（`tt/<schema>/<单号>/…`）。我们没有该 bucket 的
    下载签名能力（`GetSTS` 只给上传凭证）⇒ 对象名**不假装**能点开，`url` 留空，
    `img` 原样回给页面（排障时能看出是哪一张）。"""
    text = (value or "").strip()
    if text.lower().startswith(("http://", "https://")):
        return text
    return ""


def _progress_items(rows) -> list[dict]:
    """把上游进度行映射成 `{time, caption, url, img}`。留言读面（`images`）与进度读面（`progress`）**同源**：上游 `LevelOrderMessageList`
    没有图片字段（`docs/references/dailiantong/open-api-v3.4.md:675-698`），图片在
    `LevelOrderProgressList` ⇒ 两个出口必须用这一份映射，⛔ 不要各写一遍。"""
    items = []
    for row in rows:
        image = _row_image(row)
        items.append(
            {
                "time": _row_time(row),
                "caption": _row_text(row),
                "img": image,
                "url": _image_url(image),
            }
        )
    return items


def read_push_messages(order, *, platform: int = DEFAULT_PLATFORM) -> dict:
    """详情页「平台留言」列表（`GET …/platform-push/messages/` 的服务层）。返回 `{"messages": [...], "images": [...], "images_error": "", "error": ""}`： ```json {"messages": [{"time": "2026-10-27 10:00:00", "text": "…", "is_system": false, "sender": "接单者", "uid": "USR-…", "nickname": "…"}], "images": [{"time": "…", "caption": "…",
    "img": "tt/x/order/qr-1.png", "url": "https://…"}], "images_error": "", "error": ""} ``` - **对方 / 打手的留言也在里面**：上游 `LevelOrderMessageList` 不按发布方过滤（`MsgType` 10 系统 / 11 用户，`OperationID` 接单者 / 发单者 / 客服）； - `is_system` = `MsgType=10` **或** 正文里带**我们自己的系统标记** （接手后发送 / 自动回复——它们经用户消息通道发出，所以两个判据取或）；
    - `sender` = 发布方原文（`OperationID` → `NickName` → `UID`；都没有 ⇒ 空串）； `uid` / `nickname` 是上游原值（没有就空串）； - `text` **原样**返回：带「系统自动回复」标记时 ⛔ 不过滤、不裁剪； - **图片**：上游留言 API **没有**图片字段 ⇒ `images` 取同一单的 `LevelOrderProgressList`（与 `read_push_progress` 同一个映射）。图片读失败**不拖累留言**：`images=[]` + `images_error` 给中文原因； - ⛔ **只读**：不写库、不改状态、不发任何上游写动作； - **失败不 500**：没有推送行 / 上游业务失败 / 传输失败 ⇒ `messages=[]` + `error` 是可读中文（页面显示原因，⛔ 不把「读不到」说成「没有留言」）。"""
    try:
        push = _latest_push_with_serial(order, platform)
        integration = _active_integration(platform)
    except DailiantongPushError as exc:
        return {
            "messages": [],
            "images": [],
            "images_error": "",
            "error": str(exc),
        }

    client = _open_client(integration)
    try:
        try:
            page = client.message_list(push.serial_no, max_count=MESSAGE_LIST_MAX)
        except DailiantongError as exc:
            return {
                "messages": [],
                "images": [],
                "images_error": "",
                "error": f"读取代练通留言失败（上游提示：{exc.message}）。",
            }
        try:
            progress_page = client.progress_list(
                push.serial_no, max_count=PROGRESS_LIST_MAX
            )
        except DailiantongError as exc:
            images = []
            images_error = f"读取代练通进度图失败（上游提示：{exc.message}）。"
        else:
            images = _progress_items(progress_page.rows)
            images_error = ""
    finally:
        _close(client)

    messages = []
    for row in page.rows:
        text = _row_text(row)
        # ⚠️ `text` **原样**返回：上游内容里带我们自己的「系统自动回复」标记时
        # ⛔ 不过滤、不裁剪（页面/排障都要看得到原文）。
        messages.append(
            {
                "time": _row_time(row),
                "text": text,
                "is_system": _row_is_system(row, text),
                # ⭐ FX3：发布方（接单者 / 发单者 / 客服）；上游没给 ⇒ 空串。
                "sender": _row_sender(row),
                "uid": _row_value(row, "UID"),
                "nickname": _row_value(row, "NickName"),
            }
        )
    return {
        "messages": messages,
        "images": images,
        "images_error": images_error,
        "error": "",
    }


def read_push_progress(order, *, platform: int = DEFAULT_PLATFORM) -> dict:
    """详情页「进度 / 截图」列表（`GET …/platform-push/progress/` 的服务层）。返回 `{"progress": [...], "error": ""}`： ```json
    {"progress": [{"time": "2026-10-27 10:00:00", "caption": "…", "img": "tt/x/order/qr-1.png", "url": "https://…"}], "error": ""}
    ``` - `img` = 上游原值（对象名或 URL）；`url` 只在**已经是 http(s)** 时给（见 `_image_url`）； - ⛔ **只读**、**失败不 500**（口径同 `read_push_messages`）。"""
    try:
        push = _latest_push_with_serial(order, platform)
        integration = _active_integration(platform)
    except DailiantongPushError as exc:
        return {"progress": [], "error": str(exc)}

    client = _open_client(integration)
    try:
        try:
            page = client.progress_list(push.serial_no, max_count=PROGRESS_LIST_MAX)
        except DailiantongError as exc:
            return {
                "progress": [],
                "error": f"读取代练通进度图失败（上游提示：{exc.message}）。",
            }
    finally:
        _close(client)

    progress = _progress_items(page.rows)
    return {"progress": progress, "error": ""}


# ── P7-详情：重发一条失败 / 已关闭的推送────────────────────


def repush_failed_push(
    order,
    *,
    operator,
    platform: int = DEFAULT_PLATFORM,
    price=None,
    ensure1=None,
    ensure2=None,
    time_limit_hours=None,
    zone_server_id: str | None = None,
    memo: str | None = None,
    groups: str | None = None,
    label1: str | None = None,
    label2: str | None = None,
    requirement: str | None = None,
    hall_price_same=None,
):
    """把一条**失败 / 已关闭**的推送重新抛一次（`POST …/platform-push/repush/`）。 ⛔ **不绕开五道闸**：本函数最后就是调 `push_order`（配置 → 订单状态 → 价格 → 已有推送 → 余额，一道不少），本函数只做三件事： 1. **选哪条旧推送**：该单该平台最新一条；必须是 `failed`（上游明确失败）或
    `closed`（远端已删除 / 已撤回）——`pending` / `sent` / `unknown` 一律 `DailiantongPushError`（重复抛 = 真钱重复建单，`docs/DAILIANTONG-API.md` §9 R11）； 2. **补默认值**：页面没传的 `price` / `ensure1` / `ensure2` / `time_limit_hours`
    取**上一条推送行**的值；`zone_server_id` 取 B5 的默认区服（映射优先 → 游戏名首匹配）； 3. 写一条 `order.dailiantong_repush` 审计（重发这件事本身要能查：谁、从哪条旧行、结果是什么），然后返回 `push_order` 的新推送行。 ⚠️ 返回的是**新链路行**（旧行原样保留）。失败分层与 `push_order` 完全一致。"""
    _require_operator(operator)
    # 闸：已有有效推送 ⇒ 中文拒绝（与 `push_order` 的闸 ④ 同一份文案/判据）。
    push_module._raise_if_already_pushed(order, platform)

    previous = (
        DailiantongPush.objects.filter(order=order, platform=platform)
        .order_by("-id")
        .first()
    )
    if previous is None:
        raise DailiantongPushError(
            "这张单还没有抛过代练通，没有可重发的推送；请直接发单。"
        )
    if previous.state not in (
        DailiantongPush.State.FAILED,
        DailiantongPush.State.CLOSED,
    ):
        raise DailiantongPushError(
            f"只有「发送失败」或「已关闭」的推送才能重发，"
            f"这条推送当前是「{previous.get_state_display()}」。"
        )

    if price is None:
        price = previous.price
    if ensure1 is None:
        ensure1 = previous.ensure1
    if ensure2 is None:
        ensure2 = previous.ensure2
    if time_limit_hours is None:
        time_limit_hours = previous.time_limit_hours
    selected = (zone_server_id or "").strip()
    if not selected:
        try:
            zone = push_module.default_zone_for_game(order.game_id)
        except (DailiantongPushError, DailiantongError) as exc:
            raise DailiantongPushError(
                f"没能取到默认代练通区服（{exc}），请手动选择区服后再重发。"
            ) from exc
        selected = (zone.get("zone_server_id") or "").strip()
        if not selected:
            raise DailiantongPushError(
                "没能自动匹配到代练通区服，请手动选择区服后再重发。"
            )

    try:
        new_push = push_module.push_order(
            order,
            price=price,
            ensure1=ensure1,
            ensure2=ensure2,
            time_limit_hours=time_limit_hours,
            zone_server_id=selected,
            operator=operator,
            platform=platform,
            memo=memo,
            groups=groups,
            label1=label1,
            label2=label2,
            requirement=requirement,
            hall_price_same=hall_price_same,
        )
    except DailiantongPushError as exc:
        _record_action(
            previous,
            order,
            operator,
            action=AUDIT_REPUSH,
            reason=f"重发被拒绝：{exc}",
            from_value=previous.get_state_display(),
        )
        raise

    _record_action(
        previous,
        order,
        operator,
        action=AUDIT_REPUSH,
        reason=(
            f"重发这条推送（旧行 #{previous.pk}，"
            f"{'旧上游单号：' + previous.serial_no if previous.serial_no else '旧行没有上游单号'}；"
            f"新行 #{new_push.pk}，{new_push.get_state_display()}"
            f"{('，上游单号：' + new_push.serial_no) if new_push.serial_no else ''}）"
        ),
        from_value=previous.get_state_display(),
        to_value=new_push.get_state_display(),
    )
    return new_push


# ── 读面：申请撤销弹窗的默认金额（前端契约）──────────────────

#: 两个金额的读面缓存时长（秒）。页面刷新 / 打开撤销弹窗会重复读同一张单，
#: 30 秒足够挡住抖动，又不至于让操作人看到很旧的值。
WITHDRAW_DEFAULTS_CACHE_TTL_SECONDS = 30.0

_withdraw_defaults_lock = threading.Lock()
#: `(schema, 上游单号) -> (抓取时刻 monotonic, 结果 dict)`。
_withdraw_defaults_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def clear_withdraw_defaults_cache() -> None:
    """清空撤销默认金额缓存（测试用；生产不需要手工清）。"""
    with _withdraw_defaults_lock:
        _withdraw_defaults_cache.clear()


def withdraw_defaults_for_push(push: DailiantongPush) -> dict:
    """一条推送行「申请撤销」两个金额的**默认值**（详情现值）。返回 `{"pay_level_bal": "2.00"|None, "rep_ensure_bal": "0.00"|None, "withdraw_defaults_error": ""}`：前端拿它回填撤销弹窗的两个输入框；
    读不到时 `error` 是中文原因（页面留空要求手填，⛔ 不猜一个金额）。 ⚠️ **只读**：不写库、不发写上游；唯一上游调用是 `LevelOrderDetail`，30 秒缓存。 ⚠️ 没有上游单号 / 推送已结束（`failed` / `closed`）⇒ 不去上游白打一发，
    直接给 `null` + 说明（那种行本来也不该弹「申请撤销」）。 ⚠️ 凭据红线：只取 `PayLevelBal` / `RepEnsureBal` 两个标量，⛔ 不碰详情里的明文密码。"""
    result = {
        "pay_level_bal": None,
        "rep_ensure_bal": None,
        "withdraw_defaults_error": "",
    }
    serial = (push.serial_no or "").strip()
    if not serial:
        result["withdraw_defaults_error"] = (
            "这条推送还没有上游订单号，申请撤销的两个金额只能手填。"
        )
        return result
    if push.state not in DailiantongPush.ACTIVE_STATES:
        result["withdraw_defaults_error"] = (
            "这条推送已经结束，申请撤销的两个金额只能手填。"
        )
        return result

    schema = (getattr(db_connection, "schema_name", "") or "").strip() or "public"
    cache_key = (schema, serial)
    with _withdraw_defaults_lock:
        cached = _withdraw_defaults_cache.get(cache_key)
    if cached is not None and time.monotonic() - cached[0] < WITHDRAW_DEFAULTS_CACHE_TTL_SECONDS:
        return dict(cached[1])

    try:
        integration = _active_integration(push.platform)
        client = _open_client(integration)
    except DailiantongPushError as exc:
        result["withdraw_defaults_error"] = f"{exc}（两个金额只能手填）"
        return result

    try:
        try:
            detail = client.order_detail(serial)
        except DailiantongError as exc:
            result["withdraw_defaults_error"] = (
                f"读取代练通详情失败（{exc.message}），两个金额只能手填。"
            )
            return result
    finally:
        _close(client)

    missing = False
    for field, wire_name in (
        ("pay_level_bal", "PayLevelBal"),
        ("rep_ensure_bal", "RepEnsureBal"),
    ):
        raw = detail.get(wire_name) if isinstance(detail, dict) else None
        if raw is None or str(raw).strip() == "":
            missing = True
            continue
        try:
            result[field] = _fmt_money(_to_decimal(raw))
        except (InvalidOperation, TypeError, ValueError):
            missing = True
    if missing:
        result["withdraw_defaults_error"] = (
            "代练通详情里没有完整的「支付代练费 / 赔偿保证金」，"
            "这两个金额请手动填写。"
        )

    with _withdraw_defaults_lock:
        _withdraw_defaults_cache[cache_key] = (time.monotonic(), dict(result))
    return result
