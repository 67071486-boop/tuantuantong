import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context, tenant_context

from apps.business import jidanpu_openapi
from apps.business.models import JidanpuMemberBinding
from apps.callback import store, tenant_wsgi
from apps.platform.db_provisioning import tenant_database_name, tenant_role_name
from apps.platform.provisioning import provision_tenant


class DistributedVendorCallbackTests(TenantTestCase):
    CODE = "Ab3_-callback_codes_XY"
    HOST = "external.test.local"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with schema_context("public"):
            cls.other, _domain = provision_tenant(
                schema_name="callback_other", name="合成对照", domain="callback-other.test.local",
            )
        cls.addClassCleanup(cls._delete_other)

    @classmethod
    def _delete_other(cls):
        with schema_context("public"):
            cls.other.delete(force_drop=True)

    def create_binding(self, account, secret):
        return JidanpuMemberBinding.objects.create(
            user=get_user_model().objects.create_user(username=account),
            account=account,
            jidanpu_base_url="https://vendor.test.local",
            mode=JidanpuMemberBinding.Mode.OPENAPI,
            client_secret=secret,
            callback_code=self.CODE,
        )

    def setUp(self):
        super().setUp()
        self.binding = self.create_binding("local-vendor", "test-local-vendor-secret")
        with tenant_context(self.other):
            self.foreign = self.create_binding("foreign-vendor", "test-foreign-vendor-secret")
        directory = self.enterContext(TemporaryDirectory())
        self.events = Path(directory) / "events.jsonl"
        self.keys = Path(directory) / "keys.env"
        self.enterContext(mock.patch.dict(os.environ, {
            "CALLBACK_TENANT_SCHEMA": self.tenant.schema_name,
            "CALLBACK_EVENTS_PATH": str(self.events),
            "CALLBACK_KEYS_FILE": str(self.keys),
            "CALLBACK_PUBLIC_HOST": self.HOST,
        }))
        self.role = tenant_role_name(self.tenant.schema_name)
        self.enterContext(mock.patch.dict(settings.DATABASES["default"], {
            "NAME": tenant_database_name(self.tenant.schema_name), "USER": self.role,
        }))
        self.enterContext(override_settings(POSTGRES_ADMIN_USER=self.role))
        self.enterContext(mock.patch.object(tenant_wsgi, "close_old_connections"))

    def post(self, secret, *, code=None, host=HOST):
        params = {"action": "10", "orderNo": "PROOF-ONLY-20261121"}
        signed = f"action=10&clientSecret={secret}&orderNo=PROOF-ONLY-20261121"
        params["sign"] = hashlib.md5(signed.encode()).hexdigest()
        body = json.dumps(params).encode()
        response = {}

        def start_response(status, headers):
            response["status"] = status

        response["body"] = b"".join(tenant_wsgi.application({
            "REQUEST_METHOD": "POST", "PATH_INFO": f"/api/jidanpu/callback/{code or self.CODE}",
            "HTTP_HOST": host, "HTTP_X_TENANT": self.other.schema_name,
            "REMOTE_ADDR": "127.0.0.9", "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": "application/json", "wsgi.input": io.BytesIO(body),
        }, start_response))
        return response

    def test_worker_accepts_only_the_bound_workspace_even_if_code_is_shared(self):
        self.assertEqual(self.post(self.binding.client_secret, host="foreign.example.test")["status"], "404 Not Found")
        self.assertEqual(self.post(self.foreign.client_secret)["status"], "401 Unauthorized")
        self.assertEqual(self.post(self.binding.client_secret), {"status": "200 OK", "body": b"{}"})
        records = [json.loads(line) for line in self.events.read_text().splitlines()]
        self.assertEqual([record["body_stored"] for record in records], [False, True])
        self.assertEqual(connection.schema_name, self.tenant.schema_name)

    def test_worker_reads_rotated_key_and_refuses_old_key_without_a_file_sync(self):
        old_secret = self.binding.client_secret
        self.binding.client_secret = "test-rotated-vendor-secret"
        self.binding.save(update_fields=["client_secret", "updated_at"])
        self.assertEqual(self.post(old_secret)["status"], "401 Unauthorized")
        self.assertEqual(self.post(self.binding.client_secret)["status"], "200 OK")
        self.assertFalse(self.keys.exists())

    def test_worker_requires_the_dedicated_database_before_accepting_requests(self):
        with mock.patch.dict(settings.DATABASES["default"], {"USER": "wrong-workspace"}):
            with self.assertRaises(ImproperlyConfigured):
                self.post(self.binding.client_secret)
        self.assertFalse(self.events.exists())

    def test_worker_never_acknowledges_a_callback_if_sync_to_disk_fails(self):
        with mock.patch.object(store.os, "fsync", side_effect=OSError("synthetic fsync failure")):
            with self.assertRaises(OSError):
                self.post(self.binding.client_secret)

    def test_only_registered_workspace_uses_external_callback_address_and_db_key(self):
        with override_settings(JIDANPU_CALLBACK_HOSTS={self.tenant.schema_name: "external.test.local"}):
            self.assertEqual(
                jidanpu_openapi.callback_url(self.binding),
                f"https://external.test.local/api/jidanpu/callback/{self.CODE}",
            )
            self.assertIsNone(jidanpu_openapi.ensure_callback_key(self.binding))
            self.assertFalse(self.keys.exists())
            with tenant_context(self.other):
                self.assertEqual(
                    jidanpu_openapi.callback_url(self.foreign),
                    f"https://callback-other.test.local/api/jidanpu/callback/{self.CODE}",
                )
                self.assertTrue(jidanpu_openapi.ensure_callback_key(self.foreign).changed)
