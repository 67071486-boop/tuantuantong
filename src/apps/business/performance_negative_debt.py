"""挂负账判据的唯一实现处（2026-11-07 报酬页结算筛选返工，计划
`docs/plans/2026-11-07-settle-filter-rework.md` §5）：①「挂负账打手」快照（租户级、与筛选无关；
2026-11-08 撤强制展示后只喂红签与净负判据）与 ②「净负打手」集合（窗口内逐人 Σ +
窗口外负行 Σ 的判定产物）。
⚠️ 范围 = 仅打手域（emp_type=打单工资；业务方 2026-11-08 拍板）：押金抵扣/罚款负单都在打手域，
客服的「销售提成」负行不进快照、不算净负、不亮红签——限定都在本模块与消费侧（序列化器按行 emp_type 判）。
⚠️ 两个判据都不过 `_resolve_batch` 资格闸（被禁结算/池内单也算挂负账——那是账面事实；
结算资格是另一道闸、留在结算侧）。与已撤的「负绩效并入」线（settlement_preview）在这一条上
**有意不同**。⛔ 本模块只读：不写库、不写审计、不碰 settle/revoke 写路径。"""
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.models import (
    Case,
    Count,
    DecimalField,
    ExpressionWrapper,
    F,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from apps.business.models import EmpType, Performance, PerformanceAdjustment, SettleStatus
from apps.business.performance_amount import AmountError
from apps.business.settlement import unsettled_adjustments_prefetch
from apps.business.settlement_amount import payable_net

#: 挂负账快照的缓存键前缀（租户级、与筛选签名无关 ⇒ 不能塞进签名键；防撞口径照
#: `performance_views._PERF_TOTALS_CACHE_PREFIX`）。TTL = `PERFORMANCE_LIST_CACHE_TTL_S`
#: （0 = 紧急开关，两层都直通、零缓存读写）。
NEG_DEBT_CACHE_PREFIX = "perfnegdebt"

ZERO = Decimal("0.00")


def _amount_error_guard():
    """SQL 聚合的 `AmountError` 守卫：排除 `deduct_amount` 会抛的三种行（逐一对应它的三个
    raise，`performance_amount.py:87-100`）——让 SQL 净额与 Python `payable_net` 判据等价：
    `deduct_mode='0' ∧ deduct_value>100`（比例越界）、`deduct_mode='1' ∧ deduct_value>temp_amount`
    （固定额越界）、`deduct_mode` 表外（未知模式）。⚠️ 两列都 `default=""/0.00` 非 NULL ⇒
    不用为 NULL 分支扩写。"""
    return ~(
        Q(deduct_mode="0", deduct_value__gt=Decimal("100"))
        | Q(deduct_mode="1", deduct_value__gt=F("temp_amount"))
        | ~Q(deduct_mode__in=["", "0", "1"])
    )


def _deduct_expression():
    """`deduct_amount` 的 SQL 同构（逐项对齐）：`mode ''/None` 或 value 为 NULL ⇒ 0；
    `mode='0'` ⇒ `temp_amount × value / 100`（Postgres numeric 除法精确，与 Python Decimal
    逐值一致；±0.005/行的量化带偏差只可能出现在净负边界附近——已知边界，计划 §9-4）；
    `mode='1'` ⇒ `value`；其余 ⇒ 0（表外模式已被 `_amount_error_guard` 排除）。"""
    percent = ExpressionWrapper(
        F("temp_amount") * F("deduct_value") / Value(Decimal("100")),
        output_field=DecimalField(),
    )
    return Case(
        When(Q(deduct_mode="0") & ~Q(deduct_value__isnull=True), then=percent),
        When(Q(deduct_mode="1") & ~Q(deduct_value__isnull=True), then=F("deduct_value")),
        default=Value(Decimal("0"), output_field=DecimalField()),
        output_field=DecimalField(),
    )


def net_annotation():
    """行净额的 SQL 表达式（`payable_net` 的同构）：`bonus − 扣点 + Σ未结账调整`。
    调整腿走 **JOIN 聚合**（整表粗筛一次聚合最优）；⚠️ 只能用在**过滤**（`net__lt=0`），
    ⛔ 不能再被 `Sum()` 包一层（聚合套聚合 = FieldError——B 项窗口聚合用
    `net_annotation_per_row()`）。⚠️ 调用方必须同帧施加 `_amount_error_guard()`，
    否则等价性不成立。"""
    adj_sum = Coalesce(
        Sum(
            "adjustments__amount",
            filter=Q(adjustments__settle_status=SettleStatus.UNSETTLED),
        ),
        Value(Decimal("0"), output_field=DecimalField()),
        output_field=DecimalField(),
    )
    return ExpressionWrapper(
        F("bonus_amount") - _deduct_expression() + adj_sum,
        output_field=DecimalField(),
    )


def net_annotation_per_row():
    """行净额的**按行子查询**版（无聚合 ⇒ 可以再被 `Sum()` 包一层）：B 项窗口聚合
    `values("user_id").annotate(Sum(net))` 用它；调整腿 = 每行一条相关子查询
    （窗口行数 = 筛后窗口规模，量级与列表聚合同档）。守卫要求同 `net_annotation()`。"""
    from django.db.models import OuterRef, Subquery

    adj_sum = Subquery(
        PerformanceAdjustment.objects.filter(
            performance=OuterRef("pk"),
            settle_status=SettleStatus.UNSETTLED,
        )
        .values("performance")
        .annotate(total=Sum("amount"))
        .values("total")[:1],
        output_field=DecimalField(),
    )
    return ExpressionWrapper(
        F("bonus_amount")
        - _deduct_expression()
        + Coalesce(
            adj_sum,
            Value(Decimal("0"), output_field=DecimalField()),
            output_field=DecimalField(),
        ),
        output_field=DecimalField(),
    )


def _coarse_negative_rows():
    """挂负账粗筛（一条 SQL）：未结账 ∧ 行未软删 ∧ 单未软删 ∧ 有归属人，净额 < 0。
    ⛔ 只认打手域（emp_type=打单工资）：客服的「销售提成」负行不进快照。"""
    return (
        Performance.objects.filter(
            settle_status=SettleStatus.UNSETTLED,
            emp_type=EmpType.MAIN,
            deleted_at__isnull=True,
            order__deleted_at__isnull=True,
            user__isnull=False,
        )
        .filter(_amount_error_guard())
        .annotate(net=net_annotation())
        .filter(net__lt=0)
        .order_by("id")
    )


def _compute_snapshot():
    """快照本体：粗筛 SQL 一条 → Python 精判（`payable_net` + 未结账调整 prefetch）。
    `AmountError` 行丢弃（与已撤线 `settlement_preview._unsettled_negative_groups` 的
    同一处置口径）；产出 `{user_id: [(performance_id, Decimal net), …]}`。"""
    snapshot = {}
    for performance in _coarse_negative_rows().prefetch_related(
        unsettled_adjustments_prefetch()
    ):
        try:
            net = payable_net(performance, performance.unsettled_adjustments)
        except AmountError:
            continue
        if net >= ZERO:
            continue
        snapshot.setdefault(performance.user_id, []).append((performance.id, net))
    return snapshot


def negative_debt_snapshot():
    """挂负账快照：`{user_id: [(performance_id, net), …]}`——**账面事实**、与当前筛选无关
    ⇒ 租户级缓存一份（键 `perfnegdebt:<schema>`，TTL = `PERFORMANCE_LIST_CACHE_TTL_S`），
    稳态每 45s 至多一次聚合（列表红签与净负排除每次请求都取它；45s 陈旧只影响红签，
    行集按实时筛选）。`ttl <= 0` 直通。"""
    ttl = settings.PERFORMANCE_LIST_CACHE_TTL_S
    if ttl <= 0:
        return _compute_snapshot()
    key = NEG_DEBT_CACHE_PREFIX + ":" + connection.schema_name
    cached = cache.get(key)
    if cached is not None:
        return cached
    snapshot = _compute_snapshot()
    cache.set(key, snapshot, ttl)
    return snapshot


def _compute_net_negative(snapshot, window_qs):
    """净负判定本体：`S(u) = 快照内逐人 Σ（A 项、Python 精确）+ 窗口内非快照行 Σ（B 项、SQL）`。
    去重结构性成立：B 已把快照行剔出窗口侧 ⇒ 两集合不交、负行在窗口内只计一次。
    ⛔ B 项窗口同样只认打手域（emp_type=打单工资）：调用方窗口不带 emp_type 时在这里补上，
    与快照（A 项）同款限定、客服负行不算净负。
    `AmountError` 守卫同帧施加；边界复核照计划 §9-4：`|B(u)| ≤ 0.01×行数` 的打手补一次
    Python 精算（量化带只在这个量级影响判定）。"""
    totals = {
        user_id: sum((net for _pid, net in rows), ZERO)
        for user_id, rows in snapshot.items()
    }
    snapshot_ids = {pid for rows in snapshot.values() for pid, _net in rows}
    window = (
        window_qs.filter(emp_type=EmpType.MAIN)
        .exclude(id__in=snapshot_ids)
        .filter(_amount_error_guard())
        .annotate(net=net_annotation_per_row())
    )
    b = {
        row["user_id"]: (row["window_sum"], row["row_count"])
        for row in window.values("user_id")
        .annotate(window_sum=Sum("net"), row_count=Count("id"))
        if row["user_id"] is not None and row["window_sum"] is not None
    }
    for user_id, (window_sum, row_count) in b.items():
        totals[user_id] = totals.get(user_id, ZERO) + window_sum
    # 边界复核：把量化带内的 B 项换回 Python 逐行精算（量级极小，只发生在净负边界附近）。
    for user_id, (window_sum, row_count) in b.items():
        if abs(window_sum) > Decimal("0.01") * row_count:
            continue
        exact = ZERO
        for performance in (
            window.filter(user_id=user_id).prefetch_related(
                unsettled_adjustments_prefetch()
            )
        ):
            try:
                exact += payable_net(performance, performance.unsettled_adjustments)
            except AmountError:
                continue
        totals[user_id] = totals.get(user_id, ZERO) - window_sum + exact
    return {user_id for user_id, total in totals.items() if total < 0}


def net_negative_user_ids(snapshot, window_qs, *, cache_key):
    """净负打手 id 集（「排除整人」的判据）。随签名缓存（键由视图给：
    `perfr3:<schema>:<user>:<filters>`，TTL 同全局）；命中后零聚合 SQL。"""
    ttl = settings.PERFORMANCE_LIST_CACHE_TTL_S
    if ttl > 0:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    result = _compute_net_negative(snapshot, window_qs)
    if ttl > 0:
        cache.set(cache_key, result, ttl)
    return result
