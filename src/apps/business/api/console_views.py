"""后台首页「数据概览」的**聚合读端点**：`GET /api/business/home/overview/`。对标 = `/Index/console` 的「数据概览」卡片（证据件 `public/replica/index-console.html` 的 `pan1`…`pan13`；对标运行期那段是 `gettoday2()` → `GET /EmpAjax/GetMyDayOperationData`）。⚠️ **口径刻意与对标不同**：对标把窗口交给服务端（`day=` 档位 + `stime=` 区间串），本仓由**前端算好** `start` / `end` 送进来（业务方逐格裁定的口径 4，见 `docs/plans/2026-10-04-order-stats-console-and-price-sync.md` §1 口径 4）。
权限（fail-closed）：判据 = `apps.accounts.permissions.is_account_admin`（= 持「系统管理员」职位；租户平面上与 `apps.core.permissions.is_tenant_admin` **同真同假**），为假 ⇒ **HTTP 403 + 业务码 403**（`apps/core/exceptions.py` 的 `PermissionDenied` 分支就是 `code = 403`）；⛔ 不是「回一份全 0」（那会让前端以为算出来了）、⛔ 不另写一份管理员判据、⛔ 不看 `is_superuser`。⚠️ 这**不是 RBAC 节点判据**：本端点没有以 `home.index` 为判据的位点（它管「主界面访问」，这里管「谁能看概览数据」），所以 `apps/rbac/wiring.py` 里 `home.index` 仍登记在 `PENDING`——⛔ **不要**为了「接线图好看」补一句 `has_node("home.index")`：对系统管理员那次判断恒真（`has_node` 管理员短路）、**一个字都不决定**，写成承重判据就是**假承重**（同族：AGENTS.md §5-E「防御性写法不要写成承重逻辑」）。
窗口 = **`[start 00:00, end+1天 00:00)`**（本地时区 `Asia/Shanghai`，`USE_TZ=True`），含首尾两个整天 ⇒ 订单「落在窗口内」判据 = 本地日期落 `[start, end]`（`created_at__date__gte/__lte`，与 `apps/business/order_stats.py` 的 `claimed_at__date` 同口径；`__date` 是本地日期不是 UTC 日期）；软删订单**不算**（`Order.objects` 默认 manager 已排除）。出参逐格口径见 `home_overview_data` 里的注释；**金额** = 两位小数字符串（同 `selectors.order_list_summary`）、**率** = 一位小数字符串（前端拼 `%`，分母为 0 ⇒ `"0.0"`，⛔ 不是 `NaN`/空串）、**计数** = int。
"""
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, Exists, OuterRef, Sum
from rest_framework import serializers
from rest_framework.views import APIView

from apps.accounts.permissions import IsAccountAdmin
from apps.business.models import EmpType, Order, OrderStatus, Performance
from apps.business.order_stats import COMPLETED_LIKE_STATUSES
from apps.core.envelope import api_response

#: 金额出口的小数位（**两位小数字符串**：这一条全仓一致，与 `selectors.order_list_summary` 同形）。
#: ⚠️ 但**取整模式并不全仓一致**（独立评审核实）：本模块与 `settlement_amount` / `performance_amount` 用 `ROUND_HALF_UP`，而 `order_stats` / `performance_views` 用 `quantize(CENT)` 默认的 `ROUND_HALF_EVEN`（银行家舍入）。⛔ 别把这句写成「唯一口径」——那是**过宽的绝对句**（E-0011）；本模块**有意**取 `ROUND_HALF_UP`（金额的常规口径，与 `jidanpu_member._throw_fee` 同一档）。
CENT = Decimal("0.01")

#: 率出口的小数位（一位小数字符串；前端自己拼 `%`）。
TENTH = Decimal("0.1")

#: 「撤销」两格的常量值。**本仓没有撤销 / 仲裁状态**（`OrderStatus` 只有 8 个互斥状态，没有 CANCELLED / ARBITRATION）⇒ **数据源根本不存在**，不是「暂时算不出来」、更不是「库里有 0 条」。业务方裁定：照对标位置显示 `0` / `0%`（计划 §1 口径 3）。⚠️ 因此这两格**恒为**这两个常量、本模块**不查任何表**取它们；将来真加了撤销状态才回这里换成真查询（那时要同时改计划与前端）。
CANCEL_COUNT = 0
CANCEL_ORDER_RATE = "0.0"


class HomeOverviewQuerySerializer(serializers.Serializer):
    """查询参数的**唯一**校验口（照 `order_stats_views.OrderStatsSearchSerializer` 的写法）。⚠️ 请求来源的值进类型化查询之前一律过序列化器（E-0010）：`?start=garbage` 直接进 `filter()` 会抛 `ValueError`（HTML 500）。
    ⚠️ 两个字段**刻意 `required=False`**：缺参由视图自己判，才能给出「缺少必填参数：start、end」这句**指出缺了哪一个**的话（DRF 默认 required 文案分不出是两者都缺还是缺一个）；格式错误用 `error_messages` 说清是哪个参数、该长什么样。
    """

    start = serializers.DateField(
        input_formats=["%Y-%m-%d"],
        required=False,
        # 只吃 `yyyy-MM-dd`（`strptime`）：`"2026-09-01 00:00:00"` 与 `"20260901"` 都会被拒。
        error_messages={"invalid": "start 格式必须是 yyyy-MM-dd"},
    )
    end = serializers.DateField(
        input_formats=["%Y-%m-%d"],
        required=False,
        error_messages={"invalid": "end 格式必须是 yyyy-MM-dd"},
    )


def _money(value) -> str:
    """金额出口：两位小数字符串。⚠️ **判据用 `is not None`，不用真值**（E-0021 / D-0015）：`0` 与**负数**都是合法且有意义的金额（负值表示退款 / 冲抵），`if value:` 会把 `0.00` 当成「没算出来」；空窗口时 `Sum` 回 `None`（聚合里已给 `default=`，这里仍是那条判据的落点）。"""
    if value is None:
        value = Decimal("0.00")
    return f"{Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def _rate(numerator, denominator) -> str:
    """率出口：一位小数字符串；**分母为 0 ⇒ `"0.0"`**（⛔ 不是 `NaN` / 空串）。⚠️ `denominator == 0` 是**正常**输入（窗口内一单都没有）：表示「没有可算的分母」，既不是 100% 也不是失败。判据写成 `== 0` 而不是真值（E-0021 同族：`Decimal("0")` 是 falsy，`if not denominator` 恰好也对，但显式比较才读得出「这一支是设计的一部分」）。"""
    numerator = Decimal(numerator or 0)
    denominator = Decimal(denominator or 0)
    if denominator == 0:
        return "0.0"
    percent = numerator * Decimal("100") / denominator
    return f"{percent.quantize(TENTH, rounding=ROUND_HALF_UP):.1f}"


def home_overview_data(*, start, end):
    """窗口 `[start, end]`（含首尾整天）上的 12 格聚合。**只读**、不碰 request；返回的键是**逐字契约**（前端按它取值）。性能：4 次单发查询（金额聚合 / 状态分布 / 排除已退单的金额聚合 / MAIN 接手存在性计数），⛔ **不在 Python 里逐单循环**。逐格口径（业务方裁定，⛔ 不再自行更改）：① `add_count` 录单笔数 = 窗口内**录的**订单数（`Order.created_at` 落区间，软删不算）；② `order_price` 订单总额 = `Σ total_price`（窗口内全部单）；③ `hall_price_total` 发单总额 = `Σ hall_price`（`NULL` 按 0）；④ `profit_amount` 预计利润 = `Σ(total_price − hall_price)`、**排除当前状态 = 已退单**；⑤ `profit_rate` 利润率 = 预计利润 ÷ **同一子集**的 `Σ total_price` × 100；⑥ `rec_order_count` 接手数量 = 窗口内的单里**当前有 MAIN 接手人**的笔数（判据 = 存在一条 `Performance(emp_type=MAIN)` 且 `user` 非空的行——⚠️ **不能只判 `emp_type=MAIN`**：每张单建单时就有一条**无主** MAIN 绩效行（`Performance.user` 可空 = 还没人接，见 `models.py` 的 `uniq_order_main_taker`），只判 `emp_type` 会让接手数量恒等于录单笔数、接手率恒 100%，那是**静默算错**；另加两条：绩效行本身未软删、订单未软删）；⑦ `rec_order_rate` = 接手数量 ÷ 录单笔数 × 100；⑧ `cancel_count` / `cancel_order_rate` 恒 `0` / `"0.0"`（本仓没有撤销 / 仲裁状态，数据源不存在，见 `CANCEL_COUNT` 注释）；⑨ `refund_count` = 已退单笔数、`refund_order_rate` = 退单数量 ÷ 录单笔数 × 100（前端 `pan10` 显示 `N(x%)`）；⑩ `finish_count` = 状态 ∈ {待验收, 已完成} 的笔数（**沿用** `order_stats.COMPLETED_LIKE_STATUSES` 这套既有「完单」口径，⛔ 不自己发明第三套：那两态 label 与对标 templet 原文逐字对应）；⑪ `finish_order_rate` = 完成数量 ÷ 录单笔数 × 100。
    ⚠️ 率的**分母一律 = `add_count`（录单笔数）**，除了 `profit_rate`（分子分母必须同子集，见口径 4/5）。
    """
    # 窗口内、未软删的订单（`Order.objects` 默认 manager 就排除 `deleted_at` 非空的行）。
    # ⚠️ 「`[start 00:00, end+1天 00:00)`」有两种**等价**写法：① 本地日期落 `[start, end]`（下面这样）；② 显式 aware datetime 区间 `created_at__gte=<start 00:00>` 且 `created_at__lt=<end+1天 00:00>`。本模块取日期这一支，为的是与 `apps/business/order_stats.py` 的 `claimed_at__date__gte/__lte` 同一口径；⛔ 别只改一半（例如左边 `__date__gte`、右边 `__lt=end`）——那会把 end 那一整天漏掉。
    # ⭐（业务方拍板）：镜像卡派生的**隐藏单**不进这 12 格（它没有客户价 / 不算本租户的一笔生意；漏进去会把录单笔数、订单总额、利润率一起撑大）。
    orders = Order.objects.filter(
        created_at__date__gte=start, created_at__date__lte=end
    ).exclude(hall_card__isnull=False).exclude(
        upstream_hall_card__isnull=False
    )

    totals = orders.aggregate(
        order_price=Sum("total_price", default=Decimal("0.00")),
        hall_price_total=Sum("hall_price", default=Decimal("0.00")),
        add_count=Count("id"),
    )
    add_count = totals["add_count"]

    # 状态分布：**一次** GROUP BY（⛔ 不是逐个状态各查一次）。
    # `.order_by()` 清掉 `Order.Meta.ordering`（照 `order_list_summary` 第 3 条的实测理由：
    # 显式排序的列会被追加进 GROUP BY，把计数拆成一行一个 1、**静默**偏小）。
    status_counts = {
        row["status"]: row["n"]
        for row in orders.order_by().values("status").annotate(n=Count("id"))
    }
    refund_count = status_counts.get(OrderStatus.REFUNDED, 0)
    finish_count = sum(status_counts.get(status, 0) for status in COMPLETED_LIKE_STATUSES)

    # 预计利润：**排除已退单**的同一子集上算（口径 4/5）。
    # ⚠️ 分子分母必须同子集：拿全窗口的 `Σ total_price` 当分母会把利润率算小。
    profitable = orders.exclude(status=OrderStatus.REFUNDED)
    profit_totals = profitable.aggregate(
        total_price=Sum("total_price", default=Decimal("0.00")),
        hall_price=Sum("hall_price", default=Decimal("0.00")),
    )
    # ⚠️ `is not None`（E-0021）：金额 0 与负数都是合法值——利润可以是负的（发单价高于订单价）。
    profit_base_price = profit_totals["total_price"]
    profit_base_hall = profit_totals["hall_price"]
    if profit_base_price is None:
        profit_base_price = Decimal("0.00")
    if profit_base_hall is None:
        profit_base_hall = Decimal("0.00")
    profit_amount = profit_base_price - profit_base_hall

    # 接手数量：**存在性**子查询（`Exists`），一次查询拿到计数。用子查询而不是 `filter(performances__…)` 的 JOIN：JOIN 会让同一张单出现多次，与金额聚合放在一起时会**重复计数金额**；子查询没有这个形态风险。
    # `Performance` 的默认 manager **不**排除软删行（`order_stats._window_rows` 也是显式补的），所以这里显式写 `deleted_at__isnull=True`。
    main_taker = Performance.objects.filter(
        order=OuterRef("pk"),
        emp_type=EmpType.MAIN,
        user__isnull=False,
        deleted_at__isnull=True,
    )
    rec_order_count = orders.filter(Exists(main_taker)).count()

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "add_count": add_count,
        # 口径 2/3 的「窗口内全部单」——与预计利润那一子集**刻意不同**（那个排除已退单）。
        "order_price": _money(totals["order_price"]),
        "hall_price_total": _money(totals["hall_price_total"]),
        "profit_amount": _money(profit_amount),
        "profit_rate": _rate(profit_amount, profit_base_price),
        "rec_order_count": rec_order_count,
        "rec_order_rate": _rate(rec_order_count, add_count),
        # 恒 0：本仓没有撤销 / 仲裁状态，数据源不存在（见 `CANCEL_COUNT` 的注释）。
        "cancel_count": CANCEL_COUNT,
        "cancel_order_rate": CANCEL_ORDER_RATE,
        "refund_count": refund_count,
        "refund_order_rate": _rate(refund_count, add_count),
        "finish_count": finish_count,
        "finish_order_rate": _rate(finish_count, add_count),
    }


class HomeOverviewView(APIView):
    """`GET /api/business/home/overview/?start=YYYY-MM-DD&end=YYYY-MM-DD`。闸门 = `IsAccountAdmin`（它就是 `apps.accounts.permissions.is_account_admin` 的 DRF 包装，⛔ 没有第二份判据）。匿名 ⇒ DRF 先抛 `NotAuthenticated`（**401**，本视图有认证器而这次没认证成功）、登录但非管理员 ⇒ **403 + 业务码 403**（`permission_denied` 只在「有认证器且认证成功」时才抛 `PermissionDenied`）。
    """

    permission_classes = [IsAccountAdmin]

    def _params(self):
        """校验并返回查询参数（非法值 ⇒ `ValidationError` ⇒ **HTTP 200 + `code 1000`**）。"""
        # 空串按「没传」处理：前端清空日期框时会送 `?start=`（那是「没选」而不是非法值）。
        raw = {key: value for key, value in self.request.query_params.items() if value != ""}
        params = HomeOverviewQuerySerializer(data=raw)
        params.is_valid(raise_exception=True)
        data = params.validated_data
        missing = [name for name in ("start", "end") if name not in data]
        if missing:
            raise serializers.ValidationError("缺少必填参数：" + "、".join(missing))
        if data["start"] > data["end"]:
            # ⚠️ `start == end` 是**合法**的（= 只算那一天），只有严格晚于才拒。
            raise serializers.ValidationError("start 不能晚于 end")
        return data

    def get(self, request):
        data = self._params()
        return api_response(home_overview_data(start=data["start"], end=data["end"]))
