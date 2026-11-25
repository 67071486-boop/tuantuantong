from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase

from apps.wecom import archive
from apps.wecom.models import WecomAppConfig


class ArchiveRegistrationTests(TenantTestCase):
    @override_settings(WECOM_ARCHIVE_PROGRAM_ID="synthetic-program")
    def test_only_the_configured_workspace_can_be_registered(self):
        config = WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name, enabled=True, corp_id="synth-corp",
            agent_id=1, secret="synth-secret",
        )
        out = StringIO()
        with override_settings(WECOM_ARCHIVE_TENANT_SCHEMA=self.tenant.schema_name), patch.object(
            archive, "register_archive_callback",
        ) as register:
            call_command("register_wecom_archive_callback", stdout=out)
        register.assert_called_once_with(config, "synthetic-program")
        self.assertIn("企微已确认", out.getvalue())
        self.assertEqual(connection.schema_name, self.tenant.schema_name)

    @override_settings(WECOM_ARCHIVE_PROGRAM_ID="synthetic-program")
    def test_missing_workspace_or_disabled_app_makes_no_external_request(self):
        with patch.object(archive, "register_archive_callback") as register:
            with self.settings(WECOM_ARCHIVE_TENANT_SCHEMA="synth-unconfigured"), self.assertRaisesMessage(
                CommandError, "没有配置的企微存档工作室",
            ):
                call_command("register_wecom_archive_callback")
            with override_settings(WECOM_ARCHIVE_TENANT_SCHEMA=self.tenant.schema_name):
                with self.assertRaisesMessage(CommandError, "自建应用未启用"):
                    call_command("register_wecom_archive_callback")
        register.assert_not_called()

    @override_settings(WECOM_ARCHIVE_PROGRAM_ID="synthetic-program")
    def test_upstream_failure_is_not_reported_as_success(self):
        WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name, enabled=True, corp_id="synth-corp",
            agent_id=1, secret="synth-secret",
        )
        out = StringIO()
        with override_settings(WECOM_ARCHIVE_TENANT_SCHEMA=self.tenant.schema_name), patch.object(
            archive, "register_archive_callback", side_effect=archive.ArchiveError("企微拒绝登记"),
        ):
            with self.assertRaisesMessage(CommandError, "企微拒绝登记"):
                call_command("register_wecom_archive_callback", stdout=out)
        self.assertEqual(out.getvalue(), "")
