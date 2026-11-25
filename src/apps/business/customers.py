"""客户（会员）档案的领域层：写入与读面聚合的**唯一实现**（2026-11-10 客户线）。

写入点恰好三个（契约网 = `tests/test_customer_members.py::CustomerWritePathNetTests`）：
- W1 录单：`order_intake.create_order` 载荷带可选 `buyer_eid` ⇒ `upsert_customer` + 连单；
- W2 存量回填：管理命令 `backfill_xgj_customers`（同一份 upsert，不抄第二份）；
- W3 来源订单（2026-11-11 第五批）：收割运维脚本（服务器 /tmp，**不进仓**）⇒
  `upsert_source_order` 落 `CustomerSourceOrder` 快照。

身份键 = 闲管家 `buyer_eid`（闲鱼体系内唯一，docs/XGJ-INTEGRATION.md §5）；
⛔ 不按昵称去重（2026-11-10 业务方拍板，脱敏昵称会撞车）。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import (
    BigIntegerField,
    Count,
    DateTimeField,
    DecimalField,
    F,
    Max,
    Min,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    Value,
    ExpressionWrapper,
)
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from apps.business.models import Customer, CustomerSourceOrder, Order

#: `Customer.buyer_nick` 的列宽（models.py 是唯一出处，这里引用同一数字）。
BUYER_NICK_MAX_LENGTH = Customer._meta.get_field("buyer_nick").max_length

#: 来源单无效状态（第六批 2026-11-11 业务方拍板剔除：已退款 / 已关闭；
#: **待付款 11 保留**——未构成消费但单存在，要再剔除改这一行）。
SOURCE_ORDER_INVALID_STATUSES = (23, 24)

#: 抽屉合并列表来源单侧的截断保护上限（计划 §2.3：单客户量级 ≤ 数百；超限取最近的
#: 这么多条防失控，`total` 随之封顶）。
SOURCE_ORDERS_MERGE_CAP = 2000

#: 抽屉合并列表系统单侧的截断保护（与来源侧同款同值）：写入方 = 本租户自己的录单，
#: 单客户量级同样 ≤ 数百；`Order.Meta.ordering` 本就是 `-created_at, -id`，
#: 切片即「最近优先」。
SYSTEM_ORDERS_MERGE_CAP = SOURCE_ORDERS_MERGE_CAP


def valid_source_orders() -> QuerySet:
    """**有效来源单**全集（口径唯一实现：members 行统计与抽屉合并列表共用这一份）：

    ① 排除无效状态（23 已退款 / 24 已关闭；码表 docs/XGJ-INTEGRATION.md §5）；
    ② 排除 `source_order_no` 命中**任意在库系统订单**者——同一张单已由系统单代表，
       不剔除会订单数 / 累计消费双计（春雨实测：10 张系统单全部含在其 26 张来源单里）。
    Order 默认 manager 已排除软删单：系统单代表被删 ⇒ 来源单侧恢复可见（不双删）。
    """
    return CustomerSourceOrder.objects.exclude(
        order_status__in=SOURCE_ORDER_INVALID_STATUSES
    ).exclude(
        source_order_no__in=Order.objects.exclude(source_order_no="").values(
            "source_order_no"
        )
    )


def drawer_source_orders(customer: Customer) -> QuerySet:
    """抽屉合并列表的来源单侧：该客户的有效来源单，`updated_time` 倒序（NULL 最后）、
    截断到 `SOURCE_ORDERS_MERGE_CAP`；系统单侧由视图按 `customer.orders` 取并截断
    `SYSTEM_ORDERS_MERGE_CAP`。"""
    return (
        valid_source_orders()
        .filter(customer=customer)
        .order_by(F("updated_time").desc(nulls_last=True), "-id")[
            :SOURCE_ORDERS_MERGE_CAP
        ]
    )


def upsert_customer(*, eid: str, buyer_nick: str = "") -> tuple[Customer, bool]:
    """按 `eid` 取或建客户档案；`buyer_nick` 非空且与现值不同则刷新快照（W1/W2 共用这一份，E-0021）。

    昵称**裁到列宽**而不是拒（展示快照，⛔ 不因超宽把录单挡成 1000/500）；`eid` 是身份键、
    不裁不兜——超宽在入口序列化器（W1）/ 计数跳过（W2）显式处理。`created` 只被 W2 终态计数消费。
    """
    customer, created = Customer.objects.get_or_create(eid=eid)
    if buyer_nick and buyer_nick != customer.buyer_nick:
        customer.buyer_nick = buyer_nick[:BUYER_NICK_MAX_LENGTH]
        customer.save(update_fields=["buyer_nick", "updated_at"])
    return customer, created


def upsert_source_order(
    *,
    customer: Customer,
    source_order_no: str,
    order_status: int = 0,
    total_amount: Decimal | str | None = None,
    updated_time=None,
    shop_name: str = "",
) -> tuple[CustomerSourceOrder, bool]:
    """按来源单号幂等 upsert 一条来源订单快照（W3；唯一写入方 = 收割运维脚本，计划 §2.1）。

    匹配键 = `source_order_no`（租户 schema 内唯一）；已存在 ⇒ 整行快照刷新、不新建。
    `total_amount` 收**元**（分→元的换算在脚本，上游是分）；金额 None 按 0.00（E-0021：
    0 是合法边界，不是缺值）。`updated_time` 收 aware datetime（秒级时间戳的转换在脚本）。
    """
    if total_amount is None:
        total_amount = Decimal("0.00")
    return CustomerSourceOrder.objects.update_or_create(
        customer=customer,
        source_order_no=source_order_no,
        defaults={
            "order_status": order_status,
            "total_amount": Decimal(total_amount),
            "updated_time": updated_time,
            "shop_name": shop_name,
        },
    )


def member_scope_q(
    *,
    order_source: int | None = None,
    recorder: str | None = None,
    phone: str | None = None,
    self_only_creator: str | None = None,
) -> Q:
    """会员列表的筛选判据（打在**订单**上）：来源字典 / 录单人精确值 / 电话包含匹配，可选、可叠加。

    行聚合与「最近一单」共用这一份，避免两处各写一份筛选。
    `self_only_creator` = 限制档（只持 `customer.member_list_self_only`）时由服务端**强制**
    传入（等于把自己的登录账号 AND 进去，客户端传什么都不可能放大范围——服务端收口）。
    """
    condition = Q()
    if order_source is not None:
        condition &= Q(order_source_id=order_source)
    if recorder:
        condition &= Q(created_by=recorder)
    if phone:
        condition &= Q(customer_phone__icontains=phone)
    if self_only_creator:
        condition &= Q(created_by=self_only_creator)
    return condition


def restricted_member_customer_q(username: str) -> Q:
    """限制档的**行集**判据（计划 §2.4，2026-11-11 钉死）：
    行 = 「录单人（`Order.created_by`）= 自己的**系统订单**所连的客户」。

    ⛔ 不能写裸跨关系 `Q(orders__created_by=…)`：那是 JOIN——同客户 N 张自己的单把
    `count()` 乘成 N，且 JOIN 不走 OrderManager ⇒ 软删单混入。这里打**客户侧** IN
    子查询（走默认 manager、IN 天然去重，SQL 实测无 JOIN），筛选键与会员行集共用
    `member_scope_q` 这一份。来源单没有录单人 ⇒ 来源单-only 客户对此类用户**不可见**；
    两节点都无的用户在端点守卫就被 403，走不到这里。
    """
    return Q(
        pk__in=Order.objects.filter(member_scope_q(self_only_creator=username)).values(
            "customer_id"
        )
    )


#: members 排序的两档（视图序列化器是同一集合的 choice 校验口）。
MEMBER_ORDER_FIELDS = ("order_count", "total_amount")


def _system_stats_subqueries(base_orders: QuerySet) -> QuerySet:
    """系统单侧的分组相关子查询：内层按 `customer_id` 分组、与外层该客户 `pk` 相关。"""
    return (
        base_orders.filter(customer_id=OuterRef("pk"))
        .values("customer_id")
        .annotate(
            order_count=Count("id"),
            last_order_at=Max("created_at"),
            total_amount=Sum("total_price"),
        )
    )


def _source_stats_subqueries() -> QuerySet:
    """有效来源单侧的分组相关子查询（与系统单侧同形态；口径 = `valid_source_orders`）。"""
    return (
        valid_source_orders()
        .filter(customer_id=OuterRef("pk"))
        .values("customer_id")
        .annotate(
            order_count=Count("id"),
            last_order_at=Max("updated_time"),
            total_amount=Sum("total_amount"),
        )
    )


def members_queryset(
    base_orders: QuerySet, order_by: str = "order_count", *,
    require_matching_order: bool = False,
    with_source_stats: bool = True,
) -> QuerySet:
    """会员行 = **全部建档客户**；行统计 = **系统订单 + 有效来源订单合并**（第六批
    2026-11-11 业务方拍板）：订单数 / 累计消费 = 两段各自聚合后相加（⚠️ 各自 Coalesce 0
    再相加——NULL 直接相加得 NULL，零单行会塌成 NULL）；最近下单 = 两段 Max 取大者
    （Postgres GREATEST 忽略 NULL、两段全 NULL ⇒ NULL，「零单 = 空串」序列化语义不变）。
    有效来源单口径 = `valid_source_orders`（排除 23/24 + 已在库系统单同号——同单两面
    只算一张）。`with_source_stats=False` = 限制档（self_only）：来源单无录单人无法
    归属 ⇒ 统计保持系统单口径。筛选只作用系统订单，来源单侧聚合不随筛选变。
    聚合 = 按 `customer_id` 分组的相关子查询（两个 FK 列都有索引；147k 客户 / 35k 订单
    量级实测秒内，比 JOIN 形态更快）——⛔ 两段不能混进一条跨关系 JOIN：连乘会把读数
    乘上另一侧行数（2026-11-11 hotfix 的病灶）。`require_matching_order` = 有筛选键 /
    限制档时置 True：行可见 = 筛选范围内 ≥1 系统单（来源单-only / 裸档客户清空筛选才见）
    ——可见性判据用客户侧 `pk__in` 子查询（裸跨关系 Q 会被提升成 JOIN，注水后无
    GROUP BY 去重，行随命中单数翻倍）。Order 默认 manager 排除已删。
    排序 = `order_by` 一律降序 + `nulls_last` + `-pk` 稳定 tie-break。
    """
    rows = Customer.objects.all()
    if require_matching_order:
        rows = rows.filter(pk__in=base_orders.values("customer_id"))
    sys_grouped = _system_stats_subqueries(base_orders)
    sys_count = Coalesce(
        Subquery(sys_grouped.values("order_count"), output_field=BigIntegerField()),
        Value(0),
        output_field=BigIntegerField(),
    )
    sys_last = Subquery(sys_grouped.values("last_order_at"), output_field=DateTimeField())
    sys_amount = Coalesce(
        Subquery(
            sys_grouped.values("total_amount"),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
        Value(Decimal("0.00")),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )
    if with_source_stats:
        src_grouped = _source_stats_subqueries()
        src_count = Coalesce(
            Subquery(src_grouped.values("order_count"), output_field=BigIntegerField()),
            Value(0),
            output_field=BigIntegerField(),
        )
        src_last = Subquery(
            src_grouped.values("last_order_at"), output_field=DateTimeField()
        )
        src_amount = Coalesce(
            Subquery(
                src_grouped.values("total_amount"),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
            Value(Decimal("0.00")),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )
        annotations = {
            "order_count": ExpressionWrapper(
                sys_count + src_count, output_field=BigIntegerField()
            ),
            "last_order_at": Greatest(sys_last, src_last, output_field=DateTimeField()),
            "total_amount": ExpressionWrapper(
                sys_amount + src_amount,
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        }
    else:
        annotations = {
            "order_count": sys_count,
            "last_order_at": sys_last,
            "total_amount": sys_amount,
        }
    return rows.annotate(**annotations).order_by(
        F(order_by).desc(nulls_last=True), "-pk"
    )


def latest_orders_for_customers(base_orders: QuerySet, customer_ids: list[int]) -> dict:
    """取「每个客户在筛选范围内最近一单」（行展示列 phone / source / recorder 的取值口）。

    PostgreSQL `DISTINCT ON`（本项目唯一数据库，技术栈锁定）；排序 = 聚合行同一口径
    （客户内 `-created_at, -id`），保证「最近一单」与 `last_order_at` 同源。
    """
    if not customer_ids:
        return {}
    rows = (
        base_orders.filter(customer_id__in=customer_ids)
        .select_related("order_source")
        .order_by("customer_id", "-created_at", "-id")
        .distinct("customer_id")
    )
    return {row.customer_id: row for row in rows}


# ── 客户分析报表（2026-11-10 第三批，读聚合；视图出口在 api/customer_views.py）──────

#: 时间范围三档（api 序列化器的 choice 校验口与此同源；缺省 = 30d）。
ANALYSIS_RANGES = ("30d", "90d", "12m")

#: 档 → 天数（窗口含首含尾：start = today − (N−1)，end = today，与 dashboard/trend
#: 的 `created_at__date` 同口径）。
ANALYSIS_RANGE_DAYS = {"30d": 30, "90d": 90, "12m": 365}

#: 消费分层四档的档界（Decimal 比较，恰好落在界上归**高**一档；业务方拍板 0-100/100-500/500-2000/2000+）。
CONSUME_TIER_BOUNDS = (Decimal("100"), Decimal("500"), Decimal("2000"))

#: 消费分层四档的展示名（唯一出处；前端柱状图 x 轴直接用）。
CONSUME_TIER_LABELS = ("0-100", "100-500", "500-2000", "2000+")

#: 客户增长的固定月数（不随 range 变）。
GROWTH_MONTHS = 12

#: 未挂来源字典且 `source` 快照为空时来源行的显示名（report_stats.UNSPECIFIED 同口径）。
ANALYSIS_SOURCE_UNSET = "未指定"

#: 聚合金额的量化位（两位；视图出口按它格式化成字符串）。
ANALYSIS_CENT = Decimal("0.01")


def analysis_window(range_key: str) -> tuple[date, date]:
    """range 档 → 本地日期窗 `[start, end]`（含两端；订单按 `created_at__date` 落窗）。"""
    days = ANALYSIS_RANGE_DAYS[range_key]
    today = timezone.localdate()
    return today - timedelta(days=days - 1), today


def _period_orders(start: date, end: date) -> QuerySet:
    """本期订单基集 = **已连客户**的单（与会员行同口径；未连单不属于任何客户，不进客户分析）。"""
    return Order.objects.filter(
        customer__isnull=False,
        created_at__date__gte=start,
        created_at__date__lte=end,
    )


def analysis_kpi(start: date, end: date) -> dict:
    """KPI 五键（Decimal 原样返回，字符串形状由视图出口统一格式化）。

    `customer_total` 恒 = 客户表全量（与会员列表 `total` 同一口径，不随 range 变）；
    `repeat_customers` = 窗口内 ≥2 单的客户数；`period_amount` 含负数单（退款冲减，D-0015）；
    `avg_order` = 本期消费 ÷ 本期订单数（半单不除零，无单 = 0.00）。
    """
    base = _period_orders(start, end)
    totals = base.aggregate(order_count=Count("id"), amount=Sum("total_price"))
    order_count = totals["order_count"]
    amount = totals["amount"] if totals["amount"] is not None else Decimal("0.00")
    repeat = base.values("customer_id").annotate(c=Count("id")).filter(c__gte=2).count()
    if order_count > 0:
        avg = (amount / order_count).quantize(ANALYSIS_CENT, rounding=ROUND_HALF_UP)
    else:
        avg = Decimal("0.00")
    return {
        "customer_total": Customer.objects.count(),
        "repeat_customers": repeat,
        "period_amount": amount,
        "period_orders": order_count,
        "avg_order": avg,
    }


def analysis_source_dist(start: date, end: date) -> list[dict]:
    """本期各来源的消费合计（扇形数据，金额降序）。

    显示名 = `order_source_display_name` 的分组化：挂字典按 (FK id, 字典当前名) 分组；
    未挂字典按 per-order `source` 快照文本分组（FK 已删/没挂时显示的就是快照），
    空快照归「未指定」。负金额行照实返回（扇形侧自己滤）。
    """
    base = _period_orders(start, end)
    rows: list[dict] = []
    for row in (
        base.filter(order_source__isnull=False)
        .values("order_source_id", "order_source__name")
        .annotate(amount=Sum("total_price"))
    ):
        amount = row["amount"] if row["amount"] is not None else Decimal("0.00")
        rows.append(
            {
                "order_source": row["order_source_id"],
                "name": (row["order_source__name"] or "").strip() or ANALYSIS_SOURCE_UNSET,
                "amount": amount,
            }
        )
    for row in (
        base.filter(order_source__isnull=True)
        .values("source")
        .annotate(amount=Sum("total_price"))
    ):
        amount = row["amount"] if row["amount"] is not None else Decimal("0.00")
        rows.append(
            {
                "order_source": None,
                "name": (row["source"] or "").strip() or ANALYSIS_SOURCE_UNSET,
                "amount": amount,
            }
        )
    rows.sort(key=lambda row: (-row["amount"], row["name"]))
    return rows


def analysis_source_customer_dist(start: date, end: date) -> list[dict]:
    """本期各店铺「有订单的客户数」（扇形数据，第七批 §1.3；客户数降序）：客户按
    `customer_id` 去重、店內只计一次；系统单按来源显示名（同 `analysis_source_dist`）、
    有效来源单按 `shop_name`，同名桶合并；窗口 = 系统单 `created_at` / 来源单
    `updated_time`（NULL 不落窗），来源单侧走 `valid_source_orders` 口径。
    """
    customers_by_name: dict[str, set[int]] = {}
    base = _period_orders(start, end)
    for row in base.filter(order_source__isnull=False).values(
        "order_source__name", "customer_id"
    ).distinct():
        name = (row["order_source__name"] or "").strip() or ANALYSIS_SOURCE_UNSET
        customers_by_name.setdefault(name, set()).add(row["customer_id"])
    for row in base.filter(order_source__isnull=True).values(
        "source", "customer_id"
    ).distinct():
        name = (row["source"] or "").strip() or ANALYSIS_SOURCE_UNSET
        customers_by_name.setdefault(name, set()).add(row["customer_id"])
    for row in (
        valid_source_orders()
        .filter(updated_time__date__gte=start, updated_time__date__lte=end)
        .values("shop_name", "customer_id")
        .distinct()
    ):
        name = (row["shop_name"] or "").strip() or ANALYSIS_SOURCE_UNSET
        customers_by_name.setdefault(name, set()).add(row["customer_id"])
    rows = [{"name": name, "count": len(ids)} for name, ids in customers_by_name.items()]
    rows.sort(key=lambda row: (-row["count"], row["name"]))
    return rows


def analysis_consume_tiers() -> list[dict]:
    """消费分层四档的客户数（按客户**全时段**累计消费分档，不随 range 变）。

    零单客户按累计 0 落「0-100」档；负合计（退款冲减超过消费）同落该档。
    """
    totals = (
        Order.objects.filter(customer__isnull=False)
        .values("customer_id")
        .annotate(total=Sum("total_price"))
    )
    counts = [0, 0, 0, 0]
    for row in totals:
        total = row["total"] if row["total"] is not None else Decimal("0.00")
        tier = len(CONSUME_TIER_BOUNDS)
        for index, bound in enumerate(CONSUME_TIER_BOUNDS):
            if total < bound:
                tier = index
                break
        counts[tier] += 1
    no_order = Customer.objects.count() - len(totals)
    counts[0] += max(no_order, 0)
    return [
        {"label": label, "count": count}
        for label, count in zip(CONSUME_TIER_LABELS, counts)
    ]


def analysis_growth() -> list[dict]:
    """近 12 个月按**首单时间**分月的新增客户数（月序升序、无首单的月补 0）。

    ⛔ 不看 `Customer.created_at`——存量建档时间全是回填同一天（计划 §1），按它会
    把全部新增画成一根柱。首单更早（窗口外）的客户不计。
    """
    today = timezone.localdate()
    months: list[str] = []
    year, month = today.year, today.month
    for _ in range(GROWTH_MONTHS):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    months.reverse()
    counts: dict[str, int] = dict.fromkeys(months, 0)
    firsts = (
        Order.objects.filter(customer__isnull=False)
        .values("customer_id")
        .annotate(first_order=Min("created_at"))
    )
    for row in firsts:
        key = timezone.localtime(row["first_order"]).strftime("%Y-%m")
        if key in counts:
            counts[key] += 1
    return [{"month": name, "count": counts[name]} for name in months]
