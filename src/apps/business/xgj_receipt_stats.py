"""闲管家**收款明细**的读取层（收款线 spec §6；自建读面，对标物里没有这一页）。口径（逐条写明依据）： 1. **行集合 = 本租户 `XgjReceiptRecord` 里 `success_time` 落在窗口内的行**—— 窗口按**本地日**（`Asia/Shanghai`，spec §4-D6：交易成功时间 = 推送 `modify_time`，统计桶按本地日）；`success_time` 为空的行（推送 `modify_time` 认不出的极少数） **不进任何日期窗口**——桶的键缺失就不属于任何一天，明细留在库里可追溯， ⛔ 不把它们偷偷塞进「任意一天」。 2. **汇总 = 按 `shop_identity`（回查详情的 `seller_eid`）分组**（spec §12 F2-D：推送的 `seller_id` 是**商家级** ID、在 `XgjShop.user_identity` 里零命中——27 家店
挤成 2 行的根因 ⇒ 分组键改 `shop_identity`；它是行回查到的事实、之后不变）。 - `shop_identity` **为空**（NULL，尚未回查 / 详情取不到 `seller_eid`）的行单独归 **「未识别店铺」桶**，桶内再按 `seller_id` 分（展示名走旧三层兜底，见口径 3）；补数（`backfill_xgj_receipt_amounts --refetch-all`）完成后该桶自然为空。 - 每组给「店铺名 / 交易成功笔数 / 实收总金额 / 金额缺失笔数」； - **实收总额 = `Sum(amount)` 仅非空行**（spec §4-D7）：全空组按 `Decimal("0.00")` 展示（「计入了 0 元」与「缺失 N 笔」合起来数字能对上）； - **总笔数 = 计入金额笔数 + 缺失笔数**（spec §4-D7 的同一条）； - **排序 = 实收总额降序**（spec §6），同额按组键升序稳定 tie-break
（先例 = `order_stats.py` 的排序口径：同分顺序不稳定 ⇒ 加 tie-break）。 3. **展示名读时现解析**（spec §4-D9 + §12 F2-D）：第一层 = `shop_identity` 命中 `XgjShop.user_identity`（生产探针已证**真命中**，F-2）⇒ 用它**当前**的 `shop_name`；命中不到回退**组内快照**（取最新一行的非空快照）；再不行回退组键原文。未识别桶的行按**旧三层**兜底（`seller_id` 现解析 → 快照 → 原始 `seller_id`）—— `seller_id`→`user_identity` 是无实测的辅助路径（评审 M-6），三层保证「一定有名字」。 4. **金额一律字符串**（两位小数）交给信封（`Decimal` 不交给 JSONEncoder 变 `float`，同 `wallet_admin_views` 的口径）；空金额是 `None`（前端显示「缺失」，不是 0）。 5. **匹配结果的人话**：`match_result` JSON 数组 → 一句中文摘要（唯一出处 = 本模块的 `match_summary_text`，前端不再各拼一份）。 ⚠️ 本模块**只读**、不碰 request；查询只落在**当前连接所在的 schema** 上， ⛔ 不接受任何请求参数传入的租户线索（AGENTS.md §4）。可脱离 HTTP 直接测（先例 = `order_stats.py`）。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.business.models import XgjReceiptRecord, XgjShop
from apps.business.source_groups import source_ids_in_group

#: 全空组的「实收总额」展示值（spec §4-D7：Sum 仅非空行 ⇒ 全空 = 计入了 0 元）。
_ZERO_AMOUNT = Decimal("0.00")

#: `match_result[].result` 的机器值 → 人话短语（唯一出处；⛔ 前端别再拼一份）。
_MATCH_RESULT_LABELS = {
    "updated": "已改已结款",
    "settled_skip": "已是已结款，跳过",
    "refunded_skip": "已退单，跳过",
    "skipped": "跳过",
}


def window_bounds(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    """本地日窗口 → **aware UTC** 的 `[start, end)` 半开区间（统计桶的判据）。 ⚠️ `success_time` 存的是 UTC（`USE_TZ=True`）：`2026-10-21` 这一天 =
    上海时间 `10-21 00:00:00` ～ `10-22 00:00:00`，换算成 UTC 后进 `__gte/__lt`。
    ⛔ 不用 `__date` 后缀查找——那会按**数据库会话时区**截日期，与「按本地日」是两套判据。"""
    tz = timezone.get_current_timezone()
    start = datetime.combine(date_from, time.min).replace(tzinfo=tz)
    end = datetime.combine(date_to + timedelta(days=1), time.min).replace(tzinfo=tz)
    return start, end


def _source_group_subquery(source_group: int):
    """「当前属于该分组的店铺」→ `user_identity` 子查询（唯一：`models.py` 的 `XgjShop.user_identity`）。
    ⚠️ 语义 = 店铺**当前**所属来源的分组（计划 §4 #7 写死）：历史行会随归组变化， ⛔ 不伪造历史归属；**未识别店铺**（`shop_identity` 为空）不属于任何分组 ⇒ 选分组时被排除。
    ⚠️ 用 `Subquery`（而不是 join）与 `is_leak` 那一族同形：join 会放大行、也会让 `total` 与 `summary` 的计数分叉。"""
    return XgjShop.objects.filter(order_source_id__in=source_ids_in_group(source_group)).values("user_identity")


def _window_rows(date_from: date, date_to: date, *, source_group: int | None = None):
    """窗口内的记账行（半开区间；见 `window_bounds` 的口径）。
    `source_group` 给了 ⇒ 只留「店铺当前所属来源属于该分组」的行（见 `_source_group_subquery`）。"""
    start, end = window_bounds(date_from, date_to)
    rows = XgjReceiptRecord.objects.filter(success_time__gte=start, success_time__lt=end)
    if source_group is not None:
        rows = rows.filter(shop_identity__in=Subquery(_source_group_subquery(source_group)))
    return rows


def _shop_names_for(identities: list[str]) -> dict[str, str]:
    """一批组键（`shop_identity` 或未识别行的 `seller_id`）→ 当前店名（**一次**查询，
    不是逐组 N+1）。 ⚠️ 展示名三层兜底的**第一层**；`user_identity` 唯一 ⇒ dict 不撞键。
    空串不查（`user_identity` 不会有空值，查了也是白跑一趟）。"""
    wanted = [key for key in identities if key]
    if not wanted:
        return {}
    return dict(
        XgjShop.objects.filter(user_identity__in=wanted)
        .exclude(shop_name="")
        .values_list("user_identity", "shop_name")
    )


def _display_name(group_key: str, resolved: dict[str, str], fallbacks: list[str]) -> str:
    """三层兜底：现解析 → 组内快照（取第一个非空）→ 组键原文（spec §4-D9 / F2-D）。"""
    name = resolved.get(group_key, "")
    if name:
        return name
    for candidate in fallbacks:
        if candidate:
            return candidate
    return group_key


def _latest_snapshots(key_field: str, keys: list[str]) -> dict[str, str]:
    """按 `key_field`（`shop_identity` 或 `seller_id`）取每组**最新一行**的非空快照
    （三层兜底的**第二层**；`key_field` 只许这两列——本网钉住调用面，别放开成任意列）。 ⚠️ 展示名会变、快照是记账当时的：这里只要求「能说出是哪家店」，组内多行快照不一致时
    取**最新一行**的（`order_by(key_field, "-id")`；映射不要求跨行一致——那是历史事实，不是现行事实）。"""
    wanted = [key for key in keys if key]
    if not wanted:
        return {}
    out: dict[str, str] = {}
    rows = (
        XgjReceiptRecord.objects.filter(**{f"{key_field}__in": wanted})
        .exclude(shop_name_snapshot="")
        .order_by(key_field, "-id")
        .values_list(key_field, "shop_name_snapshot")
    )
    for key, snapshot in rows:
        # 每组取**第一行**（即该组 id 最大的那行）。
        out.setdefault(key, snapshot)
    return out


def summary_rows(date_from: date, date_to: date, *, source_group: int | None = None) -> dict:
    """汇总：`{"items": [行…], "totals": {…}}`。行结构逐键见模块 docstring 的口径 2。 ⚠️ **库侧聚合**（`values("identity_key", "seller_id") + annotate`，单表 group by；
    量级 = 每天几十到几百行，无性能题，spec §6）；归并在 Python 侧做： `shop_identity` 非空的 (identity, seller_id) 子组合并成**一个**店铺行
    （同一家的多笔可能来自不同 `seller_id`——那是商家级 ID，不构成差异），空的子组各自成「未识别店铺」桶里的一行（口径 2）。金额降序也在 Python 侧做（聚合后一次排，tie-break = 组键升序）。"""
    grouped = list(
        _window_rows(date_from, date_to, source_group=source_group)
        # ⚠️ NULL 折成 ""：SQL 的 group by 把 NULL 与 "" 当两组，而两者对口径**等价**
        #    （都是「未识别」）⇒ 库侧先折一边，Python 侧就不必两次判空。
        .annotate(identity_key=Coalesce("shop_identity", Value("")))
        .values("identity_key", "seller_id")
        .annotate(
            total_count=Count("pk"),
            amount_sum=Sum("amount"),
            missing_count=Count("pk", filter=Q(amount__isnull=True)),
        )
    )
    merged: dict[tuple[str, str], dict] = {}
    for row in grouped:
        identity = (row["identity_key"] or "").strip()
        seller_id = row["seller_id"] or ""
        key = (identity, "") if identity else ("", seller_id)
        bucket = merged.setdefault(
            key, {"count": 0, "amount": Decimal("0.00"), "missing_count": 0}
        )
        bucket["count"] += row["total_count"]
        bucket["missing_count"] += row["missing_count"]
        if row["amount_sum"] is not None:
            bucket["amount"] += row["amount_sum"]
    # 第一层现解析：identity 组按 identity、未识别子组按 seller_id——同一次查询（∪ 键集）。
    identity_keys = sorted({k[0] for k in merged if k[0]})
    unknown_sellers = sorted({k[1] for k in merged if not k[0]})
    resolved = _shop_names_for(identity_keys + unknown_sellers)
    identity_snapshots = _latest_snapshots(
        "shop_identity", [k for k in identity_keys if k not in resolved]
    )
    seller_snapshots = _latest_snapshots(
        "seller_id", [s for s in unknown_sellers if s not in resolved]
    )

    def _sort_key(entry) -> tuple:
        (identity, seller_id), bucket = entry
        return (-bucket["amount"], identity, seller_id)

    items = []
    for (identity, seller_id), bucket in sorted(merged.items(), key=_sort_key):
        if identity:
            group_key, fallback = identity, identity_snapshots.get(identity, "")
        else:
            group_key, fallback = seller_id, seller_snapshots.get(seller_id, "")
        items.append(
            {
                # ⚠️ 出信封键 = **shop_identity**（组键原文，F2-D）；未识别子组 = ""，
                #    展示名走旧三层兜底、前端有「未识别店铺」文案兜底。
                "shop_identity": identity,
                "shop_name": _display_name(group_key, resolved, [fallback]),
                "count": bucket["count"],
                # ⚠️ 金额出信封一律字符串（口径 4）。
                "amount": str(bucket["amount"]),
                "missing_count": bucket["missing_count"],
            }
        )
    totals = {
        "shop_count": len(items),
        "count": sum(r["count"] for r in items),
        "amount": str(sum((Decimal(r["amount"]) for r in items), _ZERO_AMOUNT)),
        "missing_count": sum(r["missing_count"] for r in items),
    }
    return {"items": items, "totals": totals}


def detail_rows(date_from: date, date_to: date, *,
                shop_identity: str | None = None,
                source_group: int | None = None) -> list[XgjReceiptRecord]:
    """窗口内的**明细行**（视图拿它去分页；排序 = 交易成功时间倒序 → id 倒序）。 `shop_identity` 的三档（F2-D 改名后的口径）： - `None`（参数没给）⇒ 不筛（整窗明细）；
    - 非空串 ⇒ 按它**精确**筛（页面上「点店铺看单店明细」的那一条路；spec §8.1）； - **空串**（点了「未识别店铺」桶）⇒ 只看 identity 为空的行（SQL 里 NULL 与 ""
    都算——NULL 是「还没回查到」的常态，"" 只是防御）。 ⚠️ 空串与缺席**必须可分辨**（序列化器 ⛔ 不设 `default`），否则点桶会静默变成「整窗明细都算这一家的」，数字对不上。"""
    start, end = window_bounds(date_from, date_to)
    rows = XgjReceiptRecord.objects.filter(success_time__gte=start, success_time__lt=end)
    if source_group is not None:
        rows = rows.filter(shop_identity__in=Subquery(_source_group_subquery(source_group)))
    if shop_identity is None:
        pass
    elif shop_identity == "":
        rows = rows.filter(Q(shop_identity__isnull=True) | Q(shop_identity=""))
    else:
        rows = rows.filter(shop_identity=shop_identity)
    return list(
        rows.select_related("binding").order_by("-success_time", "-id")
    )


def serialize_detail(record: XgjReceiptRecord, resolved: dict[str, str]) -> dict:
    """一行明细的出信封形状（金额字符串 / 空金额 `None`；匹配结果一句人话）。 `seller_id` 照留（**行级事实**，spec §12 F2-D）；`shop_identity` 是分组键回显，
    未识别行为空串。展示名第一层 = identity 命中；identity 为空的行走旧三层（`seller_id` 现解析 → 快照 → 原始 `seller_id`，见 `_display_name` 的组键取法）。"""
    group_key = record.shop_identity or record.seller_id
    fallbacks = [record.shop_name_snapshot]
    return {
        "id": record.pk,
        "order_no": record.order_no,
        "seller_id": record.seller_id,
        "shop_identity": record.shop_identity or "",
        "shop_name": _display_name(group_key, resolved, fallbacks),
        "user_name": record.user_name,
        "amount": "" if record.amount is None else str(record.amount),
        "amount_missing": record.amount is None,
        "success_time": (
            timezone.localtime(record.success_time).isoformat()
            if record.success_time is not None
            else None
        ),
        "matched_count": record.matched_count,
        "match_summary": match_summary_text(record.matched_count, record.match_result),
    }


def serialize_details(rows: list[XgjReceiptRecord]) -> list[dict]:
    """一**页**明细 → 出信封形状（店名现解析**一次**查询，不是逐行 N+1）。"""
    resolved = _shop_names_for(
        [row.shop_identity or row.seller_id for row in rows]
    )
    return [serialize_detail(row, resolved) for row in rows]


def match_summary_text(matched_count: int, match_result: list | object) -> str:
    """`match_result` JSON 数组 → 一句中文摘要（口径 5 的唯一出处）。"""
    if not matched_count:
        return "未匹配到系统订单"
    counts: dict[str, int] = {}
    for entry in match_result if isinstance(match_result, list) else []:
        result = entry.get("result") if isinstance(entry, dict) else None
        if result is not None:
            counts[result] = counts.get(result, 0) + 1
    parts = [
        f"{_MATCH_RESULT_LABELS.get(result, result)} {count} 张"
        for result, count in sorted(counts.items())
    ]
    return f"命中 {matched_count} 张：{'；'.join(parts)}"
