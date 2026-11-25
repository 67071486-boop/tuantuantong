"""登录式上家指派单固定窗口补拉。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from django.utils import timezone
from jidanpu.errors import ApiError

from apps.business import jidanpu_upstream as upstream


RECENT_PAGE_BUDGET = 8
BACKFILL_PAGE_BUDGET = 16


def _seconds(value):
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return int(value.timestamp())


def _stamp(seconds):
    return timezone.localtime(datetime.fromtimestamp(seconds, UTC)).strftime("%Y-%m-%d %H:%M:%S")


def _window(start, end):
    return {"start": start, "end": end}


@dataclass
class _Budget:
    remaining: int

    def fetch(self, client, page, query):
        self.remaining -= 1
        return upstream._fetch_page(client, upstream.MY_ORDERS_PATH, page=page, query=query)


@dataclass
class _ViewRead:
    rows: list
    cursor: dict
    complete: bool
    digest: str = ""
    seen: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class PullBatchReport:
    complete: bool = False
    observed_since: datetime | None = None
    read: bool = False
    error: ApiError | None = None


class _IncompleteView(ApiError):
    pass


def _page_key(order_ids):
    return sha256(json.dumps(sorted(order_ids)).encode()).hexdigest()


def _row_ids(rows):
    return [str(row.get("orderId") or "") for row in rows]


def _pull_view(client, query, budget, *, cursor=None, splittable=False):
    rows = []
    cursor = cursor or {}
    page = cursor.get("page", 1)
    pulled = cursor.get("pulled", 0)
    previous_key = cursor.get("previous_key")
    previous_total = cursor.get("previous_total")
    largest_total = cursor.get("largest_total", 0)
    digest = cursor.get("digest", "")
    seen = set(cursor.get("seen", ()))
    if page > 1 and budget.remaining:
        previous, current_total = budget.fetch(client, page - 1, query)
        if _page_key(_row_ids(previous)) != previous_key or current_total != previous_total:
            # 上家可能撤走或重新指派了窗口内的订单，旧页码不能继续使用。
            page, pulled, previous_key = 1, 0, None
            previous_total, largest_total, digest = None, 0, ""
            seen.clear()
    while budget.remaining:
        batch, total = budget.fetch(client, page, query)
        if splittable and page == 1 and batch and total is not None and total > len(batch) * (budget.remaining + 1):
            return _ViewRead([], {}, False)
        order_ids = _row_ids(batch)
        usable_ids = [order_id for order_id in order_ids if order_id]
        key = _page_key(order_ids)
        unique_ids = set(usable_ids)
        if batch and (key == previous_key or seen & unique_ids or len(unique_ids) < len(usable_ids)):
            if splittable:
                return _ViewRead([], {}, False)
            raise _IncompleteView("指派订单分页返回重复单号，暂不能确认分页完整")
        seen.update(unique_ids)
        rows.extend(batch)
        pulled += len(batch)
        largest_total = max(largest_total, total or 0)
        if not batch:
            if pulled >= largest_total:
                return _ViewRead(rows, {}, True, digest, seen)
            if splittable:
                return _ViewRead([], {}, False)
            raise _IncompleteView(f"指派订单分页未读全：共 {largest_total} 条，读到 {pulled} 条时遇到空页")
        digest = sha256((digest + key).encode()).hexdigest()
        # count 可能偏小，每个非空尾页都继续探到空页。
        page += 1
        previous_key = key
        previous_total = total
    return _ViewRead(rows, {
        "page": page, "pulled": pulled, "previous_key": previous_key, "previous_total": previous_total,
        "largest_total": largest_total, "digest": digest, "seen": sorted(seen),
    }, False, digest, seen)


def _merge_rows(rows, refunds, seen):
    for row in refunds:
        remote_id = str(row.get("orderId") or "")
        if remote_id and remote_id not in seen:
            rows.append(row)
            seen.add(remote_id)
    return rows


def _read_window(client, window, budget):
    start, end = window["start"], window["end"]
    query = {"stime": _stamp(start), "etime": _stamp(end)}
    refund_query = {**query, "orderSta": upstream.MY_ORDERS_REFUND_STATUS}
    if end - start >= 3:
        default = _pull_view(client, query, budget, splittable=True)
        if default.complete:
            refunds = _pull_view(client, refund_query, budget, splittable=True)
            if refunds.complete:
                return _merge_rows(default.rows, refunds.rows, default.seen), []
        # 两秒重叠，让分界秒在两段中至少一边不碰筛选端点。
        middle = (start + end) // 2
        return [], [_window(middle, end), _window(start, middle + 1)]

    # 极密时间段继续按页补拉。记住默认视图的单号，保持退单视图只补充遗漏行。
    cursor = window.get("cursor")
    phase = window.get("phase", "default")
    verify = window.get("verify")
    seen = set(window.get("seen", ()))
    rows = []
    if phase == "default":
        default = _pull_view(client, query, budget, cursor=cursor)
        rows = default.rows
        if not default.complete:
            return rows, [{**window, "cursor": default.cursor}]
        if _verify_again(cursor, verify, default.digest):
            return rows, [{**_window(start, end), "verify": default.digest}]
        seen = default.seen
        cursor = verify = None
    refunds = _pull_view(client, refund_query, budget, cursor=cursor)
    rows = _merge_rows(rows, refunds.rows, seen.copy())
    refund_window = {**_window(start, end), "phase": "refund", "seen": sorted(seen)}
    if not refunds.complete:
        return rows, [{**refund_window, "cursor": refunds.cursor, "verify": verify}]
    if _verify_again(cursor, verify, refunds.digest):
        return rows, [{**refund_window, "verify": refunds.digest}]
    return rows, []


def _verify_again(cursor, previous_digest, digest):
    # 固定窗口的前部仍可能换单。同一遍历结果再现后才能把跨轮窗口收尾。
    return bool(cursor or previous_digest is not None) and previous_digest != digest


class _Checkpoint:
    def __init__(self, binding, saved):
        self.binding = binding
        self.saved = saved

    def save(self, progress):
        current = self.binding.current_identity_rows().filter(enabled=True)
        if self.saved is None:
            current = current.filter(my_orders_progress__isnull=True)
        else:
            current = current.filter(my_orders_progress=self.saved)
        if not current.update(my_orders_progress=progress):
            return False
        self.saved = deepcopy(progress)
        return True


def _drain(client, progress, lane, budget, checkpoint, apply_rows):
    failed = []
    failure = None
    while len(progress[lane]) > len(failed) and budget.remaining:
        window = progress[lane][0]
        try:
            rows, outstanding = _read_window(client, window, budget)
        except ApiError as exc:
            if (
                exc.status_code not in (None, 200, 400, 422)
                or str(exc.code) in ("401", "403", "429", "468")
                or (exc.status_code == 200 and exc.code is None)
                or "限流" in str(exc)
            ):
                raise
            if isinstance(exc, _IncompleteView):
                window = {key: value for key, value in window.items() if key not in ("cursor", "verify")}
            failure = failure or exc
            failed.append(window)
            progress[lane] = [*progress[lane][1:], window]
            if not checkpoint.save(progress):
                return False, failure
            continue
        if rows:
            apply_rows(rows)
        progress[lane] = [*outstanding, *progress[lane][1:]]
        if not checkpoint.save(progress):
            return False, failure
    return True, failure


def pull_batches(*, binding, client, now, apply_rows) -> PullBatchReport:
    """推进新单与积压窗口，保留本轮失败以便正向核单后再报告。"""
    current = binding.current_identity_rows().filter(enabled=True).values("my_orders_progress").first()
    if current is None:
        return PullBatchReport()
    saved = current["my_orders_progress"]
    check = _Checkpoint(binding, saved)
    now_seconds = _seconds(now)
    identity = sha256(json.dumps([
        binding.jidanpu_base_url, binding.account, binding.mode, binding.identity_revision,
    ]).encode()).hexdigest()
    is_new = saved is None or saved["identity"] != identity
    if is_new:
        progress = {
            "identity": identity,
            "started": now.isoformat(),
            "through": now_seconds,
            "recent": [],
            "backfill": [_window(_seconds(now - timedelta(hours=upstream.MY_ORDERS_WINDOW_HOURS)), now_seconds)],
        }
    else:
        progress = deepcopy(saved)
        previous = progress["through"]
        if now_seconds > previous:
            progress["recent"].insert(0, _window(previous - 1, now_seconds))
            progress["through"] = now_seconds
    if not check.save(progress):
        return PullBatchReport()

    recent = _Budget(RECENT_PAGE_BUDGET)
    backfill = _Budget(BACKFILL_PAGE_BUDGET)
    synced, recent_error = _drain(client, progress, "recent", recent, check, apply_rows)
    if not synced:
        return PullBatchReport()
    if progress["recent"] and recent_error is None:
        progress["backfill"].extend(progress["recent"])
        progress["recent"] = []
        if not check.save(progress):
            return PullBatchReport()
    synced, backfill_error = _drain(client, progress, "backfill", backfill, check, apply_rows)
    if not synced:
        return PullBatchReport()
    read = recent.remaining < RECENT_PAGE_BUDGET or backfill.remaining < BACKFILL_PAGE_BUDGET
    error = recent_error or backfill_error
    if error is not None:
        return PullBatchReport(read=read, error=error)
    if progress["backfill"]:
        return PullBatchReport(read=read)
    if not check.save(None):
        return PullBatchReport()
    observed_since = None if is_new else datetime.fromisoformat(progress["started"])
    return PullBatchReport(complete=True, observed_since=observed_since, read=read)
