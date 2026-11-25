"""Tenant approval routing and order creation."""

import logging
from uuid import uuid4

from django.db import connection, transaction
from django.db.models import Case, F, Q, Value, When
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.choices import AccountSource, MemberType
from apps.accounts.display import user_display_name
from apps.accounts.selectors import list_accounts
from apps.accounts.wxpusher_notifications import enqueue_staff_notice
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.audit.models import OperationLog
from apps.business.api.serializers import CreateOrderAssignSerializer, OrderSerializer
from apps.business.game_scope import assert_game_allowed, game_scope
from apps.business.models import (
    Approval,
    ApprovalKind,
    ApprovalSettings,
    ApprovalStatus,
    OrderType,
    PenaltyTargetType,
)
from apps.business.order_intake import IntakeActor, create_order
from apps.business.roles import WORKER
from apps.business.order_no import generate_order_no
from apps.business.order_writes import WriteContext, clip_sales_agent
from apps.business.selectors import search_orders
from apps.business.validation import ValidationProblem, validate_order_chain, validate_required
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset
from apps.internal_chat import system_notifications
from apps.internal_chat.matrix_bridge import ChatServiceUnavailable
from apps.internal_chat.system_notice_settings import system_notice_enabled
from apps.rbac.permissions import has_node_now


logger = logging.getLogger(__name__)

APPROVAL_APPLY = "home.approval_apply"
APPROVAL_CONFIGURE = "home.approval_configure"
SYSTEM_ACTOR = "系统"
ORDER_DETAIL_NODES = frozenset({"order.view_detail", "order.view_content"})
LINKED_PENALTY_AMOUNTS = {"total_price": "0.00", "received_price": "0.00", "fee": "0.00"}
PENALTY_SUBMIT_ACTION = "order.penalty_submit"
PENALTY_APPROVE_ACTION = "order.penalty_approve"


def active_accounts():
    return list_accounts(is_active=True).exclude(staff_profile__account_source=AccountSource.APP)


def penalty_targets(*, key=None, game_id=None):
    studio = Q(staff_profile__member_type=MemberType.PARTNER, staff_profile__is_studio=True)
    return list_accounts(is_active=True, key=key, game=game_id).filter(
        Q(staff_profile__member_type=MemberType.PARTNER) | Q(groups__name=WORKER)
    ).annotate(penalty_target_type=Case(
        When(studio, then=Value(PenaltyTargetType.STUDIO)),
        default=Value(PenaltyTargetType.WORKER),
    ))


def visible_original_order(user, number):
    if not has_node_now(user, *ORDER_DETAIL_NODES):
        raise PermissionDenied("没有查看关联订单的权限")
    order = scope_queryset(user, search_orders(), scope_filter=game_scope("game_id")).filter(
        order_no=number,
    ).first()
    if order is None:
        raise ValidationError("关联订单不存在或不可见")
    return order


def settings_for_update():
    config, _ = ApprovalSettings.objects.select_for_update().get_or_create(pk=1)
    return config


def is_approval_account(user):
    profile = getattr(user, "staff_profile", None)
    return user.is_active and (
        profile is None or (profile.deleted_at is None and profile.account_source != AccountSource.APP)
    )


def _pending_refunds():
    return Approval.objects.filter(kind=ApprovalKind.REFUND, status=ApprovalStatus.PENDING)


def _notify_approval_in_chat(approval, *, recipient, stage, instruction):
    if not system_notice_enabled({"submitted": "approval_submitted", "approved": "approval_approved"}[stage]):
        return
    title = "罚款审批" if approval.kind == ApprovalKind.PENALTY else "退款处理"
    try:
        system_notifications.send_system_notification(
            recipient, text=f"{title} #{approval.pk} {instruction}", client_id=f"approval:{approval.pk}:{stage}",
        )
    except (ChatServiceUnavailable, PermissionDenied) as exc:
        logger.warning(
            "Approval chat notice unavailable tenant=%s approval=%s stage=%s reason=%s",
            connection.schema_name, approval.pk, stage, type(exc).__name__,
        )


def notify_assigned_approval(approval, *, reassigned=False):
    if approval.approver_id is None:
        return
    kind = "refund_assigned" if approval.kind == ApprovalKind.REFUND else "approval_assigned"
    original = approval.original_order
    details = {"approval_id": approval.pk}
    if original is not None:
        details["display_order_no"] = approval.refund_source_no.strip() or original.source_order_no.strip()
    reference = f"approval:{approval.pk}"
    if reassigned:
        reference = f"{reference}:{uuid4().hex}"
    enqueue_staff_notice(
        user_id=approval.approver_id, kind=kind, reference=reference,
        order_no=original.order_no if original is not None else None,
        details=details,
    )
    if approval.initiator_id is not None and not reassigned:
        _notify_approval_in_chat(
            approval, recipient=approval.approver, stage="submitted", instruction="待你处理，请前往审批中心。",
        )


def route_pending_refunds(handler_id, *, replace):
    if not replace and handler_id is None:
        return
    pending = _pending_refunds()
    if handler_id is None:
        pending.update(approver_id=None)
        return
    if replace:
        pending = pending.exclude(approver_id=handler_id)
    else:
        pending = pending.exclude(approver_id__in=active_accounts().values("pk"))
    rerouted = list(pending.select_for_update(of=("self",)).select_related("original_order").order_by("pk"))
    Approval.objects.filter(pk__in=[approval.pk for approval in rerouted]).update(approver_id=handler_id)
    for approval in rerouted:
        approval.approver_id = handler_id
        notify_assigned_approval(approval, reassigned=True)


def can_decide(approval, user):
    return (
        approval.status == ApprovalStatus.PENDING
        and approval.approver_id == user.pk
        and is_approval_account(user)
    )


def _refund_approval_key(order):
    return {
        "kind": ApprovalKind.REFUND,
        "original_order": order,
        "refund_source_no": order.source_order_no.strip(),
    }


def has_refund_approval(order):
    return Approval.objects.filter(**_refund_approval_key(order)).exists()


@transaction.atomic
def create_refund_approval(order, *, reason):
    config = settings_for_update()
    handler_id = active_accounts().filter(pk=config.refund_handler_id).values_list("pk", flat=True).first()
    approval, created = Approval.objects.get_or_create(
        **_refund_approval_key(order), defaults={"reason": reason, "approver_id": handler_id},
    )
    if created:
        notify_assigned_approval(approval)
    return approval


def prepare_penalty_order_data(order_input, original, user):
    if original is None:
        if order_input.get("order_source") is None:
            raise ValidationError("请选择订单来源")
        order_input = {
            "order_type": OrderType.BOOST, "total_price": "0.00", **order_input,
        }
    else:
        order_input = {
            "game": original.game_id,
            "server": original.server_id,
            "category": original.category_id,
            "project": original.project_id,
            "order_source": original.order_source_id,
            "source": original.source,
            "order_type": original.order_type,
            "project_text": order_input["project_text"],
            "content": original.content,
            "demand": original.demand,
            "time_requirement": original.time_requirement,
            **LINKED_PENALTY_AMOUNTS,
        }
    serializer = OrderSerializer(data=order_input)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    project = data.get("project")
    try:
        validate_required(game=data.get("game"), project_text=data.get("project_text"))
        validate_order_chain(
            game=data.get("game"),
            server=data.get("server"),
            category=data.get("category") or (project.category if project else None),
            project=project,
        )
        assert_game_allowed(user, data.get("game"))
    except ValidationProblem as exc:
        raise ValidationError(str(exc)) from exc
    return order_input


def _create_penalty_order(approval):
    initiator = approval.initiator
    if not penalty_targets().filter(
        pk=approval.target_id, penalty_target_type=approval.target_type,
    ).exists():
        raise ValidationError("罚款对象已停用或身份已改变")
    order_data = approval.order_data | {
        "hall_price": -approval.amount,
        "source_order_no": f"罚-{generate_order_no()}",
        "remark": approval.remark,
    }
    if approval.original_order_id is not None:
        visible_original_order(initiator, approval.original_order.order_no)
        order_data.update(LINKED_PENALTY_AMOUNTS)
    serializer = OrderSerializer(data=order_data)
    serializer.is_valid(raise_exception=True)
    assign_input = CreateOrderAssignSerializer(data={})
    assign_input.is_valid(raise_exception=True)
    actor = IntakeActor(
        user=initiator, log_actor=SYSTEM_ACTOR, log_actor_id=None,
        nodes=frozenset(), write_ctx=WriteContext(),
        sales_agent_fallback=clip_sales_agent(user_display_name(initiator)),
        assert_game=lambda game: assert_game_allowed(initiator, game),
        created_by=initiator.get_username(),
    )
    outcome = create_order(
        serializer=serializer, assign_input=assign_input, actor=actor,
        payload_for_order=lambda order: {"order_no": order.order_no},
    )
    result = outcome.response.data
    if result["code"] != ErrorCode.OK:
        raise ValidationError((result["data"] or {}).get("reason", result["msg"]))

    order = outcome.order
    order.penalty_approval = approval
    order.save(update_fields=["penalty_approval"])
    return order


def _penalty_approval_events(approval, approver):
    return (
        (PENALTY_SUBMIT_ACTION, approval.initiator, approval.created_at),
        (PENALTY_APPROVE_ACTION, approver, approval.decided_at),
    )


def _record_penalty_approval_logs(order, approval, approver):
    for action, actor, created_at in _penalty_approval_events(approval, approver):
        record(
            log_type=ACTION_LOG_TYPES[action], action=action,
            actor=actor.get_username(), actor_id=actor.pk,
            target_kind="order", target_id=order.order_no, order_no=order.order_no,
            source_order_no=order.source_order_no, created_at=created_at,
        )


def missing_penalty_approval_logs(order, rows):
    """从原审批补齐已清理的订单日志，只用于详情展示。"""
    if order.penalty_approval_id is None:
        return []
    missing = {PENALTY_SUBMIT_ACTION, PENALTY_APPROVE_ACTION} - {row.action for row in rows}
    if not missing:
        return []
    approval = Approval.objects.filter(
        pk=order.penalty_approval_id, kind=ApprovalKind.PENALTY, status=ApprovalStatus.APPROVED,
        initiator__isnull=False, approver__isnull=False, decided_at__gte=F("created_at"),
    ).select_related("initiator", "approver").only(
        "created_at", "decided_at", "initiator__username", "initiator__first_name",
        "approver__username", "approver__first_name",
    ).first()
    if approval is None:
        return []
    # 每张审批预留两个负数键，避免与实存日志主键冲突。
    return [
        OperationLog(
            id=position - 2 * approval.pk, log_type=ACTION_LOG_TYPES[action], action=action,
            actor=actor.get_username(), actor_id=actor.pk, actor_name=user_display_name(actor),
            order_no=order.order_no, source_order_no=order.source_order_no, created_at=created_at,
        )
        for position, (action, actor, created_at) in enumerate(_penalty_approval_events(approval, approval.approver))
        if action in missing
    ]


@transaction.atomic
def decide_approval(pk, *, user, decision, comment):
    approval = Approval.objects.select_for_update().filter(pk=pk, approver=user).first()
    if approval is None or not active_accounts().filter(pk=user.pk).exists():
        raise PermissionDenied("这条审批未分配给你")
    if approval.status != ApprovalStatus.PENDING:
        raise ValidationError("这条审批已处理")
    order = None
    if approval.kind == ApprovalKind.PENALTY and decision == ApprovalStatus.APPROVED:
        order = _create_penalty_order(approval)
    approval.status = decision
    approval.comment = comment
    approval.decided_at = timezone.now()
    approval.save(update_fields=["status", "comment", "decided_at"])
    if order is not None:
        _record_penalty_approval_logs(order, approval, user)
    if decision == ApprovalStatus.APPROVED and approval.initiator_id is not None:
        _notify_approval_in_chat(
            approval, recipient=approval.initiator, stage="approved", instruction="已通过，请前往审批中心继续处理。",
        )
    return approval
