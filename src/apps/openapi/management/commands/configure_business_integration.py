import json
import os
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django_tenants.postgresql_backend.base import is_valid_schema_name
from django_tenants.utils import get_public_schema_name, schema_context, schema_exists
from rest_framework.exceptions import ValidationError

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.openapi.models import BUSINESS_INTEGRATION_ENABLED, ApiApp, ApiAppKind, ApiAppState
from apps.openapi.serializers import AppCreateSerializer, AppUpdateSerializer, generate_app_key, generate_app_secret


class Command(BaseCommand):
    help = "在指定工作室内配置业务 SDK 应用；凭据仅写入新建的 0600 文件"

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("create", "rotate", "enable", "disable", "callback"))
        parser.add_argument("--schema", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--owner")
        parser.add_argument("--callback-url")
        parser.add_argument("--output-file", type=Path)

    def handle(self, *args, **options):
        schema, name, action = (options[k] for k in ("schema", "name", "action"))
        if not is_valid_schema_name(schema) or schema == get_public_schema_name() or not schema_exists(schema):
            raise CommandError("请指定当前数据库中已存在的工作室 schema")
        file = options["output_file"]
        if (file is not None) != (action in {"create", "rotate"}):
            raise CommandError("仅 create 和 rotate 必须提供 --output-file")
        file_written = False
        try:
            with schema_context(schema), transaction.atomic():
                app = self.configure(action, name, options)
                if file is not None:
                    descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    file_written = True
                    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                        json.dump({"client_id": app.app_key, "client_secret": app.app_secret}, stream)
                        stream.write("\n")
        except ValidationError as exc:
            raise CommandError(str(exc.detail)) from None
        except OSError as exc:
            if file_written:
                file.unlink(missing_ok=True)
            raise CommandError(f"写入凭据文件失败：{exc.strerror}") from None
        except Exception:
            if file_written:
                file.unlink(missing_ok=True)
            raise
        self.stdout.write(f"{schema} 的业务应用 {name} 已完成 {action}")

    def configure(self, action, name, options):
        callback_url = options["callback_url"]
        if action == "create":
            owner = get_user_model().objects.filter(username=options["owner"], is_active=True).first()
            if owner is None:
                raise CommandError("请用 --owner 指定当前工作室的启用账号")
            data = {"name": name}
            if callback_url is not None:
                data["callback_url"] = callback_url
            form = AppCreateSerializer(data=data)
            form.is_valid(raise_exception=True)
            if ApiApp.objects.filter(name=form.validated_data["name"]).exists():
                raise CommandError("应用名已存在")
            app = ApiApp.objects.create(
                kind=ApiAppKind.BUSINESS_INTEGRATION, state=BUSINESS_INTEGRATION_ENABLED, created_by=owner,
                app_key=generate_app_key(), app_secret=generate_app_secret(), **form.validated_data,
            )
            event = "system.open_api_app_create"
        else:
            app = ApiApp.objects.select_for_update().filter(name=name, kind=ApiAppKind.BUSINESS_INTEGRATION).first()
            if app is None:
                raise CommandError("该业务应用不存在")
            event = "system.open_api_app_update"
            if action == "rotate":
                app.app_secret = generate_app_secret()
                event = "system.open_api_app_rotate_secret"
                app.secret_rotated_at = timezone.now()
                changed = ["app_secret", "secret_rotated_at"]
            elif action == "callback":
                if callback_url is None:
                    raise CommandError("回调配置必须提供 --callback-url，传空串可关闭")
                form = AppUpdateSerializer(data={"callback_url": callback_url})
                form.is_valid(raise_exception=True)
                app.callback_url = form.validated_data["callback_url"]
                changed = ["callback_url"]
            else:
                app.state = BUSINESS_INTEGRATION_ENABLED if action == "enable" else ApiAppState.DISABLED
                changed = ["state"]
            app.save(update_fields=changed)
        record(
            log_type=ACTION_LOG_TYPES[event], action=event, actor="管理命令：业务 SDK", system=True,
            target_kind="api_app", target_id=app.pk, target_name=app.name, reason=action,
        )
        return app
