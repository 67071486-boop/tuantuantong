"""「APP 与小程序 → 数据概览」的只读聚合层（`GET /api/business/app-stats/overview/`）。计划：`docs/plans/2026-10-23-app-mp-stats.md` §7（业务方口径）与 §8（评审改准的工程做法）。 ## 四块数据与口径 | 块 | 来源 | 口径 | |---|---|---| | 小程序访问 | 微信 `datacube/getweanalysisappiddailyvisittrend` | **只有 `suqing`**；T+1（止于昨天）；失败 ⇒ `available=false` |
| 注册 | `StaffProfile.account_source='app'` | 按日；按「该账号**最早一枚** AppToken 的 `client`」拆 `mp-weixin` / `app-android` / 其它 | | 每日接单 | `OperationLog(action='order.claim')` | 按日、按 `reason`（渠道）分；「移动端」= `claim_channel.MOBILE_CHANNELS` | | 押金 | `TakerWallet.frozen` + `WalletTransaction` | 当前总额 + 冻结人数 + 每日变动；账面值**含到期未结算** |
## 三条容易写错的地方（都有用例钉着） 1. **两个窗口不是同一个**：微信 T+1 ⇒ 它的 7 天止于**昨天**；注册 / 接单 / 押金是自家数据 ⇒ 窗口含**今天**（否则今天注册的账号在测试与使用中都看不到）。响应里两个区间分别给。 2. **失败不冒充 0**：微信块失败时 `series=[]`、`totals=None`、`available=False`，⛔ 不补 0。 3. **日期按本地时区切**：`created_at` 是 `USE_TZ=True` 的 aware datetime，用 `timezone.localtime(...).date()` 分天，⛔ 不用 UTC 的 `.date()`（跨零点会错一天）。 ## 租户边界租户上下文只来自请求/连接（`connection.schema_name` 与租户库本身），本模块**不接受** 任何请求参数形式的租户线索（`AGENTS.md` §4）。微信块的开通判据是**schema 名**等于 `suqing`。"""
from datetime import date, timedelta
from decimal import Decimal

from django.db import connection
from django.db.models import OuterRef, Subquery, Sum
from django.utils import timezone

from apps.accounts.choices import AccountSource
from apps.accounts.models import AppToken, StaffProfile, TakerWallet, WalletTransaction
from apps.audit.models import OperationLog
from apps.business.claim_channel import DOWNSTREAM_CLAIM_FIELD, MOBILE_CHANNELS
from apps.business.wechat_datacube import WechatStatsError, fetch_daily_visits

#: 开通小程序数据的租户（业务方：小程序数据只给这一个租户看）。
WECHAT_TENANT_SCHEMA = "suqing"

#: 其它租户那一块显示的文案（业务方指定原话，⛔ 不改写）。
WECHAT_NOT_ENABLED_REASON = "本租户未开通小程序"

#: 注册来源三个桶（`client` 的映射；认不出的值一律进 `unknown`，⛔ 不丢行）。
CLIENT_BUCKETS = {
    "mp-weixin": "mp_weixin",
    "app-android": "app_android",
}
REGISTRATION_BUCKETS = ("mp_weixin", "app_android", "unknown")

#: 旧数据提示（计划 §8：上线前注册页不上报 `client`，历史全是 `app-android`）。
REGISTRATION_LEGACY_NOTE = (
    "历史未区分：本次上线前注册的小程序账号会记为 App（当时注册页还没有上报客户端）"
)

#: 押金的如实口径（计划 §7 第 3 条：解冻是懒结算，到期的钱在账面上仍冻着）。
DEPOSIT_NOTE = "账面冻结额（含到期未结算）"

#: 接单里 `reason` 为空的历史行（本字段是 `blank=True`）。
UNRECORDED_REASON = "未记录"

#: 会改变 `TakerWallet.frozen` 的流水 kind → 对冻结额的符号。
#: ⚠️ `release_request`（申请解冻）**一分钱都不动** ⇒ 刻意不在表里（见 `WalletTransaction`）。
FROZEN_FLOW_SIGNS = {
    WalletTransaction.KIND_FREEZE: Decimal("1"),
    WalletTransaction.KIND_RELEASE: Decimal("-1"),
    WalletTransaction.KIND_FROZEN_ADD: Decimal("1"),
    WalletTransaction.KIND_FROZEN_SUB: Decimal("-1"),
}

_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")


def _money(value) -> str:
    """金额出口：两位小数字符串（`None` / 0 / 负数都照原样，⛔ 不用真值判断）。"""
    if value is None:
        return "0.00"
    return f"{Decimal(value).quantize(_CENT):.2f}"


def _local_day(moment) -> date:
    """aware datetime → 本地日期（`USE_TZ=True`；⛔ 不用 UTC 的 `.date()`）。"""
    if timezone.is_aware(moment):
        return timezone.localtime(moment).date()
    return moment.date()


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _wechat_block(days: int, today: date) -> dict:
    """微信块（T+1：窗口止于昨天）。未开通 / 失败都回 `available=False` + 原因。"""
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    block = {
        "available": False,
        "reason": "",
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "series": [],
        "totals": None,
    }
    if connection.schema_name != WECHAT_TENANT_SCHEMA:
        block["reason"] = WECHAT_NOT_ENABLED_REASON
        return block

    try:
        series = fetch_daily_visits(days=days, today=today)
    except WechatStatsError as exc:
        # 预期失败 ⇒ 如实告诉页面（⛔ 不 500、⛔ 不补 0）。
        block["reason"] = str(exc)
        return block

    block["available"] = True
    block["series"] = series
    block["totals"] = {
        key: sum(row[key] for row in series)
        for key in ("session_cnt", "visit_pv", "visit_uv", "visit_uv_new")
    }
    return block


def _registration_block(start: date, end: date) -> dict:
    """打手 App 注册（`account_source='app'`），按最早一枚 AppToken 的 client 分桶。"""
    first_client = (
        AppToken.objects.filter(user_id=OuterRef("user_id"))
        .order_by("created_at", "id")
        .values("client")[:1]
    )
    rows = (
        StaffProfile.objects.filter(
            account_source=AccountSource.APP,
            created_at__date__gte=start,
            created_at__date__lte=end,
        )
        .annotate(first_client=Subquery(first_client))
        .values("created_at", "first_client")
    )

    series = {
        day.isoformat(): {"date": day.isoformat(), **{key: 0 for key in REGISTRATION_BUCKETS}, "total": 0}
        for day in _days(start, end)
    }
    totals = {**{key: 0 for key in REGISTRATION_BUCKETS}, "total": 0}
    for row in rows:
        bucket = CLIENT_BUCKETS.get(row["first_client"] or "", "unknown")
        entry = series[_local_day(row["created_at"]).isoformat()]
        entry[bucket] += 1
        entry["total"] += 1
        totals[bucket] += 1
        totals["total"] += 1

    return {
        "series": list(series.values()),
        "totals": totals,
        "note": REGISTRATION_LEGACY_NOTE,
    }


def _claim_block(start: date, end: date) -> dict:
    """每日接单：`OperationLog(action='order.claim')` 按日、按渠道（`reason`）分。"""
    rows = (
        OperationLog.objects.filter(
            action="order.claim",
            created_at__date__gte=start,
            created_at__date__lte=end,
        )
        .exclude(field_name=DOWNSTREAM_CLAIM_FIELD)
        .values("created_at", "reason")
    )

    series = {
        day.isoformat(): {
            "date": day.isoformat(),
            "total": 0,
            "mobile": 0,
            "by_reason": {},
        }
        for day in _days(start, end)
    }
    totals = {"total": 0, "mobile": 0, "by_reason": {}}
    for row in rows:
        reason = (row["reason"] or "").strip() or UNRECORDED_REASON
        entry = series[_local_day(row["created_at"]).isoformat()]
        entry["total"] += 1
        entry["by_reason"][reason] = entry["by_reason"].get(reason, 0) + 1
        if reason in MOBILE_CHANNELS:
            entry["mobile"] += 1
        totals["total"] += 1
        totals["by_reason"][reason] = totals["by_reason"].get(reason, 0) + 1
        if reason in MOBILE_CHANNELS:
            totals["mobile"] += 1

    return {
        "series": list(series.values()),
        "totals": totals,
        "mobile_channels": sorted(MOBILE_CHANNELS),
    }


def _deposit_block(start: date, end: date) -> dict:
    """押金：当前冻结总额 + 冻结人数 + 每日变动（只读，⛔ 不触发懒结算）。"""
    aggregate = TakerWallet.objects.aggregate(total=Sum("frozen"))
    rows = (
        WalletTransaction.objects.filter(
            kind__in=list(FROZEN_FLOW_SIGNS),
            created_at__date__gte=start,
            created_at__date__lte=end,
        )
        .values("created_at", "kind", "amount")
    )

    series = {
        day.isoformat(): {"date": day.isoformat(), "delta": _ZERO}
        for day in _days(start, end)
    }
    for row in rows:
        entry = series[_local_day(row["created_at"]).isoformat()]
        entry["delta"] += FROZEN_FLOW_SIGNS[row["kind"]] * Decimal(row["amount"])

    return {
        "total_frozen": _money(aggregate["total"]),
        "taker_count": TakerWallet.objects.filter(frozen__gt=0).count(),
        "series": [
            {"date": entry["date"], "delta": _money(entry["delta"])}
            for entry in series.values()
        ],
        "note": DEPOSIT_NOTE,
    }


def app_stats_overview(*, days: int, today: date | None = None) -> dict:
    """一屏四块的数据。`days` 由视图的序列化器保证在 1..30。"""
    today = today or timezone.localdate()
    start = today - timedelta(days=days - 1)
    return {
        "days": days,
        "date_from": start.isoformat(),
        "date_to": today.isoformat(),
        "wechat": _wechat_block(days, today),
        "registrations": _registration_block(start, today),
        "claims": _claim_block(start, today),
        "deposit": _deposit_block(start, today),
    }
