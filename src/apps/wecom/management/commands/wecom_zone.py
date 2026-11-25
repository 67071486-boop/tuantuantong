import json
import os
from contextlib import nullcontext
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, schema_context, tenant_context

from apps.platform.models import Tenant
from apps.wecom import archive, zone_client
from apps.wecom.models import WecomAppConfig


def _read_file(path, max_bytes):
    try:
        if not path.is_file():
            raise CommandError("请输入普通文件路径")
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        raise CommandError("文件读取失败") from None
    if len(data) > max_bytes:
        raise CommandError("文件超出大小限制")
    return data


def _read_params(path):
    if path is None:
        return {}
    try:
        params = json.loads(_read_file(Path(path), zone_client.MAX_PAYLOAD_BYTES))
    except (UnicodeError, ValueError, RecursionError):
        raise CommandError("参数文件须为 JSON 对象") from None
    if not isinstance(params, dict):
        raise CommandError("参数文件须为 JSON 对象")
    return params


def _open_result_file(path):
    try:
        if path is None:
            return nullcontext()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return os.fdopen(fd, "w", encoding="utf-8")
    except OSError:
        raise CommandError("新结果文件创建失败，请检查目录并使用尚未存在的路径") from None


class Command(BaseCommand):
    help = "调用当前工作室的企微数据与智能专区。结果默认不输出业务数据。"

    def add_arguments(self, parser):
        modes = parser.add_subparsers(dest="mode", required=True)
        for mode in ("sync", "async"):
            command = modes.add_parser(mode)
            command.add_argument("operation", choices=sorted(zone_client.ZONE_OPERATIONS))
            command.add_argument("--params-file")
            command.add_argument("--result-file")
        result = modes.add_parser("result")
        result.add_argument("job_reference")
        result.add_argument("--result-file")
        admin = modes.add_parser("admin")
        admin.add_argument("action", choices=sorted(zone_client.ADMIN_ENDPOINTS))
        admin.add_argument("--params-file")
        admin.add_argument("--upload-file")
        admin.add_argument("--result-file")

    def handle(self, *args, **options):
        with schema_context(get_public_schema_name()):
            tenant = Tenant.objects.filter(schema_name=archive.configured_archive_tenant()).first()
        if tenant is None:
            raise CommandError("当前数据库没有配置的企微存档工作室")

        with tenant_context(tenant):
            if not zone_client.zone_enabled():
                raise CommandError("请先配置并启用当前工作室的企微专区扩展")
            config = WecomAppConfig.objects.filter(tenant_schema=tenant.schema_name, enabled=True).first()
            if config is None or not config.is_ready():
                raise CommandError("当前工作室的企微自建应用未启用或配置不完整")
            params = _read_params(options.get("params_file"))
            upload = None
            if options.get("upload_file"):
                upload_path = Path(options["upload_file"])
                limit = (zone_client.MAX_EXPORT_TEMPLATE_BYTES if options.get("action") == "upload_export_template"
                         else zone_client.MAX_UPLOAD_BYTES)
                upload = (upload_path.name, _read_file(upload_path, limit))
            with _open_result_file(options.get("result_file")) as destination:
                try:
                    result = self._execute(config, options, params, upload)
                except zone_client.ZoneError as exc:
                    raise CommandError(str(exc)) from None
                if destination is not None:
                    try:
                        json.dump(result, destination, ensure_ascii=False)
                        destination.write("\n")
                        destination.flush()
                    except (TypeError, ValueError, OSError):
                        raise CommandError("企微已响应，但写入结果文件失败") from None

        if options["mode"] == "async":
            self.stdout.write("企微已受理异步请求。")
            if destination is None:
                self.stdout.write(result["job_reference"])
        else:
            self.stdout.write("企微已返回结果。")
            for key, title in (("status", "任务状态"), ("response_errcode", "分析任务返回码"),
                               ("result_errcode", "导出任务返回码"), ("learn_status", "学习状态")):
                if type(result.get(key)) is int:
                    self.stdout.write(f"{title} {result[key]}")
            for key, title in (("fail_list", "失败条目"), ("rejected_item_list", "拒绝入库条目"),
                               ("fail_tag_group_id_list", "无效标签组"), ("filtered_content_list", "不可用文档")):
                value = result.get(key)
                if isinstance(value, list) and value:
                    self.stdout.write(f"{title} {len(value)} 项")
        if destination is not None:
            self.stdout.write("完整结果已写入指定的本地受限文件。")

    def _execute(self, config, options, params, upload):
        match options["mode"]:
            case "sync":
                return zone_client.call_zone_operation(config, options["operation"], params)
            case "async":
                ref = zone_client.start_zone_operation(config, options["operation"], params)
                return {"job_reference": ref}
            case "result":
                return zone_client.read_zone_operation(config, options["job_reference"])
            case "admin":
                return zone_client.call_zone_admin(config, options["action"], params, upload=upload)
