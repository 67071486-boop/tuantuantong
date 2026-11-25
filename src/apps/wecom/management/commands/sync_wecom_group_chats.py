"""消费本机租户的企微群变更任务。"""
import logging

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.platform.models import Tenant
from apps.wecom.archive import run_due as sync_archive
from apps.wecom.group_sync import run_due
from apps.wecom.group_qr import cleanup_due
from apps.wecom.refund_notices import run_due as send_refund_notices
from apps.wecom.zone_events import run_due as sync_zone_events

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "刷新企微客户群的待处理变更"

    def handle(self, *args, **options):
        failed = False
        for tenant in Tenant.objects.exclude(schema_name=get_public_schema_name()):
            try:
                with tenant_context(tenant):
                    count = run_due()
                    cleaned = cleanup_due()
                self.stdout.write(f"{tenant.schema_name}: {count} 清理候选: {cleaned}")
            except Exception:
                failed = True
                logger.exception("企微群任务处理失败 tenant=%s", tenant.schema_name)
            try:
                with tenant_context(tenant):
                    archived = sync_archive()
                if archived:
                    self.stdout.write(f"{tenant.schema_name}: 同步会话记录 {archived} 条")
            except Exception:
                failed = True
                logger.exception("企微会话同步任务异常 tenant=%s", tenant.schema_name)
            try:
                with tenant_context(tenant):
                    zone_events = sync_zone_events()
                if zone_events:
                    self.stdout.write(f"{tenant.schema_name}: 处理专区通知 {zone_events} 条")
            except Exception:
                failed = True
                logger.exception("企微专区事件处理异常 tenant=%s", tenant.schema_name)
            try:
                with tenant_context(tenant):
                    refund_notices = send_refund_notices()
                if refund_notices:
                    self.stdout.write(f"{tenant.schema_name}: 已发企微退款提醒 {refund_notices} 条")
            except Exception as exc:
                failed = True
                logger.error("企微退款提醒处理异常 tenant=%s error_type=%s", tenant.schema_name, type(exc).__name__)
        if failed:
            raise CommandError("有租户的企微群任务处理失败")
