"""统一操作日志写入口和文案渲染。
结构化列保留审计原值，公开正文采用业务称呼、操作来源和展示单号。
"""
import string
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.display import user_display_name
from apps.audit.actions import (
    ACTION_LOG_TYPES,
    ACTION_TEMPLATES,
    FIELD_LABELS,
    ONLINE_PAYROLL_SETTLE,
    ONLINE_PAYROLL_SETTLE_FIELD,
    ORDER_CLAIM_FROM_DOWNSTREAM,
    ORDER_CLAIM_SYNCED_DOWNSTREAM,
    ORDER_CLAIM_TAKER_API,
    ORDER_DELETE_KEEP_PERFORMANCE,
    ORDER_DELETE_WITH_PERFORMANCE,
    RECEIPT_QUERY_RESULTS,
    QR_FETCH_SUCCESS_REASON,
)
from apps.audit.models import OperationLog
from apps.business.performance_amount import DEDUCT_FIXED, DEDUCT_PERCENT

# 渲染器**能消费**的键 = 行内列（spec §5.9）。`field_label` 不在内：它由 `field_name` 派生。
# 这份清单同时是 `record()` 转发给 `render_summary` 的清单——两处必须一致（有单测钉死）。
# `actor_id` 是**整数**、不是文本列：只用来声明「这行有没有操作人」（spec §3.2），现在**不**参与前缀判定（spec §3-A1：机器行也加前缀）——`render_summary` 仍收它（调用契约不变），但只塞进 `actor_id` 槽位、不做任何分支；不进 `_columns()`、不进 `_clip`，由 `record()` 直接传。
RENDER_INPUTS = (
    "actor", "actor_id", "actor_name", "target_id", "target_name", "order_no",
    "source_order_no", "game_name", "game_role_name", "field_name", "from_value",
    "to_value", "reason",
)


def _text(value):
    """`None` ⇒ 空串；其余转成字符串。判据是 `is None` 不是真值（E-0021）：`Decimal("0")` / `0` / `False` 都是合法且有意义的边界值，真值判断会把「传了 0」当成「没传」而静默丢值。"""
    return "" if value is None else str(value)


def _clip(value, field_name):
    """归一化 + 按 `OperationLog` 该列的 `max_length` 截断（列宽从模型元数据取，不抄数字）。"""
    text = _text(value)
    width = OperationLog._meta.get_field(field_name).max_length
    return text if width is None else text[:width]


class _RowColumns(dict):
    """行内列的取值表：缺列时返回空串（模板与 `record()` 的清单一致，正常不会走到这里）。"""

    def __missing__(self, key):
        return ""


def _placeholder_names(template):
    """模板里出现的**字段名**集合（`{a}{b!r}{c:>3}` 三种写法都算）。

    ⚠️ **不**用 `"{actor_name}" in template` 这种子串判据：它漏 `{actor_name!r}` /
    `{actor_name:>4}` 这类写法，而「模板里有没有这个占位符」是决定要不要加前缀的**承重**判据。
    """
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}



WORDING_FIELDS = {
    "deduct_mode": {DEDUCT_PERCENT: "按比例扣除", DEDUCT_FIXED: "扣除固定金额", "": "未设置"},
    "is_urgent": {"true": "是", "false": "否", "1": "是", "0": "否", "": "未设置"},
}


def business_field_value(field_name, value):
    value = _text(value)
    labels = WORDING_FIELDS.get(field_name)
    return labels.get(value.lower(), "未知") if labels is not None else value


def qr_failure_text(reason):
    """把只有排障价值的接口或异常字段换成人话。"""
    detail = _text(reason).removeprefix("自动拉取群码失败：")
    replacements = (
        ("连接查码接口失败", "无法连接查码服务"),
        ("查码接口返回", "查码服务暂时异常"),
        ("查码接口判参数非法", "查码服务无法识别订单信息"),
        ("查码接口未知", "查码服务暂时异常"),
        ("查码接口 data", "查码服务暂时异常"),
        ("查码接口 ok", "查码服务没有返回群码图片"),
        ("图片上传存储失败", "群码图片保存失败"),
        ("签发上传凭证被拒", "群码图片暂时无法上传"),
        ("认领被拒", "群码图片未能保存到订单"),
    )
    for prefix, message in replacements:
        if detail.startswith(prefix):
            return message
    return detail or "查码服务暂时异常"


def render_summary(action, *, actor, actor_id="", actor_name="", target_id="", target_name="",
                   order_no="", source_order_no="", game_name="", game_role_name="",
                   field_name="", from_value="", to_value="", reason=""):
    """结构化列生成业务正文，操作人快照只显示一次。"""
    template = ACTION_TEMPLATES.get(action)
    if action == "performance.settle" and field_name == ONLINE_PAYROLL_SETTLE_FIELD:
        template = ONLINE_PAYROLL_SETTLE
    if action == "order.delete":
        template = {
            "delete_with_performance": ORDER_DELETE_WITH_PERFORMANCE,
            "delete_keep_performance": ORDER_DELETE_KEEP_PERFORMANCE,
        }.get(field_name, template)
    if action == "order.claim":
        if field_name == "downstream_claim":
            template = ORDER_CLAIM_SYNCED_DOWNSTREAM if reason else ORDER_CLAIM_FROM_DOWNSTREAM
        elif reason == "打手对接接口" and not actor_id:
            template = ORDER_CLAIM_TAKER_API
        else:
            reason = {
                "系统网页": "网页端大厅",
                "打手App": "打手移动端大厅",
            }.get(reason, reason or "接单大厅")
    if action == "order.upstream_receipt_query":
        outcome = _text(reason).partition("结局=")[2].split(" ", 1)[0]
        to_value = RECEIPT_QUERY_RESULTS.get(outcome, "已查询")
    if action == "order.ship_result" and _text(reason).startswith("失败（内部错误："):
        reason = "失败（系统暂未能提交发货，请查看发货记录）"
    if action == "order.malailai_qr_fetch":
        if reason == QR_FETCH_SUCCESS_REASON:
            to_value, reason = "成功", ""
        else:
            to_value, reason = "失败", qr_failure_text(reason)
    if template is None:
        template = "操作记录"

    fields = _placeholder_names(template)
    source_order_no = _text(source_order_no).strip()
    display_no = source_order_no or _text(order_no)
    if "source_order_no" in fields or action == "order.copy":
        display_no = _text(order_no)
        if not source_order_no:
            template = template.replace("，来源单号：{source_order_no}", "")
    name = _text(field_name)
    values = _RowColumns(
        actor=_text(actor),
        # `actor_id` 不参与 `format_map` 的文本（spec §3.2）；⚠️ 起它也**不**参与
        # 「要不要加 `【】`前缀」的判定（spec §3-A1：机器行同样加前缀）⇒ 模板里 ⛔ 别用它。
        actor_id=_text(actor_id),
        actor_name=_text(actor_name),
        target_id=_text(target_id),
        target_name=_text(target_name),
        order_no=display_no,
        source_order_no=_text(source_order_no),
        game_name=_text(game_name),
        game_role_name=_text(game_role_name),
        field_name=name,
        field_label=FIELD_LABELS.get(name, "订单字段"),
        from_value=business_field_value(name, from_value),
        to_value=business_field_value(name, to_value),
        reason=_text(reason),
    )
    # 空值剔除（D-0337 / D-0364 同族）：模板可把可选值写成整段「（原因：{reason}）」或「（状态：{from_value} → {to_value}）」，值可选、为空 ⇒ format 前把该段整段剔除，⛔ 不许留「（原因）」「（状态： → 代练中）」空壳；剔除后与不带该段的旧模板逐字相同（既有动作 summary 口径零漂移）。
    # 判据打在**精确字面段**上（不是泛化正则）：全仓模板里合法出现这两个字面段的只有把它们当可剔除段用的那几条（grep actions.py 可验证）；模板正文里想渲染这两个字面文本本身的写法会被误伤，⛔ 别那么写。
    # 剔除判据打在**行内列**上，不回查业务表（重放渲染仍可测）。
    if not values["reason"]:
        template = template.replace("（原因：{reason}）", "")
    if not values["from_value"]:
        template = template.replace("（状态：{from_value} → {to_value}）", "")
    if action == "order.update":
        for key in ("from_value", "to_value"):
            values[key] = values[key] or "未填写"
    if not values["order_no"]:
        template = template.removesuffix("：{order_no}")
    if action == "order.claim" and not values["to_value"]:
        template = template.replace("，接手价 {to_value} 元", "")
    summary = template.format_map(values)
    if display_no and not {"order_no", "source_order_no"}.intersection(fields):
        summary = f"{summary}（订单 {display_no}）"
    # 两条缺一不可（spec §3.3 步骤 4 / §3-A1）：显示名非空、名字不在模板里；机器行的 `actor_name` 派生自 `actor` ⇒ 前缀 = 来源。
    if values["actor_name"] and "actor_name" not in _placeholder_names(template):
        summary = f"【{values['actor_name']}】{summary}"
    return summary


def _actor_name(actor, actor_id):
    """本行的**显示名**（spec §3.2）：人为 ⇒ 按 `pk/username/first_name` 三列查一次用户后取 `user_display_name`（账号表挂着手机号 / 身份证号等 PII 列，本函数**不读**别的列）；查不到（用户已删 / 非本租户）或 `actor_id` 为空 ⇒ 回落 `actor`——机器行的来源就在 `actor` 列里，⛔ 不需要新造第二个名称字段（spec §3-A1）。
    ⚠️ 判据是 `is not None`，不是真值（E-0021；`actor_id` 是整型边界）。
    ⚠️ **代价（如实，spec §3.2）**：每条人为日志多一次主键查询 ⇒ 「一次操作写 N 行日志」的调用点（`order.update` 一个被改字段一行）就是 N 次额外 SELECT，**是真的 N+1 形状**；⛔ 不加进程级缓存——显示名随用户改名变，缓存会撒谎。
    """
    if actor_id is not None:
        user = (
            get_user_model()
            .objects.filter(pk=actor_id)
            .only("pk", "username", "first_name")
            .first()
        )
        if user is not None:
            # 口径的唯一出处：`apps/accounts/display.user_display_name`（昵称优先、空串回落账号）。
            return user_display_name(user)
    return actor or "系统"


def _columns(*, actor, actor_name, target_kind, target_id, target_name, order_no,
             source_order_no, game_role_name, game_name, field_name, from_value, to_value,
             reason):
    """把一次写入的**全部文本列**在一处组装、归一化、按列宽截断。

    模型写入与 `summary` 渲染都只吃这一份字典 —— 任一处漏转发都会同时少一列，
    由 `test_record_forwards_every_renderer_input` 直接抓（评审 Important）。
    """
    raw = {
        "actor": actor, "actor_name": actor_name, "target_kind": target_kind,
        "target_id": target_id, "target_name": target_name, "order_no": order_no,
        "source_order_no": source_order_no, "game_role_name": game_role_name,
        "game_name": game_name, "field_name": field_name, "from_value": from_value,
        "to_value": to_value, "reason": reason,
    }
    return {name: _clip(value, name) for name, value in raw.items()}


def record(*, log_type, action, actor, actor_id=None, system=False, target_kind="",
           target_id="", target_name="", order_no="", source_order_no="",
           game_role_name="", game_name="", field_name="", from_value="", to_value="",
           reason="", created_at=None):
    """**唯一写入出口**（spec §7.1）。没有 `summary` 形参是**故意的**（多传一个 `summary=` 会 `TypeError` 防漂移）；`actor_name` 由 `actor_id` 在内部派生（spec §3-A1），调用方不自传；文本列统一归一化并按列宽截断（见模块 docstring）。
    ⭐ **fail-closed（模块 docstring 取向 5）**：`actor_id is None` 且 `system` 不为真 ⇒ 抛 `ValueError`——机器 / 系统来源必须**显式**传 `system=True`，有认证态的调用点必须传 `actor_id=<用户主键>` ⇒ 「漏传 `actor_id` 静默退化成机器行」这条路径不存在。
    ⚠️ 本函数在数据库写入**之前**就抛（不查用户、不留半行）。
    """
    if actor_id is None and not system:
        raise ValueError(
            "record() 拒绝写入没有操作人的日志行：人为操作的调用点必须传 "
            f"actor_id=<用户主键>；机器 / 系统来源必须显式传 system=True。"
            f"（action={action!r}，actor={actor!r}）"
        )
    columns = _columns(
        actor=actor, actor_name=_actor_name(actor, actor_id), target_kind=target_kind,
        target_id=target_id, target_name=target_name, order_no=order_no,
        source_order_no=source_order_no, game_role_name=game_role_name,
        game_name=game_name, field_name=field_name, from_value=from_value,
        to_value=to_value, reason=reason,
    )
    summary = render_summary(
        action,
        # 除 `actor_id` 外的渲染输入都从 `_columns()` 那份字典转发（同一份真相，不许漏转发）；
        # `actor_id` 是整数、不进 `_columns()`，单独直传（spec §3.2）。
        **{name: columns[name] for name in RENDER_INPUTS if name != "actor_id"},
        actor_id=actor_id,
    )
    if created_at is not None:
        columns["created_at"] = created_at
    return OperationLog.objects.create(
        log_type=log_type, action=action, actor_id=actor_id,
        summary=_clip(summary, "summary"), **columns,
    )



def record_order_read(*, actor, actor_id, action, order_no, source_order_no=""):
    """用共享数据库锁在多 worker 间合并重复查看。"""
    key = f"audit:read:{connection.schema_name}:{actor_id}:{action}:{order_no}"
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])
        cutoff = timezone.now() - timedelta(minutes=3)
        if OperationLog.objects.filter(
            actor_id=actor_id, action=action, order_no=order_no, created_at__gt=cutoff
        ).exists():
            return
        record(
            log_type=ACTION_LOG_TYPES[action], action=action, actor=actor, actor_id=actor_id,
            target_kind="order", target_id=order_no, order_no=order_no,
            source_order_no=source_order_no,
        )
