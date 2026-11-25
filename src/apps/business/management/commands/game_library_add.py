from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django_tenants.utils import tenant_context

from apps.business.models import GameLibrary


class Command(BaseCommand):
    help = "新增/更新一款平台游戏库条目（默认当前租户库；public 下必须给 --schema；新租户默认只有基础库）"

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--first-key", default=None, help="字母索引，如 L")
        parser.add_argument("--sort-order", type=int, default=None)
        parser.add_argument("--schema", default=None, help="租户 schema 名（public 下必填）")
        state = parser.add_mutually_exclusive_group()
        state.add_argument("--active", dest="is_active", action="store_const", const=True, default=None)
        state.add_argument("--inactive", dest="is_active", action="store_const", const=False)

    def handle(self, *args, **options):
        schema = options["schema"]
        if schema:
            from apps.platform.models import Tenant

            tenant = Tenant.objects.filter(schema_name=schema).first()
            if tenant is None:
                raise CommandError(f"找不到租户 schema：{schema}")
            with tenant_context(tenant):
                self._write(options)
            return
        if connection.schema_name == "public":
            raise CommandError("当前在 public 平面，请用 --schema <租户 schema> 指定要写入的租户")
        self._write(options)

    def _write(self, options):
        name = options["name"].strip()
        if not name:
            raise CommandError("游戏名称不能为空")
        entry = GameLibrary.objects.filter(name=name).first()
        if entry is None:
            current = GameLibrary.objects.order_by("-sort_order").values_list("sort_order", flat=True).first()
            entry = GameLibrary.objects.create(
                name=name,
                first_key=(options["first_key"] or "").strip(),
                sort_order=options["sort_order"] if options["sort_order"] is not None else (current or 0) + 1,
                is_active=options["is_active"] is not False,
            )
            action = "已新增"
        else:
            if options["first_key"] is not None:
                entry.first_key = options["first_key"].strip()
            if options["sort_order"] is not None:
                entry.sort_order = options["sort_order"]
            if options["is_active"] is not None:
                entry.is_active = options["is_active"]
            entry.save()
            action = "已更新"
        self.stdout.write(f"{action}：{entry.name}（first_key={entry.first_key}，sort_order={entry.sort_order}）")
