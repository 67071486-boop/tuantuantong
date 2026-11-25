"""处理企微通知出箱。"""
import logging
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, tenant_context
from apps.platform.models import Tenant
from apps.wecom.notifications import run_due

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = '发送企微通知出箱'

    def handle(self, *args, **options):
        failed = False
        for tenant in Tenant.objects.exclude(schema_name=get_public_schema_name()):
            try:
                with tenant_context(tenant):
                    count = run_due()
                self.stdout.write(f'{tenant.schema_name}: 已处理 {count}')
            except Exception:
                failed = True
                logger.exception('企微通知任务失败 tenant=%s', tenant.schema_name)
        if failed:
            raise CommandError('有租户的企微通知任务处理失败')
