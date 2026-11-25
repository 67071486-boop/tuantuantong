"""客服后台的「打手提现审核」端点。| 方法 | 路径 | 动作 ||---|---|---|| GET | `/api/business/withdrawals/` | 列表（`?state=&page=`），打开时对 `paying` 单兜底查一遍 || POST | `/api/business/withdrawals/<id>/approve/` | 通过 || POST | `/api/business/withdrawals/<id>/reject/` | 驳回（原因必填，退余额） || POST | `/api/business/withdrawals/<id>/transfer/` | 发起代付（带 `confirm_amount`） || POST | `/api/business/withdrawals/<id>/refresh/` | 查单刷新 || POST | `/api/business/withdrawals/<id>/mark-pai
d/` | 登记人工打款（单号必填） |## 三条口径1. **只认本租户、本单**：`WithdrawOrder` 是租户库里的表，`get_object()` 只从当前 schema 取，⛔ 不接受任何租户标识参数；单子不存在 / 不属于本租户一律 404（不给「别人的单」的 403 差异）。2. **闸门 = `finance.taker_withdraw_review`**（`apps/rbac/catalog.py` 的自有节点，`benchmark_id=None`）：类体 `permission_classes = [IsAuthenticated, has_node(...)]`，**fail-closed**（没节点 403）。3. **金额一律字符串出参**（与钱包端点同口径）：
`Decimal` 交给 DRF 的 JSONEncoder 会被`float()`（`rest_framework/utils/encoders.py`）⇒ 本模块显式 `f"{value:.2f}"`，⛔ 不 `float()`。## 响应契约（前后端并行施工，改它要两边一起改，计划 §4.3.1）- 列表 `data` = `paginated(...)` + `auto_transfer_enabled` + `transfer_max_amount`（字符串）；- 行对象金额是字符串，含 `transfer_error` / `channel`；- 动作端点返回 `{"id", "state", "state_text"}`，`transfer/` 额外带 `transfer_id` / `transfer_error`。## 为什么列表要「兜底查单」这里**不做定时查单任务**（计划 §1：要新增 systemd 单元，另开线）⇒ 打开列表时对当前页里`paying` 的单各查一次上游。查单失败**不能把整页打成 500**：单条失败只写 `transfer_error`、其余行照常返回（客服仍要看得到列表）。"""
import time
from decimal import Decimal, InvalidOperation

from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from apps.accounts.display import user_display_name
from apps.accounts.jeepay import JeepayError
from apps.accounts.models import WithdrawOrder
from apps.accounts.wallet import (
    WalletError,
    approve_withdraw,
    auto_transfer_enabled,
    interrupt_withdraw,
    mark_withdraw_paid,
    refresh_transfer,
    reject_withdraw,
    start_transfer,
    transfer_inflight,
    transfer_max_amount,
)
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.base import EnvelopePagination
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response, paginated
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node

#: 提现审核页的权限节点（本仓第五条自有节点，`benchmark_id=None`）。
#: ⚠️ 接线位点表（`apps/rbac/wiring.py` 的 `SITES`）指向本类的 `permission_classes`；
#: 改名要同批改那里（`tests/test_rbac_wiring.py` 的 T2 会按 AST 核对）。
WITHDRAW_REVIEW_NODE = "finance.taker_withdraw_review"

#: 列表兜底查单的**预算**（评审 A4）：每页最多查 5 条 `paying`，且总耗时不超过 3 秒。
#: 为什么必须有预算：`paying` 多的时候一次 GET 会串行打 N 次上游；上游一慢，打开列表就卡死。
LIST_REFRESH_MAX_ORDERS = 5
LIST_REFRESH_BUDGET_SECONDS = 3.0


def _money(value) -> str:
    """金额出参的唯一格式化点：两位小数、字符串（与 `app_wallet_views._money` 同口径）。"""
    return f"{value:.2f}"


def _action_name(order) -> str:
    """操作日志的 `target_name`：打手的登录账号（显示名另由 `record()` 派生）。"""
    return getattr(order.user, "username", "") or ""


class WithdrawAdminPagination(EnvelopePagination):
    """审核页分页：在标准信封的 `data` 里多带两个前端开关位（计划 §4.3.1）。"""

    def get_paginated_response(self, data):
        payload = paginated(
            data,
            total=self.page.paginator.count,
            page=self.page.number,
            page_size=self.get_page_size(self.request),
        )
        # ⚠️ `auto_transfer_enabled` 与 `transfer_max_amount` 是**同一次**从配置读出来的：
        #    不要让前端自己猜开关状态（计划 §6）。
        payload["auto_transfer_enabled"] = auto_transfer_enabled()
        limit = transfer_max_amount()
        # 上限未配置（None）⇒ 不限制；此时给空串，前端按「没有上限」处理。
        payload["transfer_max_amount"] = "" if limit is None else _money(limit)
        return api_response(payload)


class _ListQuerySerializer(serializers.Serializer):
    """列表筛选参数的唯一校验口（非法 `state` ⇒ 业务码 1000，⛔ 不是 500）。"""

    state = serializers.ChoiceField(
        choices=[code for code, _label in WithdrawOrder.STATE_CHOICES],
        required=False,
        error_messages={"invalid_choice": "state 取值不正确"},
    )


class _RejectSerializer(serializers.Serializer):
    """驳回入参：原因必填（服务层还有一道同样的闸）。"""

    remark = serializers.CharField(max_length=200, allow_blank=False, trim_whitespace=True)


class _TransferSerializer(serializers.Serializer):
    """发起代付的入参：`confirm_amount` 是**字符串/数字**，解析后必须有限、最多两位小数。

    用 `CharField` 而不是 `DecimalField`：DRF 的 `DecimalField` 会把多余精度**四舍五入**，
    而这里是「回显的单子金额」，⛔ 不许悄悄变（与 `WithdrawRequestSerializer` 同一条理由）。
    """

    confirm_amount = serializers.CharField(max_length=32)

    def validate_confirm_amount(self, value):
        try:
            amount = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            raise serializers.ValidationError("金额格式不正确")
        if not amount.is_finite():
            raise serializers.ValidationError("金额格式不正确")
        if amount.as_tuple().exponent < -2:
            raise serializers.ValidationError("金额最多两位小数")
        return amount


class _MarkPaidSerializer(serializers.Serializer):
    """登记人工打款：打款单号必填；备注可选。"""

    transfer_no = serializers.CharField(max_length=64, allow_blank=False, trim_whitespace=True)
    remark = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )


class _InterruptSerializer(serializers.Serializer):
    """财务中断入参：原因必填（服务层还有一道同样的闸）。"""

    reason = serializers.CharField(max_length=200, allow_blank=False, trim_whitespace=True)


def _first_error(serializer) -> str:
    for errors in serializer.errors.values():
        if errors:
            return str(errors[0])
    return "参数不正确"


def _row(order) -> dict:
    """一行提现单（计划 §4.3.1 的行对象，逐字实现）。"""
    return {
        "id": order.pk,
        "user_id": order.user_id,
        "username": getattr(order.user, "username", "") or "",
        "display_name": user_display_name(order.user),
        "amount": _money(order.amount),
        "alipay_account": order.alipay_account,
        "alipay_name": order.alipay_name,
        "state": order.state,
        "state_text": order.get_state_display(),
        # ⭐ 前三单快路：快路单的预计打款时间 = 申请时刻。
        # ⚠️ D-0302：本行的六个时刻键全部手拼 ⇒ 一律 `local_iso`
        #    （+08:00）；前端「切字符」格式化器直显，回 UTC 串就整列差 8 小时。
        "fast_lane": order.fast_lane,
        "payout_after": local_iso(order.payout_after),
        "channel": order.channel,
        "transfer_id": order.transfer_id,
        "transfer_error": order.transfer_error,
        # ⭐ 前端据此决定「登记打款」给不给（在途窗口里点了必然被拒）。
        "transfer_inflight": transfer_inflight(order),
        "remark": order.remark,
        "created_at": local_iso(order.created_at),
        "reviewed_at": local_iso(order.reviewed_at),
        # ⭐：超时自动审核 / 快路自动通过的单没有 `reviewer`
        #    ⇒ 给前端一个能直说的名字，别让客服在「审核人」列看到空白。
        "auto_approved": order.reviewed_at is not None and order.reviewer_id is None,
        "reviewer_name": (
            getattr(order.reviewer, "username", "") or ""
            if order.reviewer_id is not None
            else "系统"
        ),
        "paid_at": local_iso(order.paid_at),
        # ⭐ 财务中断：只对 `interrupted` 有值；前端在「已中断」tab 显示原因。
        "interrupted_at": local_iso(order.interrupted_at),
        "interrupt_reason": order.interrupt_reason,
    }


def _action_payload(order, *, with_transfer: bool = False) -> dict:
    payload = {
        "id": order.pk,
        "state": order.state,
        "state_text": order.get_state_display(),
    }
    if with_transfer:
        payload["transfer_id"] = order.transfer_id
        payload["transfer_error"] = order.transfer_error
    return payload


def _client_ip(request) -> str:
    """交给上游的 `clientIp`。

    ⚠️ 这里**取请求来源 IP**（`REMOTE_ADDR`）；生产在反向代理后面时它是代理地址——
    计划 §10 的实测项之一（服务器出口 IP / 打手 IP / 固定值该填哪个）确认后改这一处。
    """
    return request.META.get("REMOTE_ADDR") or "127.0.0.1"


class WithdrawAdminViewSet(viewsets.GenericViewSet):
    """打手提现审核（列表 + 五个写动作）。"""

    #: ⚠️ 类体装配：计划要求 `permission_classes = [IsAuthenticated, has_node(...)]`，
    #: 接线位点表也按**类属性**核这道门（`tests/test_rbac_wiring.py` 的 T2）。
    permission_classes = [IsAuthenticated, has_node(WITHDRAW_REVIEW_NODE)]

    pagination_class = WithdrawAdminPagination
    http_method_names = ["get", "post", "head", "options"]

    def permission_denied(self, request, message=None, code=None):
        """给 403 一句可辨的文案（必须转调 `super()`：它先判认证并抛 401）。"""
        super().permission_denied(
            request, message="你没有打手提现审核的权限", code=code
        )

    def get_queryset(self):
        """本租户的提现单。⛔ 不接受租户参数——租户上下文只来自认证态（AGENTS §4）。"""
        return WithdrawOrder.objects.select_related("user", "reviewer").all()

    # ── 读：列表 ──────────────────────────────────────────────────
    def list(self, request, *args, **kwargs):
        raw = {key: value for key, value in request.query_params.items() if value != ""}
        params = _ListQuerySerializer(data=raw)
        if not params.is_valid():
            return api_response(None, msg=_first_error(params), code=ErrorCode.VALIDATION)

        queryset = self.get_queryset()
        if params.validated_data.get("state") is not None:
            queryset = queryset.filter(state=params.validated_data["state"])

        page = self.paginate_queryset(queryset)
        rows = []
        # ⭐ 评审 A4：兜底查单有预算（每页 ≤5 条、总 ≤3 秒）；单条失败只写 `transfer_error`，
        #    ⛔ 不让整页 500。只查**当前页**里的 `paying` 行。
        refreshed = 0
        deadline = time.monotonic() + LIST_REFRESH_BUDGET_SECONDS
        for order in page:
            if (
                order.state == WithdrawOrder.STATE_PAYING
                and refreshed < LIST_REFRESH_MAX_ORDERS
                and time.monotonic() < deadline
            ):
                refreshed += 1
                previous_state = order.state
                try:
                    updated = refresh_transfer(order)
                except (WalletError, JeepayError):
                    order.refresh_from_db()
                else:
                    if updated.state != previous_state:
                        # ⭐ 评审建议 1：状态真的变了（落 paid / 落 failed 退款）⇒ 这不是纯读，
                        #    补一条**系统来源**的审计；状态没变就不记（避免刷屏）。
                        self._record_system_refresh(updated, previous_state)
                    order = updated
            rows.append(_row(order))
        return self.get_paginated_response(rows)

    # ── 写：通过 ──────────────────────────────────────────────────
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        order = self.get_object()
        try:
            order = approve_withdraw(order, operator=request.user)
        except WalletError as exc:
            return api_response(None, msg=str(exc), code=exc.code)
        self._record_withdraw_review(
            order,
            "finance.withdraw_approve",
            to_value=_money(order.amount),
            reason="",
        )
        return api_response(_action_payload(order))

    # ── 写：驳回 ──────────────────────────────────────────────────
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        order = self.get_object()
        serializer = _RejectSerializer(data=request.data if isinstance(request.data, dict) else {})
        if not serializer.is_valid():
            return api_response(None, msg=_first_error(serializer), code=ErrorCode.VALIDATION)
        remark = serializer.validated_data["remark"]
        try:
            order = reject_withdraw(order, operator=request.user, remark=remark)
        except WalletError as exc:
            return api_response(None, msg=str(exc), code=exc.code)
        self._record_withdraw_review(
            order,
            "finance.withdraw_reject",
            to_value=_money(order.amount),
            reason=remark,
        )
        return api_response(_action_payload(order))

    # ── 写：财务中断────────────────────────────────
    @action(detail=True, methods=["post"])
    def interrupt(self, request, pk=None):
        """`approved` 上、打款前拦下：钱退回余额。`pending` 请走 reject。"""
        order = self.get_object()
        serializer = _InterruptSerializer(
            data=request.data if isinstance(request.data, dict) else {}
        )
        if not serializer.is_valid():
            return api_response(None, msg=_first_error(serializer), code=ErrorCode.VALIDATION)
        reason = serializer.validated_data["reason"]
        try:
            order = interrupt_withdraw(
                order, operator=request.user, reason=reason, record_audit=False
            )
        except WalletError as exc:
            return api_response(None, msg=str(exc), code=exc.code)
        # 审计由视图的 `_record_withdraw_review` 写（覆盖网按名字解析视图方法的埋点助手）。
        self._record_withdraw_review(
            order,
            "finance.withdraw_interrupt",
            to_value=_money(order.amount),
            reason=reason,
        )
        return api_response(_action_payload(order))

    # ── 写：发起代付 ──────────────────────────────────────────────
    @action(detail=True, methods=["post"])
    def transfer(self, request, pk=None):
        order = self.get_object()
        # ⚠️ 开关关着时**连闸都不让走**（默认关 = 只允许人工打款，计划 §6）。
        if not auto_transfer_enabled():
            return api_response(
                None, msg="自动代付未开启，请登记人工打款", code=ErrorCode.BUSINESS
            )
        serializer = _TransferSerializer(
            data=request.data if isinstance(request.data, dict) else {}
        )
        if not serializer.is_valid():
            return api_response(None, msg=_first_error(serializer), code=ErrorCode.VALIDATION)
        try:
            order = start_transfer(
                order,
                operator=request.user,
                client_ip=_client_ip(request),
                confirm_amount=serializer.validated_data["confirm_amount"],
            )
        except WalletError as exc:
            return api_response(None, msg=str(exc), code=exc.code)
        self._record_withdraw_review(
            order,
            "finance.withdraw_transfer",
            to_value=_money(order.amount),
            reason=order.mch_order_no or "",
        )
        return api_response(_action_payload(order, with_transfer=True))

    # ── 写：查单刷新 ──────────────────────────────────────────────
    @action(detail=True, methods=["post"])
    def refresh(self, request, pk=None):
        order = self.get_object()
        try:
            order = refresh_transfer(order)
        except WalletError as exc:
            return api_response(None, msg=str(exc), code=exc.code)
        self._record_withdraw_review(
            order,
            "finance.withdraw_refresh",
            to_value=_money(order.amount),
            reason=order.get_state_display(),
        )
        return api_response(_action_payload(order, with_transfer=True))

    # ── 写：人工登记打款 ──────────────────────────────────────────
    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        order = self.get_object()
        serializer = _MarkPaidSerializer(
            data=request.data if isinstance(request.data, dict) else {}
        )
        if not serializer.is_valid():
            return api_response(None, msg=_first_error(serializer), code=ErrorCode.VALIDATION)
        transfer_no = serializer.validated_data["transfer_no"]
        try:
            order = mark_withdraw_paid(
                order,
                operator=request.user,
                transfer_no=transfer_no,
                remark=serializer.validated_data.get("remark", ""),
            )
        except WalletError as exc:
            return api_response(None, msg=str(exc), code=exc.code)
        self._record_withdraw_review(
            order,
            "finance.withdraw_mark_paid",
            to_value=_money(order.amount),
            reason=transfer_no,
        )
        return api_response(_action_payload(order))

    # ── 审计埋点（一个人为动作恰好一行）────────────────────────────
    def _record_withdraw_review(self, order, action_code: str, *, to_value: str, reason: str) -> None:
        """写一条操作日志。**每个写动作各落一行**（计划 §4.5 / §7 第 10 条）。
        ⚠️ 5 个动作各自经这一个助手写日志（域内统一埋点助手，与`config_views._record_config` 同形）。
        覆盖率网按**名字**解析助手链（`tests/test_operation_log_coverage.py` 的 `_RECORD_FORWARDERS` 已登记本名字）；⛔ 改本方法名要同批改那张登记表，否则 5 条端点会被判「声称已埋点却找不到埋点」。"""
        actor = getattr(self.request.user, "username", "") or ""
        record(
            log_type=ACTION_LOG_TYPES[action_code],
            action=action_code,
            actor=actor,
            actor_id=self.request.user.pk,
            target_kind="withdraw_order",
            target_id=str(order.pk),
            target_name=_action_name(order),
            to_value=to_value,
            reason=reason,
        )

    def _record_system_refresh(self, order, previous_state: str) -> None:
        """列表兜底查单**真的改了状态**时补一条系统审计（评审建议 1）。

        ⚠️ 来源是机器（打开列表自动查单），⛔ 不伪装成某个客服点的；`record()` 的
        fail-closed 契约要求显式 `system=True` + `actor_id=None`。
        """
        record(
            log_type=ACTION_LOG_TYPES["finance.withdraw_refresh"],
            action="finance.withdraw_refresh",
            actor="系统",
            actor_id=None,
            system=True,
            target_kind="withdraw_order",
            target_id=str(order.pk),
            target_name=_action_name(order),
            to_value=_money(order.amount),
            reason=f"打开列表自动查单：{previous_state} → {order.get_state_display()}",
        )