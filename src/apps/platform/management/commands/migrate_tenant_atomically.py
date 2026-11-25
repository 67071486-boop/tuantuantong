from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django_tenants.utils import get_public_schema_name, schema_context

from apps.platform.models import Tenant


class Command(BaseCommand):
    help = "在一笔事务里迁移现有单租户库，避免旧代码看见迁移中间态。"

    def add_arguments(self, parser):
        parser.add_argument("schema_name")

    def handle(self, *args, **options):
        schema_name = options["schema_name"]
        public = get_public_schema_name()
        with schema_context(public), transaction.atomic():
            schemas = list(Tenant.objects.exclude(schema_name=public).values_list("schema_name", flat=True))
            if schemas != [schema_name]:
                raise CommandError("当前库必须只包含指定的待升级租户")
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '5s'")
                cursor.execute("SET LOCAL statement_timeout = '60s'")
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            call_command("migrate_schemas", interactive=False)
