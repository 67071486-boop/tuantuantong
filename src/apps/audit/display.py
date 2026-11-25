"""审计日志行的**显示名**解析：`actor_id` → 昵称（空则账号名）的批量映射。单独成模块是因为同一条读面口径有**两个**消费者：系统操作日志页（`GET /api/audit/logs/` 的序列化器经 `context["actor_display_names"]` 注入）与订单详情页（`apps/business/api/order_views.py` 的 `audit` / `notes` 两个动作）——⛔ 同一事实只有一处实现，各写一份必然漂移。
⚠️ 这里**不是**重复实现 `user_display_name`：本模块只负责「一次把一页/一批行的人查回来」，逐人的「叫什么」仍然只由 `apps/accounts/display.py::user_display_name`（口径单一出处）决定。
"""
import re

from django.contrib.auth import get_user_model

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES, ORDER_ACTION_LABELS
from apps.audit.log import RENDER_INPUTS, WORDING_FIELDS, render_summary
from apps.audit.models import LogType


def actor_display_names(rows):
    """`actor_id` → 显示名（昵称，空则账号名）的**一次性**映射。
    ⚠️ 批量查一次，不要在 `SerializerMethodField` / 逐行的循环里 `User.objects.get(...)`——一页 200 行会打 200 次库（系统操作日志线 spec §11.1；判据见 `tests/test_operation_log_api.py::OperationLogActorDisplayTests::test_nickname_lookup_is_batched_not_per_row`）。`actor_id` 为 `None` 的行不进查询（历史行 / 机器行，由调用方回退到 `actor` 列）。
    ⚠️ `.only(...)` 只取 `pk` / `username` / `first_name` 三列——账号表那一侧挂着手机号 / 身份证号 / 收款账号等 PII，本函数**不读**、也不该读别的列。
    """
    ids = {row.actor_id for row in rows if row.actor_id is not None}
    if not ids:
        return {}
    return {
        # 口径的唯一出处：`apps.accounts.display.user_display_name`（昵称优先、空串回落账号）。
        user.pk: user_display_name(user)
        for user in get_user_model()
        .objects.filter(pk__in=ids)
        .only("pk", "username", "first_name")
    }

def action_label(action):
    """未知动作保留查询键，给人看的短名回退为中文分类。"""
    if action == "order.view_credentials":
        return "查看订单详情"
    if action in ORDER_ACTION_LABELS:
        return ORDER_ACTION_LABELS[action]
    if action in ACTION_LOG_TYPES:
        return LogType(ACTION_LOG_TYPES[action]).label
    return "订单操作" if action.startswith("order.") else "操作记录"


def _render_display_summary(row, source, *, copy_origin=None):
    inputs = {name: getattr(row, name) for name in RENDER_INPUTS}
    inputs["source_order_no"] = source
    inputs["actor_name"] = row.actor_name or row.actor or "系统"
    if copy_origin is not None:
        inputs["reason"] = copy_origin
    return render_summary(row.action, **inputs)


def _copy_summary(row, source):
    """复制的两笔单可能共用来源号，正文用可核实的单号区分。"""
    origin = row.from_value or row.reason
    if not origin or not row.order_no:
        return row.summary
    summary = _render_display_summary(row, "", copy_origin=origin)
    if source and source not in (origin, row.order_no):
        return f"{summary}（新单来源单号：{source}）"
    return summary


def _replace_legacy_order_number(row, source):
    """只替换首个独立订单号，保留已展示的来源号以及备注。"""
    summary = row.summary
    operator = row.actor_name or row.actor
    operator_prefix = next(
        (prefix for prefix in (f"【{operator}】", operator) if operator and summary.startswith(prefix)),
        "",
    )
    body = summary[len(operator_prefix):]
    reason = row.reason or ""
    before, note, after = body, "", ""
    if row.order_no in reason or source in reason:
        before, note, after = body.rpartition(reason)
        if not note:
            return summary
    pattern = re.compile(
        rf"(?<![0-9A-Za-z_-])(?:{re.escape(source)}|(?P<system>{re.escape(row.order_no)}))"
        r"(?![0-9A-Za-z_-])",
    )
    match = pattern.search(before)
    offset = len(operator_prefix)
    if not match:
        match = pattern.search(after)
        offset += len(before) + len(note)
    if not match or match.lastgroup != "system":
        return summary
    return summary[:offset + match.start()] + source + summary[offset + match.end():]


def summary_for_display(row, *, source_order_no=""):
    """只修补已知旧文案和结构化标识，保留原始审计。"""
    source = (row.source_order_no or "").strip() or (source_order_no or "").strip()
    summary = row.summary
    action = row.action
    if action == "order.copy":
        return _copy_summary(row, source)
    if action == "order.jidanpu_member_auto_claim":
        account = row.target_name or "对应的团团通账号"
        operator = row.actor_name or row.actor or "系统"
        return f"【{operator}】下家已接手订单 {source or row.order_no}，本地对应接手账号：{account}"
    if (
        summary == action
        or not summary
        or action in {"order.malailai_qr_fetch", "order.upstream_receipt_query"}
        or (action == "order.note" and row.reason)
        or (action in {"order.update", "performance.adjust"} and row.field_name in WORDING_FIELDS)
        or (action == "order.ship_result" and "失败（内部错误：" in summary)
        or (action == "order.view_credentials" and "查看游戏账号密码及详情" in summary)
        or (action == "order.open_edit" and "进入编辑订单页面" in summary)
        or (action == "order.delete" and "同时也自动删除了" in summary)
    ):
        return _render_display_summary(row, source)

    if source and row.order_no and source != row.order_no and action not in {"order.create", "order.note"}:
        return _replace_legacy_order_number(row, source)
    return summary
