"""Persist successful settlements inside the caller's transaction."""
from apps.business.models import (
    Performance,
    PerformanceAdjustment,
    PerformanceSettleRequest,
    SettleStatus,
)
from apps.core.error_codes import ErrorCode


class SettleError(Exception):
    """Expected manual settlement failure, translated to a business error by the caller."""

    def __init__(self, message, code):
        super().__init__(message)
        self.message = message
        self.code = code


def persist_manual_settlement(
    rows, adjustments, *, settled_rows, expected_adjustments, amounts_by_id, actor, settled_at,
):
    # Keep the original query predicates so the database rechecks eligibility at write time.
    expected_rows = len(settled_rows)
    updated_rows = rows.update(
        settle_status=SettleStatus.SETTLED, settled_at=settled_at, settled_by=actor,
    )
    updated_adjustments = adjustments.update(
        settle_status=SettleStatus.SETTLED, settled_at=settled_at,
    )
    if (updated_rows, updated_adjustments) != (expected_rows, expected_adjustments):
        raise SettleError(
            f"本批在你读取之后已被改动（绩效 {updated_rows}/{expected_rows}，"
            f"调整记录 {updated_adjustments}/{expected_adjustments}），请刷新后重试",
            ErrorCode.SETTLE_STATE_INVALID,
        )

    # The guarded updates must succeed before writing amounts by primary key.
    for row in settled_rows:
        row.settled_amount = amounts_by_id[row.pk]
    Performance.objects.bulk_update(settled_rows, ["settled_amount"])
    _settle_pending_requests([row.pk for row in settled_rows], settled_at)


def persist_online_settlement(rows, adjustments, items, *, actor, settled_at):
    """Use locked rows, locked adjustments, and items ordered by performance ID."""
    for row, item in zip(rows, items, strict=True):
        row.settle_status = SettleStatus.SETTLED
        row.settled_at = settled_at
        row.settled_by = actor
        row.settled_amount = item.amount
        row.settlement_method = "online"
    Performance.objects.bulk_update(
        rows, ["settle_status", "settled_at", "settled_by", "settled_amount", "settlement_method"],
    )
    for adjustment in adjustments:
        adjustment.settle_status = SettleStatus.SETTLED
        adjustment.settled_at = settled_at
    PerformanceAdjustment.objects.bulk_update(adjustments, ["settle_status", "settled_at"])
    _settle_pending_requests([row.pk for row in rows], settled_at, lock=True)


def _settle_pending_requests(performance_ids, settled_at, *, lock=False):
    requests = PerformanceSettleRequest.objects.filter(
        performance_id__in=performance_ids, state=PerformanceSettleRequest.State.PENDING,
    )
    settled = PerformanceSettleRequest.State.SETTLED
    if lock:
        requests = list(requests.select_for_update())
        for request in requests:
            request.state = settled
            request.settled_at = settled_at
        PerformanceSettleRequest.objects.bulk_update(requests, ["state", "settled_at"])
    else:
        requests.update(state=settled, settled_at=settled_at)
