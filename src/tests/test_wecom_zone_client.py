import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.core.management import CommandError, call_command
from django.db import connection
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context
from wechatpy.exceptions import WeChatClientException

from apps.platform.provisioning import provision_tenant
from apps.wecom import archive, zone_client
from apps.wecom.models import WecomAppConfig


def envelope(payload):
    return {"errcode": 0, "response_data": json.dumps({"output": {"payload": json.dumps(payload)}})}


class WecomZoneClientTests(TenantTestCase):
    def setUp(self):
        self.enterContext(override_settings(
            WECOM_ARCHIVE_TENANT_SCHEMA=connection.schema_name,
            WECOM_ZONE_ENABLED=True, WECOM_ARCHIVE_PROGRAM_ID="synthetic-program",
        ))
        self.config = WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name, enabled=True, corp_id="synthetic-zone-corp",
            agent_id=2, secret="synthetic-zone-secret", identity_revision=4,
        )
        self.post = self.enterContext(mock.patch.object(archive, "_archive_post"))

    def test_group_info_call_uses_own_program_and_validates_output(self):
        self.post.return_value = envelope({"errcode": 0, "group_chat": {"chat_id": "example-chat"}})

        self.assertEqual(
            zone_client.call_zone_operation(self.config, "get_group_chat", {"chatid": "example-chat"}),
            {"errcode": 0, "group_chat": {"chat_id": "example-chat"}},
        )
        args, kwargs = self.post.call_args
        self.assertEqual((args[0].pk, args[1]), (self.config.pk, "chatdata/sync_call_program"))
        self.assertEqual(args[2]["program_id"], "synthetic-program")
        self.assertEqual(args[2]["ability_id"], "tt_zone_v1")
        self.assertEqual(json.loads(json.loads(args[2]["request_data"])["input"]["payload"]), {
            "op": "get_group_chat", "params": {"chatid": "example-chat"},
        })
        self.assertEqual(kwargs, {"deadline": None})

    def test_another_tenant_cannot_use_the_same_application_and_program(self):
        other, _ = provision_tenant(
            schema_name="wecom_zone_other", name="合成租户乙", domain="wecom-zone-other.test.com",
        )
        with tenant_context(other):
            config_b = WecomAppConfig.objects.create(
                tenant_schema=connection.schema_name, enabled=True, corp_id="synthetic-other-corp",
                agent_id=2, secret="synthetic-other-secret", identity_revision=4,
            )
            with self.assertRaises(zone_client.ZoneError):
                zone_client.call_zone_operation(self.config, "get_group_chat", {"chatid": "example-chat"})
        with self.assertRaises(zone_client.ZoneError):
            zone_client.call_zone_operation(config_b, "get_group_chat", {"chatid": "example-chat"})
        self.post.assert_not_called()

    @override_settings(WECOM_ZONE_ENABLED=False)
    def test_new_switch_is_independent_from_live_archive(self):
        self.assertEqual(archive.archive_program(), "synthetic-program")
        self.assertFalse(zone_client.zone_enabled())
        with self.assertRaises(zone_client.ZoneError):
            zone_client.call_zone_operation(self.config, "get_group_chat", {"chatid": "example-chat"})
        self.post.assert_not_called()

    def test_event_get_uses_notification_transport(self):
        event = {"event_type": "hit_keyword", "timestamp": 10, "hit_keyword": {"token": "temp"}}
        self.post.return_value = envelope({"errcode": 0, "event": event})
        self.assertEqual(zone_client.read_zone_event(self.config, "synthetic-notify-1"), event)
        sent = self.post.call_args.args[2]
        self.assertEqual(sent["notify_id"], "synthetic-notify-1")
        self.assertEqual(json.loads(json.loads(sent["request_data"])["input"]["payload"]), {
            "op": "event.get", "params": {},
        })

    def test_model_task_reference_binds_async_result_to_same_program_and_revision(self):
        self.post.side_effect = [
            {"errcode": 0, "jobid": "wecom-synthetic-job"},
            {**envelope({"errcode": 0, "jobid": "model-synthetic-job"}), "response_errcode": 0},
        ]
        ref = zone_client.start_zone_operation(self.config, "create_summary_task", {"msg_list": []})
        self.assertNotEqual(ref, "wecom-synthetic-job")
        self.assertEqual(self.post.call_args.args[1], "chatdata/async_program_task")
        self.assertEqual(
            zone_client.read_zone_operation(self.config, ref)["jobid"], "model-synthetic-job",
        )
        self.assertEqual(self.post.call_args.args[1:3], (
            "chatdata/async_program_result", {"jobid": "wecom-synthetic-job"},
        ))
        self.post.reset_mock()
        with override_settings(WECOM_ARCHIVE_PROGRAM_ID="other-program"):
            with self.assertRaises(zone_client.ZoneError):
                zone_client.read_zone_operation(self.config, ref)
        self.config.identity_revision += 1
        self.config.save(update_fields=["identity_revision"])
        with self.assertRaises(zone_client.ZoneError):
            zone_client.read_zone_operation(self.config, ref)
        self.post.assert_not_called()

    def test_async_job_runtime_failure_uses_inner_errcode_and_hides_vendor_detail(self):
        self.post.side_effect = [
            {"errcode": 0, "jobid": "wecom-synthetic-job"},
            {**envelope({"errcode": 0}), "response_errcode": 710012, "errmsg": "private detail"},
        ]
        ref = zone_client.start_zone_operation(self.config, "get_group_chat", {"chatid": "example"})
        with self.assertRaisesRegex(zone_client.ZoneError, "710012") as error:
            zone_client.read_zone_operation(self.config, ref)
        self.assertNotIn("private detail", str(error.exception))

    def test_invalid_and_disallowed_requests_never_reach_the_transport(self):
        actions = [
            lambda: zone_client.call_zone_operation(self.config, "common_send_http_post", {}),
            lambda: zone_client.call_zone_operation(self.config, "search_msg", []),
            lambda: zone_client.read_zone_event(self.config, "../../notify"),
            lambda: zone_client.call_zone_admin(self.config, "set_public_key", {}),
            lambda: zone_client.call_zone_admin(self.config, "set_receive_callback", {"program_id": "elsewhere"}),
            lambda: zone_client.call_zone_admin(self.config, "set_masking", {"userid": "sample", "config": {"hide_mobile": 1}}),
            lambda: zone_client.call_zone_admin(self.config, "conversation_members", {"limit": True}),
        ]
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaises(zone_client.ZoneError):
                    action()
        self.post.assert_not_called()

    def test_stale_config_identity_is_rejected_before_requests(self):
        WecomAppConfig.objects.filter(pk=self.config.pk).update(secret="new-synthetic-secret")
        with self.assertRaises(zone_client.ZoneError):
            zone_client.call_zone_admin(self.config, "check_debug_mode")
        self.post.assert_not_called()

    def test_admin_json_endpoint_shapes_and_error_codes(self):
        for action, params, path, body in (
            ("conversation_members", {"cursor": "example", "limit": 15}, "chatdata/get_auth_user_list",
             {"cursor": "example", "limit": 15}),
            ("document_members", {}, "docdata/get_auth_user_list", {}),
            ("get_masking", {"userid": "example-employee"}, "chatdata/get_hide_sensitiveinfo_config",
             {"userid": "example-employee"}),
            ("set_masking", {"userid": "example-employee", "config": {"hide_mobile": True}},
             "chatdata/set_hide_sensitiveinfo_config", {"userid": "example-employee", "config": {"hide_mobile": True}}),
            ("set_receive_callback", {}, "chatdata/set_receive_callback", {"program_id": "synthetic-program"}),
            ("get_log_level", {}, "chatdata/get_log_level", {"program_id": "synthetic-program"}),
            ("set_log_level", {"log_level": 1}, "chatdata/set_log_level",
             {"program_id": "synthetic-program", "log_level": 1}),
            ("check_debug_mode", {}, "chatdata/check_debug_mode", {"program_id": "synthetic-program"}),
            ("open_debug_mode", {"debug_token": "example-token"}, "chatdata/open_debug_mode",
             {"program_id": "synthetic-program", "debug_token": "example-token"}),
            ("close_debug_mode", {}, "chatdata/close_debug_mode", {"program_id": "synthetic-program"}),
        ):
            with self.subTest(action=action):
                self.post.return_value = {"errcode": 0}
                self.assertEqual(zone_client.call_zone_admin(self.config, action, params), {"errcode": 0})
                self.assertEqual(self.post.call_args.args[1:3], (path, body))
                self.post.return_value = {"errcode": 710123, "errmsg": "synthetic private vendor detail"}
                with self.assertRaisesRegex(zone_client.ZoneError, "710123") as error:
                    zone_client.call_zone_admin(self.config, action, params)
                self.assertNotIn("private vendor detail", str(error.exception))

    def test_admin_log_target_requires_program_or_explicitly_configured_model(self):
        with self.assertRaisesRegex(zone_client.ZoneError, "日志目标未登记"):
            zone_client.call_zone_admin(self.config, "get_log_level", {"program_id": "synthetic-own-model"})
        self.post.assert_not_called()
        with override_settings(WECOM_ZONE_LOG_TARGETS=["synthetic-own-model"]):
            self.post.return_value = {"errcode": 0, "log_level": 3}
            self.assertEqual(zone_client.call_zone_admin(
                self.config, "get_log_level", {"program_id": "synthetic-own-model"},
            )["log_level"], 3)
            self.assertEqual(self.post.call_args.args[1:3], (
                "chatdata/get_log_level", {"program_id": "synthetic-own-model"},
            ))
            self.post.reset_mock()
            with self.assertRaises(zone_client.ZoneError):
                zone_client.call_zone_admin(self.config, "check_debug_mode", {"program_id": "synthetic-own-model"})
            self.post.assert_not_called()

    def test_file_upload_has_documented_multipart_shape_and_retries_rejected_token(self):
        with mock.patch("apps.wecom.zone_client.sdk.get_client") as factory:
            client = factory.return_value
            client.post.side_effect = [
                WeChatClientException(errcode=40014, errmsg="synthetic expired token"),
                {"errcode": 0, "type": "file", "media_id": "synthetic-media"},
            ]
            result = zone_client.call_zone_admin(
                self.config, "upload_file", upload=("test-template.txt", b"example file"),
            )
        self.assertEqual(result["media_id"], "synthetic-media")
        client.fetch_access_token.assert_called_once_with()
        self.assertEqual(client.post.call_count, 2)
        args, kwargs = client.post.call_args
        self.assertEqual(args, ("chatdata/upload_media",))
        self.assertEqual(kwargs["params"], {"type": "file"})
        self.assertTrue(kwargs["headers"]["Content-Type"].startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="media"; filename="test-template.txt"; filelength=12', kwargs["data"])
        self.assertIn(b"Content-Type: application/octet-stream", kwargs["data"])
        self.assertIn(b"example file", kwargs["data"])

    def test_document_ticket_requires_the_expected_eligible_employee(self):
        with mock.patch("apps.wecom.zone_client.sdk.get_client") as factory:
            client = factory.return_value
            client.get.side_effect = [
                {"errcode": 0, "userid": "example-employee", "user_doc_ticket": "example-document-ticket"},
                {"errcode": 0, "userid": "another-employee", "user_doc_ticket": "another-ticket"},
                {"errcode": 0, "userid": "example-employee"},
            ]
            args = (self.config, "example-auth-code")
            actor = {"expected_userid": "example-employee"}
            self.assertEqual(zone_client.get_zone_document_ticket(*args, **actor), "example-document-ticket")
            client.get.assert_called_once_with("auth/getuserinfo", params={"code": "example-auth-code"})
            with self.assertRaisesRegex(zone_client.ZoneError, "当前成员不一致"):
                zone_client.get_zone_document_ticket(*args, **actor)
            with self.assertRaisesRegex(zone_client.ZoneError, "文件授权票据"):
                zone_client.get_zone_document_ticket(*args, **actor)

    def test_export_template_uses_linked_file_upload_and_supported_formats(self):
        with mock.patch("apps.wecom.zone_client.sdk.get_client") as factory:
            client = factory.return_value
            with self.assertRaisesRegex(zone_client.ZoneError, "Word、Excel 或 TXT"):
                zone_client.call_zone_admin(self.config, "upload_export_template",
                                            upload=("template.csv", b"example file"))
            factory.assert_not_called()
            client.post.return_value = {"errcode": 0, "media_id": "synthetic-template-media"}
            result = zone_client.call_zone_admin(self.config, "upload_export_template",
                                                 upload=("template.txt", b"example file"))
        self.assertEqual(result["media_id"], "synthetic-template-media")
        self.assertEqual(client.post.call_args.args, ("media/upload",))
        self.assertEqual(client.post.call_args.kwargs["params"], {"type": "file"})

    def test_cli_protects_the_result_file_and_hides_raw_console_output(self):
        self.post.return_value = envelope({"errcode": 0, "summary": "private summary"})
        stdout = StringIO()
        with TemporaryDirectory() as tmp:
            params = Path(tmp, "params.json")
            params.write_text('{"jobid":"synthetic-model-job"}', encoding="utf-8")
            output = Path(tmp, "result.json")
            call_command("wecom_zone", "sync", "get_summary_result", "--params-file", str(params),
                         "--result-file", str(output), stdout=stdout)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(output.read_text())["summary"], "private summary")
            self.assertNotIn("private summary", stdout.getvalue())
            self.post.reset_mock()
            with self.assertRaises(CommandError):
                call_command("wecom_zone", "sync", "get_summary_result", "--result-file", str(output), stdout=stdout)
            self.post.assert_not_called()

    def test_cli_reports_task_state_and_partial_failure_without_exporting_sensitive_data(self):
        self.post.return_value = envelope({
            "errcode": 0, "status": 2, "fail_list": [{"msgid": "synthetic-private-msg"}],
            "response_data": "synthetic-publication-blocked-summary",
        })
        stdout = StringIO()
        call_command("wecom_zone", "sync", "get_summary_result", stdout=stdout)
        summary = stdout.getvalue()
        self.assertIn("任务状态 2", summary)
        self.assertIn("失败条目 1 项", summary)
        self.assertNotIn("synthetic-private-msg", summary)
        self.assertNotIn("synthetic-publication-blocked-summary", summary)
