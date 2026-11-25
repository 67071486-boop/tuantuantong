from django.conf import settings
from django.db import connection
from django.http import FileResponse
from django.utils import timezone
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.views import APIView

from apps.business.api.order_export_views import XLSX_CONTENT_TYPE, order_export_disposition
from apps.business.export_jobs import (
    FinanceExportAccess, Kind, Status, enqueue_export, export_authorization, object_key, owner_exports,
    require_live_export_rows,
)
from apps.core.envelope import api_response
from apps.core.plane import on_public_plane
from apps.core.storage import get_storage


def exports_enabled():
    return (settings.TT_ORDER_FINANCE_ASYNC_EXPORTS
            and connection.schema_name in settings.TT_ORDER_FINANCE_ASYNC_EXPORT_SCHEMAS)


class ExportJobsUnavailable(APIException):
    status_code = 404
    default_detail = "后台导出未开启"


def job_data(job):
    return {
        "id": str(job.pk),
        "status": job.status,
        "message": job.message,
        "created_at": job.created_at.isoformat(),
        "expires_at": job.expires_at.isoformat(),
    }


class ExportJobView(FinanceExportAccess, APIView):
    def initial(self, request, *args, **kwargs):
        if on_public_plane():
            raise ExportJobsUnavailable()
        return super().initial(request, *args, **kwargs)

    def finalize_response(self, request, response, *args, **kwargs):
        response["Cache-Control"] = "private, no-store"
        return super().finalize_response(request, response, *args, **kwargs)

    def owner_job(self, request, job_id):
        if not exports_enabled():
            raise NotFound()
        job = owner_exports(request.user, self.kind).filter(pk=job_id).first()
        if job is None:
            raise NotFound()
        if export_authorization(request.user, self.kind) != job.authorization:
            raise PermissionDenied("导出权限或可见范围已变，请重新导出")
        return job


class ExportJobCollectionView(ExportJobView):
    def post(self, request):
        if not exports_enabled():
            return api_response(data=None)
        job = enqueue_export(request.user, self.kind, request.data)
        return api_response(data=job_data(job), status=201)

    def get(self, request):
        if not exports_enabled():
            return api_response(data=[])
        authorization = export_authorization(request.user, self.kind)
        jobs = owner_exports(request.user, self.kind).filter(authorization=authorization).order_by("-created_at")
        return api_response(data=[job_data(job) for job in jobs[:5]])


class ExportJobStatusView(ExportJobView):
    def get(self, request, job_id):
        return api_response(data=job_data(self.owner_job(request, job_id)))


class ExportJobDownloadView(ExportJobView):
    def get(self, request, job_id):
        job = self.owner_job(request, job_id)
        if job.status != Status.READY:
            raise ValidationError(job.message or "文件还在准备中")
        require_live_export_rows(request.user, job.kind, job.row_scope)
        storage = get_storage()
        key = object_key(job.pk, job.attempts)
        meta = storage.head(key)
        if meta is None or meta[0] != job.size:
            raise NotFound()
        try:
            stream = storage.open_stream(key)
        except FileNotFoundError as exc:
            raise NotFound() from exc
        response = FileResponse(stream, content_type=XLSX_CONTENT_TYPE)
        response["Content-Length"] = str(job.size)
        response["Content-Disposition"] = (
            order_export_disposition(timezone.localtime(job.ready_at))
            if job.kind == Kind.ORDERS else 'attachment; filename="settlements.xlsx"'
        )
        return response
