"""后台首页 v2 看板（`/v2` V2Dashboard）的四条**聚合只读端点**（trend / game-distribution /
hall-stay / sync-health）。闸门 = 与 `home/overview/` 同一份 `IsAccountAdmin`（⛔ 不另写第二份）；
窗口参数照 `console_views` 的序列化器写法（畸形 / 缺参 ⇒ 200 + `1000`）；窗口判据 =
本地日期落 `[start, end]`（`created_at__date`，与 `order_stats` 同口径），软删单不算
（默认 manager 已排除）。全部只读、零迁移。
"""
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import (
    Avg,
    Count,
    DurationField,
    Exists,
    ExpressionWrapper,
    F,
    Max,
    Min,
    OuterRef,
    Q,
    Sum,
)
from django.db.models.functions import TruncDay
from django.utils import timezone
from rest_framework import serializers
from rest_framework.views import APIView

from apps.accounts.permissions import IsAccountAdmin
from apps.business.models import (
    DailiantongPush,
    EmpType,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
    PeerSyncEvent,
    Performance,
)
from apps.core.envelope import api_response

#: 金额出口的小数位（两位小数字符串，与 `console_views._money` 同形同取整）。
CENT = Decimal("0.01")

#: Game.name 为空串时的展示名。
UNGROUPED = "未分组"

#: 闲管家的同步健康没有事实源（发货队列在内存不落库、口径未拍板）⇒ 三键回 null、
#: 前端显示「—」；拍板后回来把这一格换成真查询，⛔ 不编数。
XGJ_CHANNEL_NAME = "闲管家"


def _money(value) -> str:
    """两位小数字符串；`None`（空窗口的 `Sum`）按 0。判据 `is not None`（E-0021）。"""
    if value is None:
        value = Decimal("0.00")
    return f"{Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def _minutes(duration) -> "int | None":
    """timedelta → 取整分钟；`None`（无样本）原样回 `None`，⛔ 不给 0 假数。"""
    if duration is None:
        return None
    return int(round(duration.total_seconds() / 60))


def _iso(value) -> "str | None":
    """aware datetime → 本地时区 ISO 串（前端 `new Date()` 可解析）；`None` 原样。"""
    if value is None:
        return None
    return timezone.localtime(value).isoformat()


class DashboardRangeQuerySerializer(serializers.Serializer):
    """trend / game-distribution 共用的窗口校验口（只吃 `yyyy-MM-dd`；缺参由视图点名）。"""

    start = serializers.DateField(
        input_formats=["%Y-%m-%d"],
        required=False,
        error_messages={"invalid": "start 格式必须是 yyyy-MM-dd"},
    )
    end = serializers.DateField(
        input_formats=["%Y-%m-%d"],
        required=False,
        error_messages={"invalid": "end 格式必须是 yyyy-MM-dd"},
    )


class DashboardRangeWindowView(APIView):
    """带 `[start, end]` 窗口参数的看板视图基类（trend / game-distribution 共用）。"""

    permission_classes = [IsAccountAdmin]

    def _params(self):
        """缺参 / 形状错 / start>end ⇒ `ValidationError` ⇒ 200 + `1000`（照 `console_views`）。"""
        # 空串按「没传」处理（前端清空日期框时会送 `?start=`）。
        raw = {key: value for key, value in self.request.query_params.items() if value != ""}
        params = DashboardRangeQuerySerializer(data=raw)
        params.is_valid(raise_exception=True)
        data = params.validated_data
        missing = [name for name in ("start", "end") if name not in data]
        if missing:
            raise serializers.ValidationError("缺少必填参数：" + "、".join(missing))
        if data["start"] > data["end"]:
            raise serializers.ValidationError("start 不能晚于 end")
        return data


def trend_points(*, start, end):
    """窗口内逐日 `{date, count, amount}`（date 升序）。口径：排除已退单（趋势图标题
    「已排除已退单部分」）；金额 = `Σ total_price`。没有订单的日子不回（补零由前端按窗口补）。
    `.order_by("day")` 压掉 `Meta.ordering`（显式排序列会被追加进 GROUP BY 拆多行）。"""
    rows = (
        Order.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
        .exclude(status=OrderStatus.REFUNDED)
        .annotate(day=TruncDay("created_at"))
        .values("day")
        .annotate(count=Count("id"), amount=Sum("total_price"))
        .order_by("day")
    )
    return [
        {
            "date": timezone.localtime(row["day"]).date().isoformat(),
            "count": row["count"],
            "amount": _money(row["amount"]),
        }
        for row in rows
    ]


class DashboardTrendView(DashboardRangeWindowView):
    """`GET /api/business/dashboard/trend/?start=&end=` → `[{date, count, amount}]`。"""

    def get(self, request):
        data = self._params()
        return api_response(trend_points(start=data["start"], end=data["end"]))


def game_distribution(*, start, end):
    """窗口内按游戏名分组的单量（count 降序；空名归「未分组」）。本端点不排除已退单、
    不排镜像隐藏单（趋势图的排除是它自己的标题口径，别互相「统一」）。"""
    rows = (
        Order.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
        .order_by()
        .values("game__name")
        .annotate(count=Count("id"))
    )
    out = [
        {
            "name": (row["game__name"] or "").strip() or UNGROUPED,
            "count": row["count"],
        }
        for row in rows
    ]
    out.sort(key=lambda item: (-item["count"], item["name"]))
    return out


class DashboardGameDistributionView(DashboardRangeWindowView):
    """`GET /api/business/dashboard/game-distribution/?start=&end=` → `[{name, count}]`。"""

    def get(self, request):
        data = self._params()
        return api_response(game_distribution(start=data["start"], end=data["end"]))


def hall_stay_data():
    """大厅实时快照五键（无样本的键回 `null`）。在厅池 = `WAITING_GRAB` 且无主
    （无主判据与 `console_views` 的接手数量同一形状：不存在有主的 MAIN 绩效行）；
    停留 = 池内 `now − created_at` 的 avg/max。今日被抢速度样本刻意与前端
    「今日被抢走」计数（`order_stats._window_rows`）同口径：排除软删单 / 软删绩效 /
    两路镜像隐藏单，取 `claimed_at` 本地日期为今天的 `claimed_at − order.created_at` min/max。"""
    now = timezone.now()
    main_taker = Performance.objects.filter(
        order=OuterRef("pk"),
        emp_type=EmpType.MAIN,
        user__isnull=False,
        deleted_at__isnull=True,
    )
    pool = Order.objects.filter(status=OrderStatus.WAITING_GRAB).exclude(Exists(main_taker))
    in_hall_count = pool.count()
    stays = pool.annotate(
        stay=ExpressionWrapper(now - F("created_at"), output_field=DurationField()),
    ).aggregate(avg_stay=Avg("stay"), max_stay=Max("stay"))

    grabs = (
        Performance.objects.filter(
            user__isnull=False,
            claimed_at__date=timezone.localdate(),
            deleted_at__isnull=True,
            order__deleted_at__isnull=True,
        )
        .exclude(order__hall_card__isnull=False)
        .exclude(order__upstream_hall_card__isnull=False)
        .annotate(
            dwell=ExpressionWrapper(
                F("claimed_at") - F("order__created_at"), output_field=DurationField()
            ),
        )
        .aggregate(fastest=Min("dwell"), slowest=Max("dwell"))
    )
    return {
        "in_hall_count": in_hall_count,
        "avg_stay_minutes": _minutes(stays["avg_stay"]),
        "max_stay_minutes": _minutes(stays["max_stay"]),
        "today_fastest_grab_minutes": _minutes(grabs["fastest"]),
        "today_slowest_grab_minutes": _minutes(grabs["slowest"]),
    }


class DashboardHallStayView(APIView):
    """`GET /api/business/dashboard/hall-stay/`（不读查询参数）。"""

    permission_classes = [IsAccountAdmin]

    def get(self, request):
        return api_response(hall_stay_data())


def _outbox_channel(model):
    """outbox 型渠道（`JidanpuSyncEvent` / `PeerSyncEvent` 同构）三值一次聚合。"""
    return model.objects.aggregate(
        pending=Count("id", filter=Q(status=model.Status.PENDING)),
        failed=Count("id", filter=Q(status=model.Status.FAILED)),
        last_time=Max("processed_at"),
    )


def sync_health_data():
    """四渠道 `{name, pending, failed, last_time}`。鸡蛋铺 / 下家互推 = 各自 outbox 的
    pending / failed 行数与 `processed_at` 最大值；代练通 = `DailiantongPush`（在途 =
    `is_active=True`，其中 state=unknown 的行**同时**计入 failed——两格语义不同、
    不是同一事实的两份抄写；失败 = state ∈ (unknown, failed)；last = `last_synced_at` max）；
    闲管家 = 三键全 null（见 `XGJ_CHANNEL_NAME`）。"""
    jidanpu = _outbox_channel(JidanpuSyncEvent)
    peer = _outbox_channel(PeerSyncEvent)
    dailiantong = DailiantongPush.objects.aggregate(
        pending=Count("id", filter=Q(is_active=True)),
        failed=Count(
            "id",
            filter=Q(
                state__in=(
                    DailiantongPush.State.UNKNOWN,
                    DailiantongPush.State.FAILED,
                )
            ),
        ),
        last_time=Max("last_synced_at"),
    )
    return {
        "channels": [
            {
                "name": "鸡蛋铺",
                "pending": jidanpu["pending"],
                "failed": jidanpu["failed"],
                "last_time": _iso(jidanpu["last_time"]),
            },
            {
                "name": "下家互推",
                "pending": peer["pending"],
                "failed": peer["failed"],
                "last_time": _iso(peer["last_time"]),
            },
            {
                "name": "代练通",
                "pending": dailiantong["pending"],
                "failed": dailiantong["failed"],
                "last_time": _iso(dailiantong["last_time"]),
            },
            {
                "name": XGJ_CHANNEL_NAME,
                "pending": None,
                "failed": None,
                "last_time": None,
            },
        ]
    }


class DashboardSyncHealthView(APIView):
    """`GET /api/business/dashboard/sync-health/`（不读查询参数）。"""

    permission_classes = [IsAccountAdmin]

    def get(self, request):
        return api_response(sync_health_data())
