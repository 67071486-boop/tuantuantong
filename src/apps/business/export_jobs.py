import hashlib
import json
import logging
from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.order_export_views import XLSX_CONTENT_TYPE, render_order_export
from apps.business.api.order_views import order_search_params
from apps.business.api.serializers import OrderSearchSerializer, SettlementSearchSerializer
from apps.business.api.settlement_views import (
    render_settlement_export,
    scoped_settlement_rows,
    settlement_export_params,
)
from apps.business.game_scope import game_scope, games_of
from apps.business.models import FinanceExportJob
from apps.business.order_flags import scope_by_creator
from apps.business.performance_amount import AmountError
from apps.business.selectors import search_orders
from apps.core.permissions import is_tenant_admin
from apps.core.plane import on_public_plane
from apps.core.scoping import scope_queryset
from apps.core.storage import PRIVATE_FINANCE_EXPORT_PREFIX, get_storage
from apps.rbac.permissions import has_node, nodes_of

logger = logging.getLogger(__name__)
Job = FinanceExportJob
Kind = Job.Kind
Status = Job.Status
PERMISSIONS = {
    Kind.ORDERS: ("order.export",),
    Kind.SETTLEMENTS: ("finance.payroll", "finance.export_performance"),
}
AUDIT_ACTIONS = {Kind.ORDERS: "order.export", Kind.SETTLEMENTS: "settlement.export"}
LIFETIME = timedelta(hours=2)
LEASE = timedelta(minutes=3)
MAX_ATTEMPTS = 3
MAX_ACTIVE_FOR_OWNER = 3
MAX_JOBS_PER_TENANT = 64
MAX_BYTES = 24 * 1024 * 1024
ENQUEUE_LOCK_ID = 747680314


class FinanceExportAccess:
    kind = None

    def get_permissions(self):
        return [has_node(node)() for node in PERMISSIONS[self.kind]]


def export_parameters(kind, raw):
    if not isinstance(raw, dict) or any(type(value) not in {str, int, bool} for value in raw.values()):
        raise ValidationError("请填写有效的导出筛选条件")
    fields = OrderSearchSerializer().fields if kind == Kind.ORDERS else SettlementSearchSerializer().fields
    filters = {k: value for k, value in raw.items() if k in fields}
    if len(json.dumps(filters, ensure_ascii=False).encode()) > 32768:
        raise ValidationError("导出筛选条件过长，请缩小范围")
    return filters, _validated_filters(kind, filters)


def _validated_filters(kind, filters):
    if kind == Kind.ORDERS:
        return order_search_params(SimpleNamespace(query_params=filters), OrderSearchSerializer)
    return settlement_export_params(filters)


def export_authorization(user, kind):
    if on_public_plane() or not user or not user.is_active:
        raise PermissionDenied("你没有导出权限")
    nodes = nodes_of(user)
    if not nodes.issuperset(PERMISSIONS[kind]):
        raise PermissionDenied("你没有导出权限")
    is_admin = is_tenant_admin(user)
    identity = (
        connection.schema_name, user.pk, user.get_username(), kind, is_admin, sorted(nodes),
        [] if is_admin else list(games_of(user).order_by("pk").values_list("pk", flat=True)),
    )
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def enqueue_export(user, kind, raw):
    authorization = export_authorization(user, kind)
    filters, _ = export_parameters(kind, raw)
    now = timezone.now()
    with transaction.atomic():
        # The enqueue count and insert share a per-database lock.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [ENQUEUE_LOCK_ID])
        retained = Job.objects.filter(expires_at__gt=now)
        active = retained.filter(status__in=(Status.PENDING, Status.RUNNING), requester=user)
        existing = active.filter(kind=kind, authorization=authorization, filters=filters).first()
        if existing is not None:
            return existing
        if retained.count() >= MAX_JOBS_PER_TENANT or active.count() >= MAX_ACTIVE_FOR_OWNER:
            raise ValidationError("导出任务较多，请稍后重试")
        return Job.objects.create(
            requester=user, kind=kind, filters=filters, authorization=authorization, expires_at=now + LIFETIME,
        )


def owner_exports(user, kind):
    if on_public_plane():
        raise PermissionDenied("你没有导出权限")
    return Job.objects.filter(requester=user, kind=kind, expires_at__gt=timezone.now())


def object_key(job_id, attempt):
    return f"{PRIVATE_FINANCE_EXPORT_PREFIX}/{connection.schema_name}/{job_id}/{attempt}.xlsx"


def _claim_export():
    now = timezone.now()
    with transaction.atomic():
        candidates = Job.objects.filter(expires_at__gt=now).filter(
            Q(status=Status.PENDING) | Q(status=Status.RUNNING, lease_until__lte=now),
        )
        job = candidates.select_for_update(skip_locked=True).order_by("created_at").first()
        if job is None:
            return None
        if job.attempts >= MAX_ATTEMPTS:
            job.status = Status.FAILED
            job.message = "生成文件失败，请重新导出"
            job.lease_until = None
            job.save(update_fields=["status", "message", "lease_until"])
            return None
        job.status = Status.RUNNING
        job.attempts += 1
        job.lease_until = now + LEASE
        job.save(update_fields=["status", "attempts", "lease_until"])
        return job


def _live_attempt(job):
    now = timezone.now()
    return Job.objects.select_for_update().filter(
        pk=job.pk, attempts=job.attempts, status=Status.RUNNING,
        lease_until__gt=now, expires_at__gt=now,
    ).first()


def _record_failure(job, message, *, retry=False):
    with transaction.atomic():
        stored = _live_attempt(job)
        if stored is None:
            return
        will_retry = retry and stored.attempts < MAX_ATTEMPTS
        stored.status = Status.PENDING if will_retry else Status.FAILED
        stored.lease_until = None
        stored.message = "" if will_retry else message[:250]
        stored.save(update_fields=["status", "lease_until", "message"])


def require_live_export_rows(user, kind, row_scope):
    if not isinstance(row_scope, list):
        raise PermissionDenied("文件缺少可见范围记录，请重新导出")
    if not row_scope:
        return
    if kind == Kind.ORDERS:
        current = scope_queryset(user, search_orders().filter(pk__in=row_scope), scope_filter=game_scope("game_id"))
        all_visible = scope_by_creator(user, current).count() == len(row_scope)
    else:
        exported = {tuple(pair) for pair in row_scope}
        current = scoped_settlement_rows(user, {}).filter(pk__in=[pk for pk, _ in exported]).order_by()
        all_visible = set(current.values_list("pk", "order_id")) == exported
    if not all_visible:
        raise PermissionDenied("部分导出记录已不在可见范围内，请重新导出")


def _publish(job, size, row_scope):
    with transaction.atomic():
        stored = _live_attempt(job)
        if stored is None:
            return False
        user = get_user_model().objects.filter(pk=stored.requester_id).first()
        if export_authorization(user, stored.kind) != stored.authorization:
            raise PermissionDenied("导出权限或可见范围已变，请重新导出")
        require_live_export_rows(user, stored.kind, row_scope)
        stored.row_scope = row_scope
        stored.status = Status.READY
        stored.size = size
        stored.ready_at = timezone.now()
        stored.lease_until = None
        stored.save(update_fields=["status", "size", "ready_at", "lease_until", "row_scope"])
        action = AUDIT_ACTIONS[stored.kind]
        record(log_type=ACTION_LOG_TYPES[action], action=action, actor=user.get_username(), actor_id=user.pk)
        return True


def _generate_workbook(job):
    # The worker renders under one authorization and data snapshot.
    needs_snapshot = transaction.get_autocommit()
    with transaction.atomic():
        if needs_snapshot:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        user = get_user_model().objects.filter(pk=job.requester_id).first()
        if export_authorization(user, job.kind) != job.authorization:
            raise PermissionDenied("导出权限或可见范围已变，请重新导出")
        filters = _validated_filters(job.kind, job.filters)
        render = render_order_export if job.kind == Kind.ORDERS else render_settlement_export
        return render(user, filters)


def run_export():
    if on_public_plane():
        raise RuntimeError("导出任务必须在工作室内执行")
    job = _claim_export()
    if job is None:
        return False
    key = object_key(job.pk, job.attempts)
    storage = None
    try:
        payload, row_scope = _generate_workbook(job)
        if len(payload) > MAX_BYTES:
            raise ValidationError("导出文件过大，请缩小筛选范围")
        storage = get_storage()
        storage.put_bytes(key, payload, content_type=XLSX_CONTENT_TYPE)
        if _publish(job, len(payload), row_scope):
            return True
    except (ValidationError, PermissionDenied) as exc:
        detail = exc.detail
        message = str(detail[0] if isinstance(detail, list) and detail else detail)
        _record_failure(job, message)
    except AmountError as exc:
        _record_failure(job, exc.message)
    except Exception as exc:
        logger.warning("财务导出本次执行失败 (%s): %s", type(exc).__name__, job.pk)
        _record_failure(job, "生成文件失败，请重新导出", retry=True)
    if storage is not None:
        # If commit outcome is uncertain, only remove a file proven to be unpublished.
        if not Job.objects.filter(pk=job.pk, attempts=job.attempts, status=Status.READY).exists():
            storage.delete(key)
    return True


def remove_expired_exports():
    if on_public_plane():
        raise RuntimeError("导出任务清理必须在工作室内执行")
    # Allow an expired worker time to finish closing its files before reaping.
    deadline = timezone.now() - LEASE
    expired = Job.objects.filter(expires_at__lte=deadline).order_by("expires_at")
    for pk, attempts in expired.values_list("pk", "attempts")[:16]:
        if attempts:
            storage = get_storage()
            for attempt in range(1, attempts + 1):
                storage.delete(object_key(pk, attempt))
        expired.filter(pk=pk).delete()
