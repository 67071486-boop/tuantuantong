"""鸡蛋铺独立接收器。进程只读取配置指定的工作室库。"""

import os
from functools import partial

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, close_old_connections
from django_tenants.utils import get_public_schema_name, schema_context, tenant_context

from apps.business.models import JidanpuMemberBinding
from apps.callback import config, wsgi
from apps.platform.db_provisioning import tenant_database_name, tenant_role_name
from apps.platform.models import Tenant


def tenant_scope():
    schema = os.environ.get("CALLBACK_TENANT_SCHEMA", "")
    database = settings.DATABASES["default"]
    if (not schema or database["NAME"] != tenant_database_name(schema)
            or database["USER"] != tenant_role_name(schema)
            or settings.POSTGRES_ADMIN_USER != database["USER"]):
        raise ImproperlyConfigured("回调接收器需要对应工作室的专库账号")
    return schema


def official_secret(code, *, schema):
    close_old_connections()
    try:
        with schema_context(get_public_schema_name()):
            tenant = Tenant.objects.get(schema_name=schema, is_active=True)
        with tenant_context(tenant):
            value = (
                JidanpuMemberBinding.objects.filter(mode=JidanpuMemberBinding.Mode.OPENAPI, callback_code=code)
                .exclude(client_secret="")
                .order_by("-updated_at", "-pk")
                .values_list("client_secret", flat=True)
                .first()
            )
    except Tenant.DoesNotExist as exc:
        raise config.SecretUnavailable("回调工作室不可用") from exc
    except DatabaseError as exc:
        raise config.SecretUnavailable("回调密钥查询失败") from exc
    finally:
        close_old_connections()
    if not value or not value.strip():
        raise config.CallbackCodeUnknown("未登记回调码")
    return value.strip()


def application(environ, start_response):
    host = os.environ.get("CALLBACK_PUBLIC_HOST", "")
    if not host:
        raise ImproperlyConfigured("独立接收器缺少工作室回调域名")
    if environ.get("HTTP_HOST") != host:
        start_response("404 Not Found", [("Content-Length", "0")])
        return [b""]
    lookup = partial(official_secret, schema=tenant_scope())
    return wsgi.application(environ, start_response, official_secret_lookup=lookup, durable=True)
