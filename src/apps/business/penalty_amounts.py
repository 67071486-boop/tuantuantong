"""Amount approved for a generated penalty order."""

from apps.business.models import ApprovalKind, ApprovalStatus


PENALTY_AMOUNT_LOCKED = "罚款金额已由审批确定，不能修改。"


def approved_penalty_price(order):
    if order.penalty_approval_id is None:
        return None
    approval = order.penalty_approval
    if (
        approval.kind == ApprovalKind.PENALTY
        and approval.status == ApprovalStatus.APPROVED
        and approval.amount is not None
    ):
        return -approval.amount
    return None


def changes_approved_penalty_price(order, price):
    approved = approved_penalty_price(order)
    return approved is not None and price != approved
