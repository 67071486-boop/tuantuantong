"""第三方平台订单管理页的**工具栏批量动作**：`POST /api/business/platform-pushes/actions/`——11 颗工具栏按钮的批量执行（按 `action` 分派）。为什么是一个端点而不是 11 个：按钮**都作用在同一批勾选行上**、判据 / 失败语义完全同形（逐笔判、逐笔回原因、部分成功如实报），做 11 条路径会让「同一件事有 11 个入口」、并把批量的公共部分（单号解析、数据权限、上限、逐笔回显）抄 11 遍 ⇒ 一个端点 + `action` 分派、公共部分只有一份。⚠️ **刻意不复用 `run_batch` 的「整批回滚」**：撤回 / 确认完单 / 同步交易状态是**上游写动作**，数据库能回滚、**代练通那边回滚不了**——套用 `run_batch`，「第 3 笔失败 ⇒ 本批未做任何修改」就是一句假话（前两笔可能已在远端删单 / 扣款）⇒ 平台动作一律**逐笔尽力而为**、结果逐笔回显；本地动作（改状态 / 退款 / 改价 / 标记）也走同一个回显口径，页面才能用一套话术说清「成功几笔、失败几笔、为什么」。
11 个动作的落点（对标语义 → 本仓能力）：`withdraw` → `dailiantong_actions.withdraw_push`（对标 `/OrderAjax/DeTransctionPlatformOrder`）；`accept` → `accept_push`（`LevelOrderOver` + 回读）；`arbitrate` → ⚠️ **D 线未就绪** ⇒ 本地「待仲裁」标记 = 一条 `order.note` 订单日志（如实提示尚未提交平台）；`sync` → `sync_push`（复用 `refresh_push_status`）；`change_status` → `order_writes.transition_order`（既有状态机，**先过「已抛单且远端未结束」的闸**）；`refund` → `order_writes.refund_total_write`（与 `/orders/<no>/refunds` 同一实现）；`set_label` / `set_tag` / `clear_tag` / `urgent` → `update_order_fields` 写对应列；`change_taker_price` → `order_writes.change_price`（写 `Order.total_price`）。
权限（⛔ 不新增节点）：类级 `order.list`（能看这一页的人才谈得上批量操作）；逐动作另加**既有节点**（⛔ 不借 `order.list` 一个门放行全部）：`change_status` 按目标态取 `order.change_status_completed` / `order.send_to_grab_hall` / `order.change_status`（与 `OrderViewSet.TRANSITION_TARGET_PERMISSIONS` 同源），`refund` ⇒ `order.online_refund`，`change_taker_price` ⇒ `order.submit_payout_price` / `order.edit_payout_price`，标签 / 标记 / 加急 ⇒ `order.add_edit` / `order.submit_edit`；权限失败是 **403**（与单笔端点同一种「没有权限」出口），不是业务码。失败分层（⛔ 不要 500）：请求形状不对（未知 `action` / 空 `order_nos` / 缺必填值 / 目标状态非法）⇒ DRF 400 被本仓处理器包成 **HTTP 200 + `code=1000`**（不碰任何业务写入）；单笔业务失败（远端状态不对 / 前置闸 / 上游拒绝 / 回读不符）⇒ **HTTP 200 + 信封**（`code=0`）+ `items[i].ok=false` + 中文 `msg`，**部分成功**照实回 `ok_count` / `fail_count`；未预期的异常仍然**响亮地 500**（AGENTS.md §5-E：不吞成业务结果）。凭据红线（AGENTS.md §4）：响应里只有 `order_no` / `ok` / `msg`，⛔ 没有 `app_secret`、支付密码、上游详情；`msg` 只可能来自服务层的中文说明或上游 `Err` 原文（服务层保证）。
"""


from __future__ import annotations

from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.views import APIView

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.order_batch import parse_order_nos, resolve_orders
from apps.business.dailiantong_actions import (
    WITHDRAW_COMMENT_MAX_LENGTH,
)
from apps.business.dailiantong_push import DailiantongPushError
from apps.business.models import DailiantongPush, OrderStatus
from apps.business.platform_adapters import adapter_for_order
from apps.business.order_writes import (
    WriteContext,
    change_price,
    refund_total_write,
    transition_order,
    update_order_fields,
)
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node, has_node_now, nodes_of

#: 类级节点（页面可见性 = 订单列表节点；⛔ 本线不新增节点）。
PAGE_NODE = "order.list"
PLATFORM_WRITE_NODE = "order.platform_write"

# ── 动作名（与前端常量逐字一致；⛔ 改这里必须同一次改页面）────────────────

ACTION_WITHDRAW = "withdraw"
ACTION_ACCEPT = "accept"
ACTION_ARBITRATE = "arbitrate"
ACTION_SYNC = "sync"
ACTION_CHANGE_STATUS = "change_status"
ACTION_REFUND = "refund"
ACTION_SET_LABEL = "set_label"
ACTION_SET_TAG = "set_tag"
ACTION_CLEAR_TAG = "clear_tag"
ACTION_URGENT = "urgent"
ACTION_CHANGE_TAKER_PRICE = "change_taker_price"

#: 动作 → 页面按钮的中文名（只服务结果文案；⛔ 不拿它做分支判据）。
ACTION_LABELS = {
    ACTION_WITHDRAW: "批量撤回",
    ACTION_ACCEPT: "批量验收",
    ACTION_ARBITRATE: "批量仲裁",
    ACTION_SYNC: "同步交易状态",
    ACTION_CHANGE_STATUS: "改系统状态",
    ACTION_REFUND: "记退款",
    ACTION_SET_LABEL: "更改标签",
    ACTION_SET_TAG: "改标记",
    ACTION_CLEAR_TAG: "删标记",
    ACTION_URGENT: "加急",
    ACTION_CHANGE_TAKER_PRICE: "改接单价",
}

ACTION_CHOICES = [(value, label) for value, label in ACTION_LABELS.items()]

#: 仲裁理由上限（对标 `layer.prompt` 的 `maxlength: 100`；逐字照抄）。
ARBITRATION_REASON_MAX_LENGTH = 100

#: 「改系统状态」按目标态要的节点（与 `OrderViewSet.TRANSITION_TARGET_PERMISSIONS` 同源）。
CHANGE_STATUS_NODES = {
    OrderStatus.COMPLETED: ("order.change_status_completed",),
    OrderStatus.WAITING_GRAB: ("order.send_to_grab_hall",),
}
DEFAULT_CHANGE_STATUS_NODE = "order.change_status"

#: 退款节点（与 `RefundView` 同源：⛔ 不因为走了本端点就放宽）。
REFUND_NODE = "order.online_refund"

#: 改接单价的节点（与 `OrderViewSet.ORDER_PERMISSIONS["price"]` 同源）。
TAKER_PRICE_NODES = ("order.submit_payout_price", "order.edit_payout_price")

#: 标签 / 标记 / 加急（写 `Order` 的普通可编辑列）的节点（与 PATCH 同源）。
ORDER_FIELD_NODES = ("order.add_edit", "order.submit_edit")


def _remote_status_text(code) -> str:
    """远端状态码的人话（只服务结果文案；解析不了就给原值，⛔ 不编状态名）。"""
    from apps.business.dailiantong_actions import REMOTE_STATUS_LABELS

    if code is None:
        return "未知"
    return REMOTE_STATUS_LABELS.get(code, f"未知（{code}）")


class PlatformPushActionSerializer(serializers.Serializer):
    """`POST /api/business/platform-pushes/actions/` 的入参。⚠️ **一个键集装 11 个动作**：每个动作只读自己那几个键、其余键忽略（与页面一致——页面每次只送一个动作需要的键）；必填判据在 `validate()` 里按 `action` 分档，所以未知 `action` / 缺键都是 `1000`（⛔ 不进业务层、⛔ 不碰上游）。
    """

    action = serializers.ChoiceField(
        choices=ACTION_CHOICES,
        error_messages={
            "required": "请指定要执行的动作",
            "invalid_choice": "不支持的操作",
        },
    )
    #: 勾选的订单号（`parse_order_nos` 负责空 / 格式 / 重复 / 上限的中文原因）。
    order_nos = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        error_messages={"required": "请至少选择一笔订单", "not_a_list": "订单号格式不正确"},
    )
    #: 撤回 / 仲裁的说明（对标 `Comment` / 仲裁理由；长度上限取服务层的撤回事由列宽）。
    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=WITHDRAW_COMMENT_MAX_LENGTH,
        trim_whitespace=True,
        error_messages={
            "max_length": f"说明不能超过 {WITHDRAW_COMMENT_MAX_LENGTH} 个字符",
        },
    )
    #: `change_status` 的目标状态（`OrderStatus` 的枚举名，与 `/transition/` 同口径）。
    target = serializers.ChoiceField(
        choices=[value for value, _label in OrderStatus.choices],
        required=False,
        error_messages={
            "invalid_choice": "目标系统状态不合法",
            "required": "请选择要改成的系统状态",
        },
    )
    #: `refund` 的三个键（与 `RefundRequestSerializer` 同形：累计总额 + 原因 + 幂等键）。
    refund_total = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        error_messages={"invalid": "退款金额格式不对（最多两位小数）"},
    )
    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
        trim_whitespace=True,
        error_messages={"max_length": "退款原因不能超过 255 个字符"},
    )
    request_id = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=64,
        error_messages={"max_length": "请求幂等号不能超过 64 个字符"},
    )
    #: `set_label` 的标签文本（空串 = 清除标签；对标下拉里有「删除标签」一项）。
    label = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=64,
        trim_whitespace=True,
        error_messages={"max_length": "标签不能超过 64 个字符"},
    )
    #: `set_tag` 的自定义标记（非空；「删标记」是另一个动作 `clear_tag`）。
    tag = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=128,
        trim_whitespace=True,
        error_messages={
            "blank": "标记内容不能为空（要清空请用「删标记」）",
            "max_length": "标记不能超过 128 个字符",
        },
    )
    #: `urgent` 的目标值：`true` = 加急（页面「加急」是对标那颗开关式按钮的**设置**档）。
    urgent = serializers.BooleanField(
        required=False,
        error_messages={"invalid": "加急标记必须是真 / 假"},
    )
    #: `change_taker_price` 的新接单价（可 0、可负数：金额列口径见 D-0015）。
    taker_price = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        error_messages={"invalid": "接单价格式不对（最多两位小数）"},
    )

    def validate(self, attrs):
        """按 `action` 校验必填键（失败 = `1000`，⛔ 不进业务层）。"""
        action = attrs["action"]
        if action == ACTION_CHANGE_STATUS and not attrs.get("target"):
            raise serializers.ValidationError({"target": "请选择要改成的系统状态"})
        if action == ACTION_REFUND:
            missing = {}
            if attrs.get("refund_total") is None:
                missing["refund_total"] = "请填写累计退款总额"
            if not (attrs.get("reason") or "").strip():
                missing["reason"] = "请填写退款原因"
            if not (attrs.get("request_id") or "").strip():
                missing["request_id"] = "缺少请求幂等号（页面重新打开退款弹窗即可）"
            if missing:
                raise serializers.ValidationError(missing)
        if action == ACTION_SET_LABEL and attrs.get("label") is None:
            raise serializers.ValidationError({"label": "请填写订单标签（留空表示清除）"})
        if action == ACTION_SET_TAG and not (attrs.get("tag") or "").strip():
            raise serializers.ValidationError({"tag": "请填写标记内容"})
        if action == ACTION_URGENT and attrs.get("urgent") is None:
            raise serializers.ValidationError({"urgent": "请指定加急标记"})
        if action == ACTION_CHANGE_TAKER_PRICE and attrs.get("taker_price") is None:
            raise serializers.ValidationError({"taker_price": "请填写新的接单价"})
        return attrs


def _permission_denial(user, action: str, data) -> str:
    """逐动作的额外节点判据；返回空串 = 放行（⛔ 不用 `order.list` 一个门放行全部）。"""
    if action in (ACTION_WITHDRAW, ACTION_ACCEPT) and not has_node_now(user, PLATFORM_WRITE_NODE):
        return "你没有交易平台涉钱写操作的权限"
    if action == ACTION_CHANGE_STATUS:
        target = str(data.get("target") or "")
        codes = CHANGE_STATUS_NODES.get(target, (DEFAULT_CHANGE_STATUS_NODE,))
        return "" if has_node_now(user, *codes) else "你没有改订单状态的权限"
    if action == ACTION_REFUND:
        return "" if has_node_now(user, REFUND_NODE) else "你没有订单退款权限"
    if action == ACTION_CHANGE_TAKER_PRICE:
        return "" if has_node_now(user, *TAKER_PRICE_NODES) else "你没有改接单价的权限"
    if action in (ACTION_SET_LABEL, ACTION_SET_TAG, ACTION_CLEAR_TAG, ACTION_URGENT):
        return "" if has_node_now(user, *ORDER_FIELD_NODES) else "你没有编辑订单的权限"
    return ""


def _write_context(request) -> WriteContext:
    """与单笔端点同口径的写入上下文（操作人一律取认证态，⛔ 不读请求体 `actor`）。"""
    actor = getattr(request.user, "username", "") or ""
    return WriteContext(
        actor=actor,
        log_actor=actor,
        log_actor_id=request.user.pk,
        nodes=nodes_of(request.user),
        user=request.user,
    )


class PlatformPushActionView(APIView):
    """`POST /api/business/platform-pushes/actions/`（逐笔执行 + 逐笔回原因）。"""

    def get_permissions(self):
        """类级 = `order.list`（与读面 / T10b 动作同口径）。"""
        return [has_node(PAGE_NODE)()]

    def post(self, request):
        payload = PlatformPushActionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        order_nos, error = parse_order_nos(request.data.get("order_nos"))
        if error:
            return api_response(data=None, msg=error, code=ErrorCode.VALIDATION)

        action = data["action"]
        denial = _permission_denial(request.user, action, data)
        if denial:
            raise PermissionDenied(denial)

        ordered, issues = resolve_orders(request, order_nos)
        issues_by_no = {issue["order_no"]: issue for issue in issues}
        rows_by_no = {order.order_no: order for order in ordered}
        ctx = _write_context(request)

        items = []
        for order_no in order_nos:
            issue = issues_by_no.get(order_no)
            if issue is not None:
                items.append(
                    {"order_no": order_no, "ok": False, "msg": issue["msg"]}
                )
                continue
            order = rows_by_no[order_no]
            try:
                msg = _run_platform_push_action(
                    request, order, action=action, data=data, ctx=ctx
                )
            except DailiantongPushError as exc:
                items.append(
                    {"order_no": order_no, "ok": False, "msg": str(exc)}
                )
                continue
            items.append({"order_no": order_no, "ok": True, "msg": msg})

        ok_count = sum(1 for item in items if item["ok"])
        fail_count = len(items) - ok_count
        return api_response(
            data={
                "action": action,
                "items": items,
                "ok_count": ok_count,
                "fail_count": fail_count,
            },
            msg=_batch_summary(action, items, ok_count, fail_count),
        )


# ── 逐笔分派（模块级：埋点覆盖网按名字跟到下面那次 `record(...)`）────────────


def _run_platform_push_action(request, order, *, action: str, data, ctx) -> str:
    """执行一笔；返回**这一笔**的中文成功说明（失败一律抛 `DailiantongPushError`）。⭐ 多平台骨架：平台动作（撤回 / 验收 / 同步 / 手改状态闸）按**这张单最新一条有效推送行的 `platform`** 取适配器再分派 ⇒ 混合平台的一次批量里每一笔都走自己平台的适配器；某一笔的平台未接入只让那一笔回中文错误、其它笔照做。本地动作（改状态 / 退款 / 改价 / 标签 / 标记 / 加急）与平台无关，⛔ 不走适配器。
    """
    if action == ACTION_WITHDRAW:
        push = adapter_for_order(order).withdraw_push(
            order,
            operator=request.user,
            comment=data.get("comment") or None,
        )
        if push.state == DailiantongPush.State.CLOSED:
            return "已删除并即时退款"
        return "已提交协商撤销，等待打手 / 客服处理"

    if action == ACTION_ACCEPT:
        adapter_for_order(order).accept_push(order, operator=request.user)
        return "已确认完单（远端状态：已结算）"

    if action == ACTION_SYNC:
        push = adapter_for_order(order).sync_push(order, operator=request.user)
        return f"已同步（远端状态：{_remote_status_text(push.remote_status)}）"

    if action == ACTION_ARBITRATE:
        # ⚠️ D 线（申请客服介入）未就绪 ⇒ **本地标记待仲裁**：写一条订单日志。
        # ⛔ 不假装已提交平台：文案与日志正文都写明「尚未提交代练通」。
        reason = (data.get("comment") or "").strip()
        detail = f"：{reason}" if reason else ""
        record(
            log_type=ACTION_LOG_TYPES["order.note"],
            action="order.note",
            actor=(getattr(request.user, "username", "") or "").strip() or "系统",
            actor_id=request.user.pk,
            target_kind="order",
            target_id=order.order_no,
            target_name=order.order_no,
            order_no=order.order_no,
            reason=f"标记待仲裁（本地记录，尚未提交代练通）{detail}",
        )
        return "已在本地订单日志记下「待仲裁」（尚未提交代练通）"

    if action == ACTION_CHANGE_STATUS:
        denial = adapter_for_order(order).manual_status_denial(order)
        if denial:
            raise DailiantongPushError(denial)
        outcome = transition_order(
            order, target=data["target"], ctx=ctx, dry_run=False
        )
        _raise_on_outcome(outcome)
        return f"系统状态已改为 {order.get_status_display()}"

    if action == ACTION_REFUND:
        outcome = refund_total_write(
            order,
            refund_total=data["refund_total"],
            reason=data["reason"],
            request_id=data["request_id"],
            ctx=ctx,
            dry_run=False,
        )
        _raise_on_outcome(outcome)
        return f"累计退款额已记为 {data['refund_total']}"

    if action == ACTION_CHANGE_TAKER_PRICE:
        outcome = change_price(
            order,
            total_price=data["taker_price"],
            reason=(
                "来自第三方平台订单管理页"
                + (f"：{data['comment']}" if data.get("comment") else "")
            ),
            ctx=ctx,
            dry_run=False,
        )
        _raise_on_outcome(outcome)
        return f"接单价已改为 {data['taker_price']}"

    # 本地普通列：标签 / 标记 / 删标记 / 加急（与 PATCH 同一个核心 + 同一个逐字段留痕）。
    if action == ACTION_SET_LABEL:
        return _update_order_field(
            request, order, ctx, "order_label", data["label"], "标签"
        )
    if action == ACTION_SET_TAG:
        return _update_order_field(request, order, ctx, "tag", data["tag"], "标记")
    if action == ACTION_CLEAR_TAG:
        return _update_order_field(request, order, ctx, "tag", "", "标记")
    if action == ACTION_URGENT:
        flag = bool(data["urgent"])
        return _update_order_field(
            request,
            order,
            ctx,
            "is_urgent",
            flag,
            "加急标记",
            display="加急" if flag else "取消加急",
        )
    # pragma: no cover - 枚举已被序列化器挡死，这里只防「加了 action 忘了分支」
    raise DailiantongPushError(f"不支持的操作：{action}")


def _update_order_field(
    request, order, ctx, field: str, value, label: str, *, display: str = ""
) -> str:
    outcome = update_order_fields(
        order,
        validated={field: value},
        request_user=request.user,
        ctx=ctx,
        dry_run=False,
    )
    _raise_on_outcome(outcome)
    return f"{display or label}已更新"


def _raise_on_outcome(outcome) -> None:
    """`WriteOutcome` 失败 ⇒ 翻成 `DailiantongPushError`（页面只认一种失败出口）。"""
    if not outcome.ok:
        raise DailiantongPushError(outcome.msg)


def _batch_summary(action: str, items, ok_count: int, fail_count: int) -> str:
    """整批的一句话结论（页面直接显示；失败时带上第一个原因）。"""
    label = ACTION_LABELS.get(action, "批量操作")
    if fail_count == 0:
        return f"{label}已完成：成功 {ok_count} 笔"
    first_failure = next((item for item in items if not item["ok"]), None)
    reason = "" if first_failure is None else first_failure["msg"]
    if ok_count == 0:
        return f"{label}失败：{reason}" if reason else f"{label}失败"
    return f"{label}部分成功：成功 {ok_count} 笔，失败 {fail_count} 笔（{reason}）"
