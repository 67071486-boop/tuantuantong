"""QQ 网关事件处理器。

⚠️ **调用方保证已在租户上下文内**（`worker` 的线程入口模板包了 `tenant_context`）；
本模块不做租户切换，只做事件到 ORM 的翻译。

原则：
- **单事件失败不断连**：`handle_dispatch` 的调用方（reader 线程）每条都包 try/except，
  本模块内部对**外部 API**（审批接口）也各自吞异常——网关事件处理不能被上游 API 拖死；
- **无论处置成败都留痕**：入群申请每次到达都会有一行 `QQBotJoinRequestLog`
  （成功 = `auto_*`；失败 = `failed` + `error_code`；不自动处理 = `manual_pending`）；
- **未知事件不抛**：官方会加新事件，老代码收到只 `logger.debug`（⫽ 留 debug 不是
  warning：未知事件是常态，不是故障）。
"""
from __future__ import annotations

import logging
from datetime import datetime as _dt
from datetime import timezone as _tz

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.qqbot import qqbot_api
from apps.qqbot.models import (
    QQBotBinding,
    QQBotGroup,
    QQBotJoinRequestLog,
)

logger = logging.getLogger(__name__)


def _touch_binding_event() -> None:
    """盖 `last_event_at`（运维判「这机器人还收不收得到事件」的锚）。失败不抛。

    ⚠️ 已知未改（评审 P3-6 登记）：每条事件都写一次库、没有节流——事件频率低时是
    可接受的小账，先不动。"""
    try:
        binding = QQBotBinding.get_solo()
        binding.last_event_at = timezone.now()
        binding.save(update_fields=["last_event_at", "updated_at"])
    except Exception:  # noqa: BLE001 - 健康列失败不该影响事件本身
        logger.debug("qqbot: 盖 last_event_at 失败", exc_info=True)


def _parse_moment(raw):
    """官方时间字段的宽容解析（epoch 秒 / ISO 文本 / 其它 ⇒ None）。

    ⚠️ 官方文档没钉死各事件的时刻格式（`apply_at` / `timestamp` 的实际形态要等真机
    联调核对，E-0005 口径）——解析失败落 `None`，⛔ 不让一个时间字段炸掉整条事件。
    """
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        return _dt.fromtimestamp(raw, tz=_tz.utc)
    text = str(raw)
    parsed = parse_datetime(text)
    if parsed is None and text.isdigit():
        return _dt.fromtimestamp(int(text), tz=_tz.utc)
    return parsed


def _upsert_group(group_openid: str, *, status: str | None = None, **fields) -> QQBotGroup:
    """按 openid 建或改群投影（`first_seen_at` 是 auto_now_add，只在新建时落值）。"""
    group, _created = QQBotGroup.objects.update_or_create(
        group_openid=group_openid, defaults=fields
    )
    if status is not None:
        group.status = status
        group.save(update_fields=["status"])
    return group


def handle_dispatch(t: str | None, d: dict | None) -> None:
    """分发表：`t`（事件名）→ 处理器；未知事件 debug 后返回。"""
    if not t:
        return
    handler = _HANDLERS.get(t)
    if handler is None:
        logger.debug("qqbot: 未登记的网关事件 %s，忽略", t)
        return
    handler(d or {})
    _touch_binding_event()


# ── 各事件的投影口径 ────────────────────────────────────────────────────────


def _on_group_add_robot(d: dict) -> None:
    """进群：投影置 `active`（回迁也走这里——gone 的群再进，一行改回 active）。"""
    openid = d.get("group_openid") or ""
    if not openid:
        return
    _upsert_group(
        openid,
        status=QQBotGroup.Status.ACTIVE,
        joined_at=_parse_moment(d.get("timestamp")),
    )


def _on_group_del_robot(d: dict) -> None:
    """被移出：`status=gone`。**不删行**——推送配置与历史留痕都挂着 FK。"""
    openid = d.get("group_openid") or ""
    if not openid:
        return
    group = QQBotGroup.objects.filter(group_openid=openid).first()
    if group is None:
        group = QQBotGroup.objects.create(group_openid=openid)
    group.status = QQBotGroup.Status.GONE
    group.save(update_fields=["status"])


def _on_group_msg_reject(d: dict) -> None:
    """管理员关了「允许机器人主动消息」⇒ 主动推送要停（`allow_proactive_msg=False`）。"""
    _set_flag(d, False)


def _on_group_msg_receive(d: dict) -> None:
    """管理员开了「允许机器人主动消息」⇒ `allow_proactive_msg=True`。"""
    _set_flag(d, True)


def _set_flag(d: dict, value: bool) -> None:
    openid = d.get("group_openid") or ""
    if not openid:
        return
    group, _ = QQBotGroup.objects.get_or_create(group_openid=openid)
    group.allow_proactive_msg = value
    group.save(update_fields=["allow_proactive_msg"])


def _on_group_at_message(d: dict) -> None:
    """群里说话：群投影 upsert + 盖活动时间（`last_seen_at` 是 auto_now，save 即盖）。

    ⚠️ 已知未改（评审 P3-6 登记）：这里是 `get_or_create` + `first` 两条查询，
    可收成一条——事件量小先不动。"""
    openid = d.get("group_openid") or ""
    if not openid:
        return
    QQBotGroup.objects.get_or_create(group_openid=openid)
    group = QQBotGroup.objects.filter(group_openid=openid).first()
    if group is not None:
        group.last_seen_at = timezone.now()
        group.save(update_fields=["last_seen_at"])


def _on_group_join_request(d: dict) -> None:
    handle_join_request(d)


_HANDLERS = {
    "GROUP_ADD_ROBOT": _on_group_add_robot,
    "GROUP_DEL_ROBOT": _on_group_del_robot,
    "GROUP_MSG_REJECT": _on_group_msg_reject,
    "GROUP_MSG_RECEIVE": _on_group_msg_receive,
    "GROUP_AT_MESSAGE_CREATE": _on_group_at_message,
    "GROUP_JOIN_REQUEST": _on_group_join_request,
}


# ── 入群审批 ───────────────────────────────────────────────────────────────


def _decide(group: QQBotGroup, payload: dict) -> tuple[str, str]:
    """按群策略给出 `(op, reject_reason)`；`op` ∈ `approve` / `decline`。"""
    mode = group.join_policy_mode
    if mode == QQBotGroup.JoinPolicyMode.APPROVE_ALL:
        return "approve", ""
    if mode == QQBotGroup.JoinPolicyMode.DECLINE_ALL:
        return "decline", "本群未开放加入"
    if mode == QQBotGroup.JoinPolicyMode.WHITELIST:
        whitelist = group.join_policy_whitelist or []
        if (
            payload.get("member_openid") in whitelist
            or payload.get("union_openid") in whitelist
        ):
            return "approve", ""
        return "decline", "不在白名单内"
    return "manual_pending", ""


def handle_join_request(payload: dict) -> None:
    """入群申请：按群策略调审批接口，**无论成败都写一行留痕**。

    策略 `off`（或群投影还不存在）⇒ 只写 `manual_pending`，不调接口。
    审批 API 抛异常 ⇒ 吞掉、留 `failed`（不能让一个上游 500 把 WS 事件循环打死）。
    """
    group = QQBotGroup.objects.filter(
        group_openid=payload.get("group_openid") or ""
    ).first()
    common = {
        "group": group,
        "join_request_id": str(payload.get("join_request_id") or ""),
        "member_openid": str(payload.get("member_openid") or ""),
        "union_openid": str(payload.get("union_openid") or ""),
        "username": str(payload.get("username") or ""),
        "apply_at": _parse_moment(payload.get("apply_at")),
        "apply_source": str(payload.get("apply_source") or ""),
        "risk_tips": str(payload.get("risk_tips") or ""),
        "verify_info": payload.get("verify_info"),
    }
    if group is None:
        # 群投影还没建过（ADD_ROBOT 事件没来过）：真人申请不能静默丢，留人工。
        QQBotJoinRequestLog.objects.create(
            **common, decision=QQBotJoinRequestLog.Decision.MANUAL_PENDING
        )
        return
    op, reason = _decide(group, payload)
    if op == "manual_pending":
        QQBotJoinRequestLog.objects.create(
            **common, decision=QQBotJoinRequestLog.Decision.MANUAL_PENDING
        )
        return
    binding = QQBotBinding.get_solo()
    try:
        ok, _data, code, message = qqbot_api.approve_join_request(
            _token_manager(binding),
            group.group_openid,
            common["member_openid"],
            op=op,
            join_request_id=common["join_request_id"] or None,
            reject_reason=reason or None,
        )
    except Exception as exc:  # noqa: BLE001 - 上游异常也留痕（见 docstring）
        QQBotJoinRequestLog.objects.create(
            **common,
            decision=QQBotJoinRequestLog.Decision.FAILED,
            error_code=type(exc).__name__,
            acted_at=timezone.now(),
        )
        return
    if ok:
        decision = (
            QQBotJoinRequestLog.Decision.AUTO_APPROVED
            if op == "approve"
            else QQBotJoinRequestLog.Decision.AUTO_DECLINED
        )
        QQBotJoinRequestLog.objects.create(**common, decision=decision, acted_at=timezone.now())
        return
    QQBotJoinRequestLog.objects.create(
        **common,
        decision=QQBotJoinRequestLog.Decision.FAILED,
        error_code=code,
        reject_reason=reason,
        acted_at=timezone.now(),
    )


def _token_manager(binding) -> qqbot_api.TokenManager:
    """按绑定行取（或建）token 管理器。缓存挂在本模块（reader / 事件处理共用）。

    ⭐ 评审 P3-4：缓存键用 **`app_id`**（全局唯一），⛔ 不是 `binding.pk`——binding 是
    **每租户单例**（所有租户的 pk 都是 1），多租户进程里按 pk 缓存会**跨租户互踩**，
    每次租户切换都判 `app_id` 不等、每事件重建 TokenManager。
    （⚠️ 已知残差：换了 secret 要重启 qqbot_ws 才生效——设置页有灰字提示，评审登记。）"""
    global _TM_CACHE
    cached = _TM_CACHE.get(binding.app_id)
    if cached is None:
        cached = qqbot_api.TokenManager(binding.app_id, binding.app_secret)
        _TM_CACHE[binding.app_id] = cached
    return cached


_TM_CACHE: dict = {}
