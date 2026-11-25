"""关联订单只从当前页的可见行展开，沿用独立展开请求的筛选与脱敏。"""

from collections import defaultdict

from apps.business.api.serializers import OrderPenaltyListSerializer
from apps.business.selectors import penalty_children_for_parents, penalty_order_rows


MAX_RELATED_ORDERS = 300
MAX_PENALTY_LEVELS = 8


def order_page_relations(items, *, exact_matches, related_scope, visible_scope, include_unmatched, context):
    """超过批量上限时省略关联数据，由客户端沿用独立展开请求。"""
    remaining = MAX_RELATED_ORDERS
    split_ids = {row["split_id"] for row in items if row["split_id"] and row["split_members"] > 1}
    split_members = defaultdict(list)
    if split_ids:
        split_scope = visible_scope if include_unmatched else exact_matches
        rows = penalty_order_rows(
            split_scope.filter(split_id__in=split_ids), related_scope, visible_scope,
            exact_split_matches=exact_matches,
        )
        members = list(rows[:remaining + 1])
        if len(members) > remaining:
            return None
        remaining -= len(members)
        for row in OrderPenaltyListSerializer(members, many=True, context=context.copy()).data:
            split_members[row["split_id"]].append(row)

    primary_rows = []
    for row in items:
        members = split_members.get(row["split_id"], ())
        primary_rows.extend(members if len(members) > 1 else (row,))

    primary_numbers = {row["order_no"] for row in primary_rows}
    pending = {row["order_no"] for row in primary_rows if row["penalty_child_count"]}
    seen = set()
    penalty_children = {}
    for _ in range(MAX_PENALTY_LEVELS):
        if not pending:
            break
        seen.update(pending)
        children = {number: [] for number in pending}
        related_rows = penalty_children_for_parents(related_scope, visible_scope, pending)
        rows = list(related_rows[:remaining + 1])
        if len(rows) > remaining:
            return None
        remaining -= len(rows)
        pending = set()
        for row in OrderPenaltyListSerializer(rows, many=True, context=context.copy()).data:
            children[row["penalty_parent_order_no"]].append(row)
            number = row["order_no"]
            if row["penalty_child_count"] and not row["split_id"] and number not in primary_numbers and number not in seen:
                pending.add(number)
        penalty_children.update(children)

    if pending:
        return None
    return {"split_members_by_id": split_members, "penalty_children_by_parent": penalty_children}
