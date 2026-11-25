"""客服接发单统计 / 经营报表的**只读**读取层。 spec：`docs/specs/2026-10-17-support-stats-and-business-report-design.md` 计划：`docs/plans/2026-10-17-support-stats-and-business-report.md` 两个公开函数： - `support_stats(...)`：页面「客服接发单统计」的两张表（按操作人员 / 按店铺）。 - `business_report(...)`：页面「经营报表」的 6 个数字卡 + 4 张图的数据。
## 口径（唯一出处是 spec §2；这里只重复最容易抄错的三条） 1. **接单价 = `Order.total_price`**、**发单价 = `Order.hall_price`**（空按 0 参与求和） —— 订单列表「接单价 / 发单价」两列同源；⛔ 不是 `received_price` / `platform_price`。 2. **预计绩效 = 总接单价 − 总发单价**（页面一）；**利润 = 接单价 − 发单价 − 退款** （页面二，且页面二的 6 卡与 4 图**排除已退单状态**）。页面一**不排除已退单**。
3. 页面二的「接单时间」= 该订单**主绩效行**（`Performance.emp_type=打单工资`）的 `claimed_at`；「交单时间」= `Order.submitted_at`。 ## 数据权限所有读都经过 `scope_queryset(viewer, qs, scope_filter=game_scope("game_id"))`：查看者只能看到自己有权限的游戏（管理员由卡点短路）。租户上下文只来自认证态，本模块**不接受**任何请求参数形式的租户线索（`AGENTS.md` §4）。 ## 金额一律 `Decimal` 计算、接口回**两位小数字符串**（`"0.00"`），避免 JS 浮点误差。负金额与 0 都是合法值（D-0015），⛔ 不做 `min_value` / 截断。"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Count, DateTimeField, F, OuterRef, Subquery, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from apps.accounts.display import user_display_name
from apps.business.game_scope import game_scope
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset

#: 金额零值（`Decimal`，不是 float）。
ZERO = Decimal("0.00")
#: 金额最小单位（两位小数）。
CENT = Decimal("0.01")
#: 没有归属人 / 没有来源时的显示名（spec §2）。
UNSPECIFIED = "未指定"
#: 页面二统计口径（spec §6.2）。
STAT_TYPES = ("claim", "submit")
#: 页面二滚动窗口长度（天；含今天与起点）。
LAST_MONTH_DAYS = 30
THREE_MONTH_DAYS = 90
YEAR_DAYS = 365
#: 趋势图月数（spec §5 第 4 张图：最近 12 个自然月，含本月）。
TREND_MONTHS = 12


class ReportError(Exception):
    """统计页的**预期**失败（参数指向不存在的东西 / 日期区间反了）。
    形状与 `order_stats.OrderStatsError` 同构：`code` 直接进信封当业务码、`message` 当 `msg`。"""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.message = message
        self.code = code


def _today() -> date:
    """当前本地日期。**单独成函数**是为了测试能 patch（6 个滚动窗口都依赖它）。"""
    return timezone.localdate()


def _dec(value) -> Decimal:
    """`None` / `int` / `Decimal` → 两位小数 `Decimal`（金额一律走这里）。"""
    if value is None:
        return ZERO
    return Decimal(value).quantize(CENT)


def _money(value) -> str:
    """金额 → 两位小数字符串（接口契约）。"""
    return f"{_dec(value):.2f}"


def _pct(current, previous) -> str:
    """环比百分数（两位小数字符串）；上月为 0 时记 0（spec §5）。"""
    current = _dec(current)
    previous = _dec(previous)
    if previous == ZERO:
        return "0.00"
    return f"{(current - previous) / previous * 100:.2f}"


def _base_orders(viewer):
    """本租户、未软删、且查看者有游戏权限的订单基集。 ⚠️ `Order.objects` 的默认 manager **已经**排除软删（`OrderManager.get_queryset`），
    ⛔ 这里不再叠一条 `deleted_at__isnull=True`（同一事实两处判据会漂移）。 ⚠️ 本模块**只允许这一处** `Order.objects`：`tests.test_order_read_path_net` 按
    「文件 + 作用域名 + 重数」登记读点；在别的函数里再写一句会各算一个落点。 ⭐（业务方拍板）：镜像卡派生的**隐藏单**不进报表基集——它的 `total_price` 是对端发单价、不是本租户的收入，算进客服业绩 / 经营报表就是虚增。"""
    return scope_queryset(
        viewer,
        Order.objects.exclude(hall_card__isnull=False).exclude(
            upstream_hall_card__isnull=False
        ),
        scope_filter=game_scope("game_id"),
    )


def _filter_game_kefu(qs, *, game_id, kefu_ids):
    """按游戏 / 接单销售客服收窄（两页共用；空 = 不筛）。"""
    if game_id is not None:
        qs = qs.filter(game_id=game_id)
    if kefu_ids:
        qs = qs.filter(sales_user_id__in=kefu_ids)
    return qs


def _with_stat_at(qs, stat_type: str):
    """给订单基集加 `stat_at`（页面二的统计时间）注解。 - `claim`：主绩效行的 `claimed_at`（唯一约束 `uniq_order_main_taker` 保证一个订单
    最多一条 `打单工资` 行）； - `submit`：`Order.submitted_at`。 ⚠️ 用 `Subquery` 而不是 `Min("performances__claimed_at")`：后者会为了取一个时刻
    把绩效表 JOIN 进来，行数虽由唯一约束保证、但 SQL 更难读，也更容易在后续加过滤时悄悄变成多行。"""
    if stat_type == "claim":
        claim_at = Subquery(
            Performance.objects.filter(
                order=OuterRef("pk"), emp_type=EmpType.MAIN
            ).values("claimed_at")[:1],
            output_field=DateTimeField(),
        )
        return qs.annotate(stat_at=claim_at)
    return qs.annotate(stat_at=F("submitted_at"))


def _window(qs, stat_type: str, date_from: date, date_to: date):
    """页面二的时间窗（按 `stat_type` 选列；含两端；没有时刻的行不计）。"""
    if date_from > date_to:
        raise ReportError("开始日期不能晚于结束日期", ErrorCode.VALIDATION)
    return _with_stat_at(qs, stat_type).filter(
        stat_at__isnull=False, stat_at__date__range=(date_from, date_to)
    )


def _aggregate(qs) -> dict:
    """一条 `aggregate` 取四个基础数，`profit` 在这里按 spec §2 算。"""
    row = qs.aggregate(
        order_count=Count("id"),
        total_price=Sum("total_price"),
        total_hall_price=Sum("hall_price"),
        refund_fee=Sum("refund_fee"),
    )
    total_price = _dec(row["total_price"])
    total_hall_price = _dec(row["total_hall_price"])
    refund_fee = _dec(row["refund_fee"])
    return {
        "order_count": row["order_count"] or 0,
        "total_price": total_price,
        "total_hall_price": total_hall_price,
        "refund_fee": refund_fee,
        "profit": total_price - total_hall_price - refund_fee,
    }


def _sales_keyed(qs) -> dict:
    """按「接单销售客服」分组：`{(kind, value): 聚合行}`。 - `sales_user` 非空 ⇒ key = `("user", 用户主键)`，名字取**当前**显示名
    （`apps.accounts.display.user_display_name`：昵称优先、空回落登录账号）； - `sales_user` 为空的老单 ⇒ key = `("text", sales_agent 文本)`，空文本归「未指定」。
    ⚠️ 两类 key 分开：一个真实用户与一条同名的文本快照**不合并**（spec §9 第 9 条），否则「谁是主键」会丢。"""
    result: dict[tuple[str, object], dict] = {}

    rows = qs.filter(sales_user__isnull=False).values("sales_user_id").annotate(
        order_count=Count("id"),
        total_price=Sum("total_price"),
        total_hall_price=Sum("hall_price"),
        refund_fee=Sum("refund_fee"),
    )
    user_ids = [row["sales_user_id"] for row in rows]
    users = get_user_model().objects.in_bulk(user_ids) if user_ids else {}
    for row in rows:
        key = ("user", row["sales_user_id"])
        user = users.get(row["sales_user_id"])
        name = user_display_name(user) if user is not None else f"#{row['sales_user_id']}"
        result[key] = _sales_row(name, row)

    text_rows = qs.filter(sales_user__isnull=True).values("sales_agent").annotate(
        order_count=Count("id"),
        total_price=Sum("total_price"),
        total_hall_price=Sum("hall_price"),
        refund_fee=Sum("refund_fee"),
    )
    for row in text_rows:
        raw_name = row["sales_agent"] or ""
        # ⚠️ key 用**原始文本**、显示名才 strip：`"客服甲"` 与 `"客服甲 "` 若共用一个 key，
        # 后一行会覆盖前一行 ⇒ 静默丢掉一组的数量/金额。
        # 这里宁可显示两行同名（合计仍对），也不合并两组数据。
        name = raw_name.strip() or UNSPECIFIED
        key = ("text", raw_name)
        result[key] = _sales_row(name, row)
    return result


def _sales_row(name: str, row: dict) -> dict:
    total_price = _dec(row["total_price"])
    total_hall_price = _dec(row["total_hall_price"])
    return {
        "name": name,
        "order_count": row["order_count"] or 0,
        "total_price": total_price,
        "total_hall_price": total_hall_price,
        "refund_fee": _dec(row["refund_fee"]),
    }


def _support_rows(grouped: dict) -> tuple[list[dict], dict]:
    """把 `_sales_keyed` 的结果摊成页面要的行 + 合计行（页面一、页面二共用）。"""
    rows = []
    for row in grouped.values():
        total_price = row["total_price"]
        total_hall_price = row["total_hall_price"]
        rows.append(
            {
                "name": row["name"],
                "order_count": row["order_count"],
                "total_price": total_price,
                "total_hall_price": total_hall_price,
                "performance": total_price - total_hall_price,
            }
        )
    rows.sort(key=lambda item: (-item["performance"], item["name"]))
    total = {
        "name": "合计",
        "order_count": sum(item["order_count"] for item in rows),
        "total_price": sum((item["total_price"] for item in rows), ZERO),
        "total_hall_price": sum((item["total_hall_price"] for item in rows), ZERO),
        "performance": sum((item["performance"] for item in rows), ZERO),
    }
    return rows, total


def _source_rows(qs) -> tuple[list[dict], dict]:
    """按 `Order.source` 文本快照分组（页面一的店铺表 + 页面二的来源/利润两张饼共用）。行 = `{name, order_count, total_price, total_hall_price, profit}`；页面一不展示 profit、
    由它自己挑字段，页面二两张饼各取 `total_price` / `profit`——**同一次 group by**， ⛔ 不为了「两张饼」再查一遍。 ⚠️ 用文本快照而不是 `order_source` 外键：字典项被删后 FK 会变空，而历史单仍应按
    当时那个名字统计（spec §2「订单来源 / 店铺」）。改名也不回写历史名（D4/R7 的快照语义）。"""
    rows = []
    for row in qs.values("source").annotate(
        order_count=Count("id"),
        total_price=Sum("total_price"),
        total_hall_price=Sum("hall_price"),
        refund_fee=Sum("refund_fee"),
    ):
        name = (row["source"] or "").strip() or UNSPECIFIED
        total_price = _dec(row["total_price"])
        total_hall_price = _dec(row["total_hall_price"])
        rows.append(
            {
                "name": name,
                "order_count": row["order_count"] or 0,
                "total_price": total_price,
                "total_hall_price": total_hall_price,
                "profit": total_price - total_hall_price - _dec(row["refund_fee"]),
            }
        )
    rows.sort(key=lambda item: (-item["total_price"], item["name"]))
    total = {
        "name": "合计",
        "order_count": sum(item["order_count"] for item in rows),
        "total_price": sum((item["total_price"] for item in rows), ZERO),
        "total_hall_price": sum((item["total_hall_price"] for item in rows), ZERO),
        "profit": sum((item["profit"] for item in rows), ZERO),
    }
    return rows, total


def support_stats(*, date_from: date, date_to: date, viewer) -> dict:
    """页面一：按操作人员 / 按店铺（订单来源）的接发单统计。 - 窗口 = `Order.created_at` 的本地日期，含两端；
    - **不排除已退单**（spec §2 / §9 第 2 条：业务方只要 5 个字段，预计绩效是算术值）；
    - 金额回两位小数字符串。"""
    if date_from > date_to:
        raise ReportError("开始日期不能晚于结束日期", ErrorCode.VALIDATION)
    qs = _base_orders(viewer).filter(created_at__date__range=(date_from, date_to))

    operators, operators_total = _support_rows(_sales_keyed(qs))
    sources, sources_total = _source_rows(qs)

    for row in operators:
        row["total_price"] = _money(row["total_price"])
        row["total_hall_price"] = _money(row["total_hall_price"])
        row["performance"] = _money(row["performance"])
    for row in sources:
        row["total_price"] = _money(row["total_price"])
        row["total_hall_price"] = _money(row["total_hall_price"])
        # `profit` 是页面二两张饼共用的中间量；页面一的契约没有这一列（spec §6.1）。
        row.pop("profit", None)
    for row in (operators_total, sources_total):
        row["total_price"] = _money(row["total_price"])
        row["total_hall_price"] = _money(row["total_hall_price"])
        row.pop("profit", None)
    operators_total["performance"] = _money(operators_total["performance"])

    return {
        "range": {
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
        },
        "operators": operators,
        "operators_total": operators_total,
        "sources": sources,
        "sources_total": sources_total,
    }


def _previous_month(today: date) -> tuple[date, date]:
    """上一个自然月的首尾（`today` 所在月的上一个月）。"""
    first_of_this_month = today.replace(day=1)
    last_of_prev = first_of_this_month - timedelta(days=1)
    return last_of_prev.replace(day=1), last_of_prev


def _trend(qs_all, stat_type: str, today: date) -> dict:
    """最近 12 个自然月（含本月）的订单数 / 接单价 / 环比。
    ⚠️ `qs_all` 是**未排除已退单**的基集（调用方自己决定是否 `exclude`）；本函数负责
    补 `stat_at`、按月聚合、补零成连续月序列、算环比。"""
    first_this_month = today.replace(day=1)
    # 当前月往前第 `TREND_MONTHS - 1` 个月的第一天（2026-10 ⇒ 2025-11）。
    offset = first_this_month.month - 1 - (TREND_MONTHS - 1)
    start_month = date(
        first_this_month.year + offset // 12, offset % 12 + 1, 1
    )
    qs = _with_stat_at(qs_all, stat_type).filter(
        stat_at__isnull=False, stat_at__date__range=(start_month, today)
    )
    by_month: dict[str, dict] = {}
    for row in qs.annotate(month=TruncMonth("stat_at")).values("month").annotate(
        order_count=Count("id"), total_price=Sum("total_price")
    ):
        by_month[row["month"].strftime("%Y-%m")] = {
            "order_count": row["order_count"] or 0,
            "total_price": _dec(row["total_price"]),
        }

    months: list[str] = []
    counts: list[int] = []
    amounts: list[str] = []
    count_growth: list[str] = []
    amount_growth: list[str] = []
    prev_count = 0
    prev_amount = ZERO
    cursor = start_month
    for index in range(TREND_MONTHS):
        label = cursor.strftime("%Y-%m")
        bucket = by_month.get(label, {"order_count": 0, "total_price": ZERO})
        months.append(label)
        counts.append(bucket["order_count"])
        amounts.append(_money(bucket["total_price"]))
        # 第一个月没有「上一个月」可比：记 0，而不是拿窗口外的数据硬算。
        count_growth.append("0.00" if index == 0 else _pct(bucket["order_count"], prev_count))
        amount_growth.append("0.00" if index == 0 else _pct(bucket["total_price"], prev_amount))
        prev_count = bucket["order_count"]
        prev_amount = bucket["total_price"]
        # 下一个月：本月的下一个月 1 号。
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return {
        "months": months,
        "order_counts": counts,
        "amounts": amounts,
        "count_growth": count_growth,
        "amount_growth": amount_growth,
    }


def _kefu_options(qs, stat_type: str, date_from: date, date_to: date) -> list[dict]:
    """「接单客服」多选的候选：筛选窗里有单的销售客服（按当前显示名排序）。
    ⚠️ 不受 `kefu` 筛选本身影响（调用方传**未套 kefu** 的基集）——否则选中一个人之后
    下拉里就只剩他自己，换不回「全部」。"""
    window = _window(qs, stat_type, date_from, date_to)
    ids = (
        window.filter(sales_user__isnull=False)
        .values_list("sales_user_id", flat=True)
        .distinct()
    )
    users = get_user_model().objects.in_bulk(list(ids))
    options = [
        {"id": user.id, "name": user_display_name(user)}
        for user in users.values()
    ]
    options.sort(key=lambda item: (item["name"], item["id"]))
    return options


def _not_refunded(qs):
    return qs.exclude(status=OrderStatus.REFUNDED)


def business_report(
    *,
    date_from: date,
    date_to: date,
    stat_type: str,
    game_id: int | None,
    kefu_ids: list[int] | None,
    viewer,
) -> dict:
    """页面二：6 个数字卡 + 来源分布 + 客服接单统计 + 12 月趋势。
    **6 卡与 4 图一律排除已退单**（对标文案「不含已退单状态」）；唯一例外是
    「客服接单统计」里的**退单量**系列——它数的就是已退单，否则永远是 0。"""
    if stat_type not in STAT_TYPES:
        raise ReportError("stat_type 只能是 claim 或 submit", ErrorCode.VALIDATION)
    if date_from > date_to:
        raise ReportError("开始日期不能晚于结束日期", ErrorCode.VALIDATION)

    if game_id is not None and not Game.objects.filter(
        pk=game_id, deleted_at__isnull=True
    ).exists():
        raise ReportError("游戏不存在", ErrorCode.VALIDATION)

    today = _today()
    base_all = _base_orders(viewer)
    base = _filter_game_kefu(base_all, game_id=game_id, kefu_ids=kefu_ids)
    base_no_kefu = _filter_game_kefu(base_all, game_id=game_id, kefu_ids=None)

    # 6 个数字卡：前 5 个是利润（排除已退单），第 6 个是退款额（同一筛选窗）。
    prev_start, prev_end = _previous_month(today)
    windows = {
        "search_profit": (date_from, date_to),
        "last_month_profit": (today - timedelta(days=LAST_MONTH_DAYS - 1), today),
        "prev_month_profit": (prev_start, prev_end),
        "three_month_profit": (today - timedelta(days=THREE_MONTH_DAYS - 1), today),
        "year_profit": (today - timedelta(days=YEAR_DAYS - 1), today),
    }
    cards: dict[str, str] = {}
    for key, (start, end) in windows.items():
        sums = _aggregate(_not_refunded(_window(base, stat_type, start, end)))
        cards[key] = _money(sums["profit"])
    search_window = _window(base, stat_type, date_from, date_to)
    # 第 6 张卡也按对标文案「不含已退单状态」：只统计**非已退单**订单上的部分退款。
    cards["search_refund"] = _money(
        _aggregate(_not_refunded(search_window))["refund_fee"]
    )

    # 来源分布（筛选窗、排除已退单）：与页面一共用 `_source_rows`，一行同时给金额与利润。
    source_rows, source_total = _source_rows(_not_refunded(search_window))

    # 客服接单统计：订单数 / 金额排除已退单；退单量数已退单（同一窗口、同样的分组键）。
    active_rows = _sales_keyed(_not_refunded(search_window))
    refund_rows = _sales_keyed(search_window.filter(status=OrderStatus.REFUNDED))
    keys = set(active_rows) | set(refund_rows)
    support_rows = []
    for key in keys:
        active = active_rows.get(key)
        refunded = refund_rows.get(key)
        name = (active or refunded)["name"]
        support_rows.append(
            {
                "name": name,
                "order_count": active["order_count"] if active else 0,
                "amount": active["total_price"] if active else ZERO,
                "refund_count": refunded["order_count"] if refunded else 0,
            }
        )
    support_rows.sort(key=lambda item: (-item["order_count"], item["name"]))

    return {
        "range": {
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "stat_type": stat_type,
        },
        "cards": cards,
        "source_distribution": [
            {
                "name": row["name"],
                "amount": _money(row["total_price"]),
                "profit": _money(row["profit"]),
            }
            for row in source_rows
        ],
        "source_total": {
            "amount": _money(source_total["total_price"]),
            "profit": _money(source_total["profit"]),
        },
        "support_stats": {
            "names": [row["name"] for row in support_rows],
            "order_counts": [row["order_count"] for row in support_rows],
            "amounts": [_money(row["amount"]) for row in support_rows],
            "refund_counts": [row["refund_count"] for row in support_rows],
        },
        "trend": _trend(_not_refunded(base), stat_type, today),
        "kefu_options": _kefu_options(base_no_kefu, stat_type, date_from, date_to),
    }
