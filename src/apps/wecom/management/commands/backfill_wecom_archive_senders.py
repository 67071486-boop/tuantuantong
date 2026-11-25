from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime
from django_tenants.utils import get_public_schema_name, schema_context, tenant_context

from apps.platform.models import Tenant
from apps.wecom import archive
from apps.wecom.archive_sender_repair import backfill_senders
from apps.wecom.models import WecomAppConfig


def _parse_time(value):
    try:
        parsed = parse_datetime(value)
    except ValueError:
        parsed = None
    if parsed is None:
        raise CommandError("时间请用 ISO 8601 格式，带时区偏移")
    return parsed


class Command(BaseCommand):
    help = "预览或补齐已存档企微群消息的发送人 ID 和类型。"

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="企微存档工作室的 schema")
        parser.add_argument("--start", required=True, help="包含起点的 ISO 8601 时间")
        parser.add_argument("--end", required=True, help="不包含终点的 ISO 8601 时间")
        parser.add_argument("--chat-id", action="append", default=[], help="限定企微群 ID，可重复指定")
        parser.add_argument("--max-batches", type=int, default=12, help="最多拉取的历史批数，默认 12")
        parser.add_argument("--apply", action="store_true", help="写回缺失的发送人资料，缺省只预览")

    def handle(self, *args, **options):
        schema = options["tenant"]
        if schema != archive.configured_archive_tenant():
            raise CommandError("指定工作室与当前企微存档配置不符")
        start, end = _parse_time(options["start"]), _parse_time(options["end"])
        with schema_context(get_public_schema_name()):
            tenant = Tenant.objects.filter(schema_name=schema).first()
        if tenant is None:
            raise CommandError("当前数据库没有配置的企微存档工作室")

        with tenant_context(tenant):
            config = WecomAppConfig.objects.filter(tenant_schema=schema, enabled=True).first()
            if config is None:
                raise CommandError("当前工作室的企微自建应用未启用")
            try:
                result = backfill_senders(
                    config, start, end, chat_ids=options["chat_id"],
                    max_batches=options["max_batches"], apply=options["apply"],
                )
            except archive.ArchiveError as exc:
                raise CommandError(str(exc)) from None

        mode = "补齐" if options["apply"] else "预览"
        self.stdout.write(f"{mode}：近期缺发送人 {result.eligible} 条，可补 {result.recovered} 条，已写回 {result.updated} 条。")
        if result.expired:
            self.stdout.write(f"已超过企微可重取窗口 {result.expired} 条。")
        if result.limited:
            self.stdout.write("已达到本次拉取批数上限，仍有消息待核对，可分时间段继续。")
