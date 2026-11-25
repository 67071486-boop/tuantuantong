from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, schema_context, tenant_context

from apps.internal_chat.order_grab_qr_notices import run_due
from apps.platform.models import Tenant


class Command(BaseCommand):
    help = "发送本机工作室到期的接单群码系统通知"

    def handle(self, *args, **options):
        public = get_public_schema_name()
        with schema_context(public):
            tenants = list(Tenant.objects.exclude(schema_name=public).order_by("schema_name"))

        failed = False
        for tenant in tenants:
            try:
                with tenant_context(tenant):
                    attempted = run_due()
            except Exception as exc:
                failed = True
                self.stderr.write(f"{tenant.schema_name}: 接单群码通知处理失败（{type(exc).__name__}）")
                continue
            if attempted:
                self.stdout.write(f"{tenant.schema_name}: 本轮处理接单群码通知 {attempted} 条")
        if failed:
            raise CommandError("有工作室的接单群码通知未处理，详见上方")
