"""Deliver after-sales notifications after their WeCom conversation summary is ready."""

import re
from datetime import timedelta
from time import monotonic

from django.db import connection, transaction
from django.db.models import Exists, F, Func, OuterRef, Q, Subquery, Value
from django.utils import timezone
from rest_framework.exceptions import APIException

from apps.accounts.admin_mutations import lock_tenant_admin_mutations
from apps.audit.models import LogType, OperationLog
from apps.business.api.order_views import OrderViewSet
from apps.business.game_scope import game_scope
from apps.business.models import Order
from apps.business.xgj_push import _TRIM_CHARS
from apps.core.scoping import scope_queryset
from apps.internal_chat.system_notice_settings import wecom_notice_window
from apps.internal_chat.system_notifications import send_order_notification
from apps.rbac.permissions import has_node_now
from apps.wecom import aftersales
from apps.wecom.archive import ArchiveError, archive_program, decrypt_message_key
from apps.wecom.models import (
    WecomAftersalesSettings, WecomAppConfig, WecomArchiveMessage, WecomArchiveOrderGroup, WecomGroupChat,
    WecomRefundNotice, WecomZoneKeywordHit,
)
from apps.wecom.zone_client import call_zone_operation
from apps.wecom.zone_events import _require_current_config


_MAX_AGE = timedelta(hours=2)
_RETRY_DELAY = timedelta(minutes=1)
_CONTEXT_WINDOW = timedelta(minutes=15)
_CONTEXT_SIZE = 12
_MAX_BATCH = 8
_ERROR_ATTEMPTS = 5
_RUN_BUDGET_SECONDS = 12
_INVALID_TEXT_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\ud800-\udfff]")


def _scoped_chats(queryset, config, policy):
    if policy.group_ids:
        groups = aftersales.eligible_groups(config).filter(chat_id__in=policy.group_ids)
        return queryset.filter(chat_id__in=groups.values("chat_id"))
    dismissed = WecomGroupChat.objects.filter(identity_revision=config.identity_revision).filter(
        Q(dismissed=True) | Q(pending_dismissal=True),
    )
    return queryset.exclude(chat_id="").exclude(chat_id__in=dismissed.values("chat_id"))


def _new_hits(config, policy, program, since):
    messages = _scoped_chats(WecomArchiveMessage.objects.filter(
        msg_id=OuterRef("msg_id"), identity_revision=config.identity_revision,
        sent_at__gte=since,
    ), config, policy)
    return WecomZoneKeywordHit.objects.filter(
        identity_revision=config.identity_revision, program_id=program, rule_id=policy.rule_id,
        has_hit_keyword=True, received_at__gte=since, refund_notice__isnull=True,
    ).filter(Exists(messages)).annotate(
        sent_when=Subquery(messages.values("sent_at")[:1]),
        message_chat_id=Subquery(messages.values("chat_id")[:1]),
    ).order_by("received_at", "pk")[:_MAX_BATCH]


def _order_candidates(config, group, program):
    links = WecomArchiveOrderGroup.objects.filter(
        config=config, identity_revision=config.identity_revision, program_id=program, chat_id=group.chat_id,
        order_id=OuterRef("pk"),
    ).exclude(matched_order_no="")
    source_duplicates = Order.objects.exclude(pk=OuterRef("pk")).alias(
        _other_source=Func(F("source_order_no"), Value(_TRIM_CHARS), function="BTRIM"),
    ).filter(Q(order_no=OuterRef("_trimmed_source")) | Q(_other_source=OuterRef("_trimmed_source")))
    link_matches = Exists(links.filter(matched_order_no=OuterRef("order_no"))) | (
        Exists(links.filter(matched_order_no=OuterRef("_trimmed_source"))) & ~Exists(source_duplicates)
    )
    attached = Q(_has_group_link=True)
    name = group.name.strip()
    if name and not group.pending:
        attached |= Q(order_no=name) | Q(_trimmed_source=name)
    return Order.objects.exclude(hall_card__isnull=False).exclude(upstream_hall_card__isnull=False).alias(
        _trimmed_source=Func(F("source_order_no"), Value(_TRIM_CHARS), function="BTRIM"),
        _has_group_link=link_matches,
    ).filter(attached).only("id", "order_no", "source_order_no").order_by("pk")


def _bind_order(notice, config, group, program):
    candidates = list(_order_candidates(config, group, program)[:2])
    if not candidates:
        _defer(notice, "unmatched")
        return False
    order = candidates[0]
    if len(candidates) != 1 or notice.order_id not in (None, order.pk):
        _defer(notice, "ambiguous")
        return False
    if notice.order_id is None:
        notice.order_id = order.pk
        notice.save(update_fields=["order"])
    return True


def _defer(notice, problem):
    notice.problem = problem
    notice.save(update_fields=["problem"])


def _stop(notice, problem, now):
    notice.problem = problem
    notice.closed_at = now
    notice.save(update_fields=["problem", "closed_at"])


def _retry_error(notice, problem, now):
    notice.failures += 1
    notice.problem = problem
    notice.closed_at = now if notice.failures >= _ERROR_ATTEMPTS else None
    notice.next_attempt_at = now + _RETRY_DELAY * 2**(notice.failures - 1)
    notice.save(update_fields=["failures", "problem", "closed_at", "next_attempt_at"])


def _summary_input(notice, config):
    messages = WecomArchiveMessage.objects.filter(identity_revision=config.identity_revision, chat_id=notice.chat_id)
    trigger = messages.filter(msg_id=notice.hit.msg_id, sent_at=notice.message_sent_at).first()
    if trigger is None:
        raise ArchiveError("摘要缺少命中消息")
    earlier = list(messages.filter(sent_at__gte=trigger.sent_at - _CONTEXT_WINDOW).filter(
        Q(sent_at__lt=trigger.sent_at) | Q(sent_at=trigger.sent_at, pk__lt=trigger.pk),
    ).order_by("-sent_at", "-pk")[:_CONTEXT_SIZE - 1])
    return [{"msgid": row.msg_id, "encrypt_info": {"secret_key": decrypt_message_key(row)}}
            for row in [*reversed(earlier), trigger]]


def _failed_trigger(notice, result):
    failures = result.get("fail_list", [])
    return not isinstance(failures, list) or any(
        not isinstance(row, dict) or row.get("msgid") == notice.hit.msg_id for row in failures
    )


def _request_summary(notice, config, deadline, now):
    if not notice.summary_job_id:
        result = call_zone_operation(config, "create_summary_task", {"msg_list": _summary_input(notice, config)},
                                     deadline=deadline)
        job_id = result.get("jobid")
        if not isinstance(job_id, str) or not 0 < len(job_id) <= 256 or not job_id.isprintable():
            raise ArchiveError("企微摘要任务编号异常")
        if _failed_trigger(notice, result):
            _stop(notice, "summary_rejected", now)
            return
        notice.summary_job_id = job_id
        notice.failures = 0
        notice.problem = ""
        notice.save(update_fields=["summary_job_id", "failures", "problem"])
        return

    result = call_zone_operation(config, "get_summary_result", {"jobid": notice.summary_job_id}, deadline=deadline)
    status = result.get("status")
    if type(status) is not int or status not in (0, 1, 2):
        raise ArchiveError("企微摘要任务状态异常")
    if status == 0:
        if notice.failures:
            notice.failures = 0
            notice.problem = ""
            notice.save(update_fields=["failures", "problem"])
        return
    summary = result.get("response_data")
    if isinstance(summary, str):
        summary = _INVALID_TEXT_CHARACTERS.sub("", summary).strip()[:3000]
    if status == 2 or _failed_trigger(notice, result) or not isinstance(summary, str) or not summary:
        _stop(notice, "summary_failed", now)
        return
    notice.summary = summary
    notice.failures = 0
    notice.problem = ""
    notice.next_attempt_at = now
    notice.save(update_fields=["summary", "failures", "problem", "next_attempt_at"])


def _creator_id(order):
    # A reused username cannot stand in for the authenticated actor on the creation log.
    actors = list(OperationLog.objects.filter(
        log_type=LogType.ORDER, action__in=("order.create", "order.copy"), actor_id__gt=0,
        target_kind="order", target_id=order.order_no, order_no=order.order_no,
    ).order_by().values_list("actor_id", flat=True).distinct()[:2])
    return actors[0] if len(actors) == 1 else None


def _recipients(order, policy):
    roles = WecomAftersalesSettings.OrderRecipient
    selected = set(policy.fixed_recipient_ids)
    if policy.order_recipient == roles.CREATOR:
        selected.add(_creator_id(order))
    elif policy.order_recipient == roles.PUBLISHER:
        selected.add(order.hall_publisher_id)
    elif policy.order_recipient == roles.SALES:
        selected.add(order.sales_user_id)
    orders = Order.objects.filter(pk=order.pk)
    return [
        user for user in aftersales.active_users().filter(pk__in=selected)
        if has_node_now(user, *OrderViewSet.ORDER_PERMISSIONS["retrieve"])
        and scope_queryset(user, orders, scope_filter=game_scope("game_id")).exists()
    ]


def _send(notice, config, group, program, now, policy):
    order = Order.objects.select_for_update().filter(pk=notice.order_id).first()
    if order is None:
        _defer(notice, "unmatched")
        return False
    if not _bind_order(notice, config, group, program):
        return False
    recipients = _recipients(order, policy)
    if not recipients:
        _defer(notice, "no_visible_recipient")
        return False
    rule_title = "「我要退款」" if policy.legacy else "售后纠纷通知规则"
    message = f"企微群聊命中{rule_title}，请查看订单。\n\n会话摘要\n{notice.summary}"
    with transaction.atomic():
        lock_tenant_admin_mutations()
        enabled, resumed_at = wecom_notice_window()
        if not enabled or (resumed_at is not None and notice.message_sent_at < resumed_at):
            return False
        for recipient in recipients:
            result = send_order_notification(
                recipient, order_no=order.order_no, text=message, client_id=f"wecom-refund:{notice.pk}",
            )
            if not isinstance(result, dict) or type(result.get("id")) is not int or result["id"] <= 0:
                raise APIException("系统通知保存未确认")
    notice.recipient = recipients[0]
    notice.delivered_recipient_ids = [recipient.pk for recipient in recipients]
    notice.sent_at = now
    notice.closed_at = now
    notice.failures = 0
    notice.problem = ""
    notice.save(update_fields=["recipient", "delivered_recipient_ids", "sent_at", "closed_at", "failures", "problem"])
    return True


@transaction.atomic
def _process(notice_id, config, program, now, deadline, *, deliver=False):
    notice = WecomRefundNotice.objects.select_for_update(of=("self",), skip_locked=True).select_related("hit").filter(
        pk=notice_id, closed_at__isnull=True,
    ).first()
    if notice is None or bool(notice.summary) != deliver or notice.next_attempt_at > now:
        return False
    if notice.message_sent_at <= now - _MAX_AGE:
        _stop(notice, "expired", now)
        return False
    try:
        _require_current_config(config, program)
    except ArchiveError:
        _defer(notice, "config_changed")
        return False
    policy = aftersales.current_policy(config)
    if (
        policy is None or notice.hit.rule_id != policy.rule_id or not notice.chat_id
        or (policy.group_ids and notice.chat_id not in policy.group_ids)
        or notice.message_sent_at < policy.started_at or notice.hit.received_at < policy.started_at
    ):
        return False
    notice.next_attempt_at = now + _RETRY_DELAY
    notice.save(update_fields=["next_attempt_at"])
    if policy.group_ids:
        groups = aftersales.eligible_groups(config)
    else:
        groups = WecomGroupChat.objects.filter(identity_revision=config.identity_revision)
    groups = groups.filter(chat_id=notice.chat_id)
    if deliver:
        groups = groups.select_for_update()
    group = groups.first()
    if group is None:
        if policy.group_ids:
            return False
        # With no group snapshot, only a verified archive-to-order binding may match.
        group = WecomGroupChat(chat_id=notice.chat_id, pending=True)
    if group.dismissed or group.pending_dismissal:
        return False
    if deliver:
        try:
            return _send(notice, config, group, program, now, policy)
        except APIException:
            _retry_error(notice, "delivery_unavailable", now)
    elif _bind_order(notice, config, group, program):
        try:
            _request_summary(notice, config, deadline, now)
        except ArchiveError:
            _retry_error(notice, "summary_unavailable", now)
    return False


def run_due(*, now=None):
    if connection.schema_name != "kexing":
        return 0
    config = WecomAppConfig.objects.filter(tenant_schema=connection.schema_name).first()
    if not aftersales.available(config):
        return 0
    now = now or timezone.now()
    program = archive_program()
    pending = WecomRefundNotice.objects.filter(
        hit__identity_revision=config.identity_revision, hit__program_id=program, closed_at__isnull=True,
    )
    pending.filter(message_sent_at__lte=now - _MAX_AGE).update(closed_at=now, problem="expired")
    aftersales.sync_settings(config, now)
    enabled, resumed_at = wecom_notice_window()
    if not enabled:
        return 0
    policy = aftersales.current_policy(config)
    if policy is None:
        return 0
    start = max(policy.started_at, resumed_at) if resumed_at is not None else policy.started_at
    since = max(start, now - _MAX_AGE)
    notices = _scoped_chats(pending.filter(
        hit__rule_id=policy.rule_id,
        hit__received_at__gte=start, message_sent_at__gte=start,
    ), config, policy)
    for hit in _new_hits(config, policy, program, since):
        WecomRefundNotice.objects.get_or_create(
            hit=hit, defaults={"chat_id": hit.message_chat_id, "message_sent_at": hit.sent_when, "next_attempt_at": now},
        )

    deadline = monotonic() + _RUN_BUDGET_SECONDS
    due = list(notices.filter(next_attempt_at__lte=now).order_by("next_attempt_at", "pk")
               .values_list("id", "summary")[:_MAX_BATCH])
    delivered = 0
    for notice_id, summary in due:
        if monotonic() >= deadline:
            break
        if not summary:
            _process(notice_id, config, program, now, deadline)
        if monotonic() < deadline:
            delivered += _process(notice_id, config, program, now, deadline, deliver=True)
    return delivered
