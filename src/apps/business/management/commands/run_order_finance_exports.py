from django.conf import settings
from django.core.management.base import BaseCommand
from django_tenants.utils import get_public_schema_name, schema_context, tenant_context

from apps.business.export_jobs import remove_expired_exports, run_export
from apps.platform.models import Tenant


class Command(BaseCommand):
    help = "在当前工作室数据库执行待处理的订单财务导出"

    def handle(self, *args, **options):
        public = get_public_schema_name()
        with schema_context(public):
            tenants = list(Tenant.objects.exclude(schema_name=public).order_by("schema_name"))
        for tenant in tenants:
            with tenant_context(tenant):
                remove_expired_exports()
                if (tenant.is_active and settings.TT_ORDER_FINANCE_ASYNC_EXPORTS
                        and tenant.schema_name in settings.TT_ORDER_FINANCE_ASYNC_EXPORT_SCHEMAS):
                    run_export()
