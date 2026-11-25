"""打手「我的收入」的两个 App 端点（spec §3.2 / §3.4）：`GET /api/app/income/summary/`（四个数 + 四个计数 + 提前结算额度）、`GET|POST /api/app/income/settle-requests/`（我的申请 + 额度 / 申请一条，只收 `performance_id`）。口径（spec §2，业务方定）：**只算「我接的单」**（`Performance.user = request.user` ∧ `emp_type=打单工资` ∧ 订单未软删，与 `my-orders` 的 `get_queryset()` 同一组条件；⭐「预算」再排掉作废类 `REFUNDED`——本项目没有「已取消」档，逐条理由见 `BUDGET_VOID_ORDER_STATUSES`）；**预算 / 实算用「接手价」**（`effective_taker_price`，⛔ 不另写一套取价；两边都没价 ⇒ `MissingPriceError` ⇒ 该行**不计入金额**，⛔ 不回落 `0.00`）；**未结 / 已结用「绩效金额」**（`bonus_amount`，财务实际打的是这个数）；金额一律两位小数字符串（`apps.openapi.serializers.money`），算不出给 `null`、一档一行都没有 ⇒ `"0.00"`（空集的和是 0，是真值不是「算不出」）。
鉴权与平面：`AppPlaneMixin` + `IsAuthenticated`——`public` 平面一律 404、匿名 401（与 `/api/app/wallet/*` 同套）；⛔ 不接受 `user_id` / 租户标识参数，人一律取 `request.user`（`AGENTS.md` §4）；表在租户库 ⇒ 跨租户天然不可达。⛔ 申请不改任何金额与状态（spec §3.4 第 7 条）：本模块**只写 `PerformanceSettleRequest` 一行**（不写绩效行 / 钱包 / 订单），真正打款仍是财务的「改结算状态」（`settlement.settle_performances`，同事务把本行置 `settled` + 写 `settled_at`）。
"""
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.app_auth_views import AppPlaneMixin
from apps.business.models import (
    EmpType,
    OrderStatus,
    Performance,
    PerformanceSettleRequest,
    SettleStatus,
)
from apps.business.performance_amount import MissingPriceError, effective_taker_price
from apps.business.settle_requests import used_early_settle_count
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset
from apps.openapi.serializers import money

#: 额度上限的**设置键**（`config/settings.py` 的 `EARLY_SETTLE_LIMIT`，spec §3.4 规则 1）。
#: ⚠️ 运行期 `getattr(settings, ...)` 读（⛔ 不在模块顶部 `from django.conf import settings`
#: 之后把这个值抄成常量）：测试的 `override_settings` 必须能改到它。
EARLY_SETTLE_LIMIT_SETTING = "EARLY_SETTLE_LIMIT"
#: 设置缺失 / 非法时的默认额度（业务方 口径 = 3）。
DEFAULT_EARLY_SETTLE_LIMIT = 3

#: 「额度用完」与「这一行不能申请」是两条**不同的**拒绝（见 `apps/core/error_codes.py`
#: 的 1035 / 1036 注释）：前者是人身上的事、后者是单上的事。
INVALID_CODE = ErrorCode.EARLY_SETTLE_INVALID
LIMIT_CODE = ErrorCode.EARLY_SETTLE_LIMIT_EXCEEDED


#: 「预算」**不计入**的订单状态（业务方追加口径：已退单 / 已取消的钱不算接单总额）。
#: ⚠️ 本项目 `OrderStatus` 的八个取值里没有「已取消」档，能表达「这单一轮作废、钱不该再算收入」的**只有 `REFUNDED`（已退单）** ⇒ 只排它；`ISSUE` / `PAUSED` **刻意不排除**——单还在、处理完可能回到进行中，剔出「我接的单」会让接单总额无故变小。⚠️ 退单后绩效行的 `temp_amount` / `bonus_amount` 已被退款钩子清零，但 `taker_price` / `hall_price` 还在 ⇒ 不排它，「预算」会继续把退掉的钱算进去。
BUDGET_VOID_ORDER_STATUSES = (OrderStatus.REFUNDED,)


class EarlySettleError(Exception):
    """提前结算申请的**预期**失败。形状与 `WalletError` / `ClaimError` 同构：
    `code` 直接进信封当业务码、`message` 直接当 `msg`（给 App 显示中文）；⚠️ 在 `transaction.atomic()` 里用**抛异常**退出（⛔ 不 `return`，E-0022）。
    """

    def __init__(self, message, code=INVALID_CODE):
        super().__init__(message)
        self.message = message
        self.code = code


def early_settle_limit():
    """当前生效的额度上限（整数）。
    ⚠️ 判据是「能不能转成非负整数」，不是真值（E-0021）：`0` 是**合法配置**（= 谁都不能申请），⛔ 不能回落到默认 3；非法值（`None` / `"abc"` / 负数）才回落。
    """
    raw = getattr(settings, EARLY_SETTLE_LIMIT_SETTING, DEFAULT_EARLY_SETTLE_LIMIT)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_EARLY_SETTLE_LIMIT
    return max(value, 0)


def my_performances(user):
    """「我接的单」的**唯一**取数口径（summary 与申请校验共用）。
    四条与 `MyOrdersViewSet.get_queryset()` 逐条相同（spec §3.2「与 my-orders 同口径」）：`user=me`、`emp_type=打单工资`、订单未软删、**绩效行自己未软删**；再走一次数据权限卡点（基线 pass-through，但接线必须在——与 my-orders 同形，⛔ 不发明新的可见范围规则，E-0006）。⚠️ **`deleted_at__isnull=True` 是承重的**：垃圾桶只软删**绩效行**、不删订单 ⇒ 少了这条，一条被删的「已完成 · 未结账」行会照样进四数（金额虚高）、还能被成功申请提前结算（白占 1/3 额度而财务在报酬列表里看不到 ⇒ 永远结不掉）。⚠️ 申请创建走的是**同一个函数** ⇒ 判据在 summary 与申请校验两处**同源**，⛔ 不要在任何一处另写一份过滤。
    """
    qs = Performance.objects.select_related("order").filter(
        user=user,
        emp_type=EmpType.MAIN,
        order__deleted_at__isnull=True,
        deleted_at__isnull=True,
    )
    return scope_queryset(user, qs)


def _sum_taker_prices(rows):
    """Σ 接手价（两位小数字符串 / `None`）。
    有行但**一个价都没有** ⇒ `None`（算不出，⛔ 不用 `0.00` 冒充）；一行都没有 ⇒ `"0.00"`（空集的和是 0，能算出来）；部分行没价 ⇒ 只累加有价的那几行（spec §2：「无价 ⇒ **不计入金额**」）。
    """
    total = Decimal("0.00")
    priced = False
    for row in rows:
        try:
            value = effective_taker_price(row)
        except MissingPriceError:
            # ⚠️ **只吞这一档**（两边都没价，语义明确 = 这行没有接手价）。
            # 其它金额异常（数据坏了、`Decimal` 转不动）照旧响亮上抛，⛔ 不折成 0。
            continue
        total += value
        priced = True
    if rows and not priced:
        return None
    return money(total)


def _sum_bonus(rows):
    """Σ 绩效金额（两位小数字符串）。
    `bonus_amount` 是 `null=False default=0.00` 的列 ⇒ 永远算得出来，**不会**回 `None`（空集 ⇒ `"0.00"`）；行值是 `Decimal`（`DecimalField`），这里不引入 float。
    """
    total = Decimal("0.00")
    for row in rows:
        total += row.bonus_amount
    return money(total)


def early_settlement_payload(user):
    """额度出参：`{"used": n, "limit": m}`（spec §3.2 的形状，恰好两个键）。
    ⚠️ **`used` 只数 `pending` / `settled`**（spec §8.1：`cancelled` 不占额度，作废后可重新申请）；口径的唯一出处是 `apps.business.settle_requests.QUOTA_STATES`（申请时的额度判定调**同一个**函数）⇒ ⛔ 不要在这里写 `filter(user=...)` 全量 count。⚠️ **不返回 `remaining`**：spec 给的形状只有两个键，剩余由调用方 `limit - used` 算（多一个键改契约）。
    """
    return {
        "used": used_early_settle_count(user),
        "limit": early_settle_limit(),
    }


def income_summary(user):
    """四个数 + 四个计数 + 额度（spec §3.2 的**完整** `data`）。四档口径（spec §2；`budget` 带追加口径）：`budget` = 我接的**全部**单（含进行中）去掉作废类（`REFUNDED`，见 `BUDGET_VOID_ORDER_STATUSES`）的 Σ 接手价，计数 = 剩下的行数；`actual` = 其中订单已完成的 Σ 接手价（退单单不是「已完成」⇒ 天然不在内）；`unsettled` / `settled` = 已完成单里 `settle_status` 未结账 / 已结账的 Σ **绩效金额**。
    ⚠️ 键顺序照 spec §3.2 的示例（`budget` / `actual` / `unsettled` / `settled` / `counts` / `early_settlement`）——前端按键取，顺序不是契约但别无故打乱。⚠️ 一次 `list()` 取全部行、四档在内存里分（同一批行、四个口径），⛔ 不要为每一档各打一条聚合查询——四条 SQL 各自算价会让「取价逻辑」散成四份。⚠️ **`counts.budget` 与 `budget` 同一组行**：「共 N 单」与那个金额必须指同一批单，否则金额与条数各说一套。
    """
    rows = list(my_performances(user))
    budget_rows = [
        row for row in rows if row.order.status not in BUDGET_VOID_ORDER_STATUSES
    ]
    completed = [row for row in rows if row.order.status == OrderStatus.COMPLETED]
    unsettled = [
        row for row in completed if row.settle_status == SettleStatus.UNSETTLED
    ]
    settled = [row for row in completed if row.settle_status == SettleStatus.SETTLED]
    return {
        "budget": _sum_taker_prices(budget_rows),
        "actual": _sum_taker_prices(completed),
        "unsettled": _sum_bonus(unsettled),
        "settled": _sum_bonus(settled),
        "counts": {
            "budget": len(budget_rows),
            "actual": len(completed),
            "unsettled": len(unsettled),
            "settled": len(settled),
        },
        "early_settlement": early_settlement_payload(user),
    }


def settle_request_payload(row):
    """一条申请的出参（列表与 POST 的响应共用同一个形状）。
    ⚠️ **带 `bonus_amount`**（绩效金额）：App 要显示「我申请的这笔是多少钱」——那是打手自己的钱，⛔ 不是客户付款价（spec §2 红线）。⚠️ 时间走 `local_iso`（项目时区 `+08:00`）：本模块的载荷不是 DRF 序列化器渲染的，裸 `datetime` 会被 JSON 编码器按 UTC 出串（E-0238 家族，见 `datetime_display.py`）。⚠️ `order_no` 走 `select_related` 预取（列表 queryset 已带上），不产生 N+1。
    """
    return {
        "id": row.pk,
        "performance_id": row.performance_id,
        "order_no": row.performance.order.order_no,
        "state": row.state,
        "bonus_amount": money(row.performance.bonus_amount),
        "created_at": local_iso(row.created_at),
        "settled_at": local_iso(row.settled_at),
    }


def create_settle_request(user, performance_id):
    """给 `performance_id` 建一条提前结算申请；返回新建的行（预期失败抛 `EarlySettleError`）。判据顺序（spec §3.4 规则 2）：① **归属**——必须是「我接的」（`my_performances(user)` 命中；不存在 / 不是我的 / 不是打单工资 / 单已软删 ⇒ 同一句「该绩效不存在或不属于你」，合并成一句是**有意**的：分开说等于给越权请求一个探测面）；② 订单已完成；③ `settle_status = 未结账`（已结账 ⇒ 钱已打过）；④ 没有「仍在生效」的已有申请（`pending` / `settled` 都挡，**`cancelled` 不挡**——作废后可重申并**复用那一行**，一对一唯一）；⑤ 额度未用完（`used < limit`，`used` 只数 `pending` / `settled`，spec §8.1）。
    ⚠️ **额度判定必须串行**：额度按人计数、第一笔申请前没有任何行可锁 ⇒ 锁**账号行**（`User.objects.select_for_update()`）让同一人的并发申请排队；⛔ 不要「先 count 再 create」不加锁（两个并发会一起看到 `used = limit - 1`）。⚠️ 唯一约束撞了（`IntegrityError`）⇒ 翻成 1035「已申请过」：预期失败，⛔ 不让它变成 500（E-0010）。
    """
    with transaction.atomic():
        # ① 锁人（见 docstring 的「额度判定必须串行」）。
        User.objects.select_for_update().get(pk=user.pk)
        performance = my_performances(user).filter(pk=performance_id).first()
        # ⚠️ 这里用 `is None` 判，不用真值（E-0021）：主键是数字，但判据的形状要统一。
        if performance is None:
            raise EarlySettleError("该绩效不存在或不属于你")
        order = performance.order
        if order.status != OrderStatus.COMPLETED:
            raise EarlySettleError(
                f"订单未完成（当前：{order.status}），不能申请提前结算"
            )
        # ⚠️ 判据按 spec §3.4 的原文用 `settle_status`（⛔ 不换成 `is_settled` 的三字段并集）：
        # 「是否结过账」在**结账那条链**上由 `is_settled` 兜，这里只回答
        # 「这一行现在还能不能申请」，而 spec 点名的是 `settle_status=未结账`。
        if performance.settle_status == SettleStatus.SETTLED:
            raise EarlySettleError("该绩效已结账，不需要申请提前结算")
        # ⭐（spec §8.1）：**作废（`cancelled`）的申请不挡重申**。同一个绩效行只能有一条申请（`OneToOneField`）⇒ 重申时**复用那一行**，重置成 `pending` + 这一次的申请时间（历史作废事实不在库外留副本，那一行本身就是「当前这条申请」的状态机）。
        # ⚠️ `select_for_update`：与「撤销结账 / 退款把 pending 改成 cancelled」那两个写入点串行（它们走 `update()`，会等这把行锁）。
        existing = (
            PerformanceSettleRequest.objects.select_for_update()
            .filter(performance=performance)
            .first()
        )
        if existing is not None and existing.state != PerformanceSettleRequest.State.CANCELLED:
            raise EarlySettleError("该绩效已申请过提前结算，请等待财务处理")
        used = used_early_settle_count(user)
        limit = early_settle_limit()
        if used >= limit:
            raise EarlySettleError(
                f"提前结算申请额度已用完（{used}/{limit}）",
                LIMIT_CODE,
            )
        if existing is not None:
            # ⚠️ `created_at` 是 `auto_now_add`（只在 insert 时写）⇒ 重申必须**显式**改成这一次的时刻，否则「申请时间」停在上一回（打手与财务都会读错）；`settled_at` 一起清（作废的申请从没被结过账，留着上一轮的结账时刻是假话）。
            # ⚠️ **`user` 也要跟着换**：重指派（`reset_taker_performance` 把旧人的申请作废、绩效行归新人）之后新人重申复用的是**同一条**申请行；不改 `user` 的话这条申请会继续算在**旧人**的额度上（新人 used=0、旧人 used 不降），两边都错。
            existing.user = user
            existing.state = PerformanceSettleRequest.State.PENDING
            existing.settled_at = None
            existing.created_at = timezone.now()
            existing.save(
                update_fields=["user", "state", "settled_at", "created_at"]
            )
            return existing
        try:
            return PerformanceSettleRequest.objects.create(
                performance=performance, user=user
            )
        except IntegrityError as exc:
            # 并发兜底：两笔同单申请里后到的那笔撞唯一约束 ⇒ 与上面那条同名业务拒绝。
            raise EarlySettleError(
                "该绩效已申请过提前结算，请等待财务处理"
            ) from exc


class EarlySettleRequestSerializer(serializers.Serializer):
    """`POST /api/app/income/settle-requests/` 的入参：**只收 `performance_id`**（spec §3.4）。
    ⚠️ 用 `IntegerField`（不是 `CharField`）：主键是整数列，让畸形值在入口就是 `1000`（「字符串直进 filter()」的 500 见 E-0010）。⛔ 不加 `min_value`：`0` / 负数查不到就是「不存在」，由 1035 解释，⛔ 不在入口造 spec 没写过的约束（`SettleRequestSerializer` 同一条理由）。⚠️ **不接收其它字段**：DRF `Serializer` 默认忽略多余键，`{"performance_id": 1, "user_id": 999}` 也不会被采纳——归属永远取认证态。
    """

    performance_id = serializers.IntegerField(
        error_messages={
            "required": "缺少 performance_id",
            "invalid": "performance_id 必须是整数",
        }
    )


def _first_error(serializer):
    """取第一条中文错误（与 `app_wallet_views._first_error` 同形）。"""
    for errors in serializer.errors.values():
        if errors:
            return str(errors[0])
    return "参数不正确"


class AppIncomeSummaryView(AppPlaneMixin, APIView):
    """`GET /api/app/income/summary/`——「我的收入」页的四个数（spec §3.2）。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return api_response(income_summary(request.user))


class AppIncomeSettleRequestsView(AppPlaneMixin, APIView):
    """`GET`（我的申请 + 额度）/ `POST`（申请一条）——spec §3.4 第 3、4 条。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = (
            PerformanceSettleRequest.objects.select_related(
                "performance", "performance__order"
            )
            .filter(user=request.user)
            .order_by("-created_at", "-id")
        )
        return api_response(
            {
                "items": [settle_request_payload(row) for row in rows],
                "early_settlement": early_settlement_payload(request.user),
            }
        )

    def post(self, request):
        if not isinstance(request.data, dict):
            return api_response(None, msg="参数不正确", code=ErrorCode.VALIDATION)
        serializer = EarlySettleRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(
                None, msg=_first_error(serializer), code=ErrorCode.VALIDATION
            )
        try:
            row = create_settle_request(
                request.user, serializer.validated_data["performance_id"]
            )
        except EarlySettleError as exc:
            # 业务失败 HTTP 仍 200（本仓口径：结果由 `code` 表达）。
            return api_response(None, msg=exc.message, code=exc.code)
        return api_response(
            {
                "settle_request": settle_request_payload(row),
                # ⚠️ 额度**当场重算**（申请已落库 ⇒ `used` 已含这一条）。
                # ⛔ 不要拿「申请前的 used + 1」去拼：两条数字来源必然漂。
                "early_settlement": early_settlement_payload(request.user),
            }
        )
