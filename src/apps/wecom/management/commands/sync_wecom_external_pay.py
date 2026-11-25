"""逐租户同步企微对外收款。"""
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.platform.models import Tenant
from apps.wecom.external_pay import SyncError, run_due


class Command(BaseCommand):
    help = "同步本机租户的企微对外收款"

    def handle(self, *args, **options):
        failed = False
        for tenant in Tenant.objects.exclude(schema_name=get_public_schema_name()):
            try:
                with tenant_context(tenant):
                    run_due()
            except SyncError as exc:
                failed = True
                self.stderr.write(f"{tenant.schema_name}: {exc}")
        if failed:
            raise CommandError("部分租户的企微收款同步失败")
