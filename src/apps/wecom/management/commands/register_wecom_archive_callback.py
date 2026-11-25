from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, schema_context, tenant_context

from apps.platform.models import Tenant
from apps.wecom import archive
from apps.wecom.models import WecomAppConfig


class Command(BaseCommand):
    help = "为当前配置的工作室自建应用登记专区程序。企微每个应用只保留一个接收程序。"

    def handle(self, *args, **options):
        with schema_context(get_public_schema_name()):
            tenant = Tenant.objects.filter(schema_name=archive.configured_archive_tenant()).first()
        if tenant is None:
            raise CommandError("当前数据库没有配置的企微存档工作室")

        with tenant_context(tenant):
            program_id = archive.archive_program()
            if not program_id:
                raise CommandError("请先配置当前工作室的 WECOM_ARCHIVE_PROGRAM_ID")
            config = WecomAppConfig.objects.filter(tenant_schema=tenant.schema_name).first()
            if config is None or not config.is_ready():
                raise CommandError("当前工作室的企微自建应用未启用或配置不完整")
            try:
                archive.register_archive_callback(config, program_id)
            except archive.ArchiveError as exc:
                raise CommandError(str(exc)) from None

        self.stdout.write(self.style.SUCCESS("企微已确认登记当前工作室的专区回调接收程序"))
