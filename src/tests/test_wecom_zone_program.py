import importlib.util
import io
import json
import os
import tempfile
import time
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase


EVENT_CASES = (
    ("chat_archive_audit_approved_single", "chat_archive_audit_approved",
     {"userid": "synthetic-staff", "external_userid": "synthetic-customer"}),
    ("chat_archive_audit_approved_room", "chat_archive_audit_approved",
     {"chatid": "synthetic-group", "external_userid": "synthetic-customer"}),
    ("hit_keyword", "hit_keyword", {"token": "synthetic-keyword-token"}),
    ("auth_knowledge_base", "auth_knowledge_base",
     {"knowledge_base_id": "synthetic-kb", "knowledge_base_name": "Synthetic FAQ"}),
    ("unauth_knowledge_base", "unauth_knowledge_base",
     {"knowledge_base_id": "synthetic-kb", "knowledge_base_name": "Synthetic FAQ"}),
    ("delete_knowledge_base", "delete_knowledge_base",
     {"knowledge_base_id": "synthetic-kb", "knowledge_base_name": "Synthetic FAQ"}),
    ("knowledge_base_learn_done", "knowledge_base_learn_done",
     {"knowledge_base_id": "synthetic-kb", "knowledge_base_name": "Synthetic FAQ", "doc_id": 123, "learn_status": 0}),
    ("chat_archive_export_finished", "chat_archive_export_finished", {"jobid": "synthetic-export-job"}),
)


def req(body, *, call_type=1, ability="tt_zone_v1", notify_id="", is_async=False, corp="synthetic-corp", agent=17):
    request = mock.Mock()
    request.get_data.return_value = json.dumps(body)
    request.get_call_type.return_value = call_type
    request.get_ability_id.return_value = ability
    request.get_notify_id.return_value = notify_id
    request.get_is_async.return_value = is_async
    request.get_job_info.return_value = "synthetic-job-context" if is_async else ""
    request.get_corpid.return_value = corp
    request.get_agentid.return_value = agent
    request.get_req_id.return_value = "synthetic-request"
    return request


def app_body(op, params):
    return {"input": {"payload": json.dumps({"op": op, "params": params})}}


def unwrap(data):
    return json.loads(json.loads(data)["payload"])


class WecomZoneProgramTests(SimpleTestCase):
    def setUp(self):
        module_path = Path(__file__).resolve().parents[2] / "deploy" / "wecom-archive" / "zone_program.py"
        spec = importlib.util.spec_from_file_location("zone_program_under_test", module_path)
        self.program = importlib.util.module_from_spec(spec)
        self.vendor = mock.MagicMock()
        modules = mock.patch.dict("sys.modules", {"wwspecapisdk": self.vendor, "zone_operations": mock.MagicMock()})
        modules.start()
        self.addCleanup(modules.stop)
        spec.loader.exec_module(self.program)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.program.TOKEN_DIR = Path(temporary.name) / "legacy"
        self.program.EVENT_DIR = Path(temporary.name) / "events"
        self.sdk = self.vendor.ChatDataSDK.return_value
        self.sdk.spec_notify_app.return_value = '{"errcode":0}'
        self.sdk.program_async_job_call_back.return_value = '{"errcode":0}'
        self.vendor.spec_gen_notify_id.return_value = "synthetic-notify"
        self.vendor.spec_env.get_is_debug_mode.return_value = False

    def make_handler(self, request, length=16):
        handler = self.program.ArchiveHandler.__new__(self.program.ArchiveHandler)
        handler.path = "/"
        handler.headers = {"Content-Length": str(length)}
        handler.rfile = io.BytesIO(b"a" * length)
        handler.wfile = io.BytesIO()
        handler.reply = mock.Mock()
        handler.send_error = mock.Mock()
        self.vendor.WWToSpecHTTPReqDecoder.return_value = request
        return handler

    def test_sync_event_notifications_are_identity_bound(self):
        for index, (event_type, event_key, values) in enumerate(EVENT_CASES):
            with self.subTest(event_type=event_type):
                identifier = f"synthetic-notify-{index}"
                self.vendor.spec_gen_notify_id.return_value = identifier
                incoming = {"event_type": event_type, "timestamp": 1408091189,
                            event_key: {**values, "unused_private_content": "discard"}}
                expected = {"event_type": event_type, "timestamp": 1408091189, event_key: values}
                self.assertEqual(self.program.handle_request(req(incoming, call_type=2)), "")
                self.assertEqual(json.loads(self.sdk.spec_notify_app.call_args.args[0]), {
                    "notify_id": identifier, "notify_scene": 94,
                })
                body = app_body("event.get", {})
                self.assertEqual(unwrap(self.program.handle_request(req(body, notify_id=identifier))), {
                    "errcode": 0, "event": expected,
                })
                with self.assertRaises((ValueError, FileNotFoundError)):
                    self.program.handle_request(req(body, notify_id=identifier, agent=18))
                with self.assertRaises((ValueError, FileNotFoundError)):
                    self.program.handle_request(req(body, notify_id=identifier, corp="another-corp"))

    def test_legacy_new_message_notification_keeps_scene_and_response(self):
        incoming = {"event_type": "conversation_new_message", "timestamp": 1408091189,
                    "conversation_new_message": {"token": "synthetic-chat-token"}}
        self.assertEqual(self.program.handle_request(req(incoming, call_type=2, ability="")), "")
        self.assertEqual(self.sdk.spec_notify_app.call_count, 1)
        self.assertEqual(json.loads(self.sdk.spec_notify_app.call_args.args[0]), {
            "notify_id": "synthetic-notify", "notify_scene": 93,
        })
        body = {"input": {"payload": "{}"}}
        legacy = req(body, ability="tt_archive_token", notify_id="synthetic-notify")
        self.assertEqual(unwrap(self.program.handle_request(legacy)), {"errcode": 0, "token": "synthetic-chat-token"})
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.program.handle_request(req(app_body("event.get", {}), notify_id="synthetic-notify"))

    def test_event_file_is_private_and_expired_event_is_unavailable(self):
        incoming = {"event_type": "hit_keyword", "timestamp": 1408091189,
                    "hit_keyword": {"token": "synthetic-keyword-token"}}
        self.program.handle_request(req(incoming, call_type=2))
        [file] = list(self.program.EVENT_DIR.glob("*.json"))
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        expired = time.time() - 601
        os.utime(file, (expired, expired))
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.program.handle_request(req(app_body("event.get", {}), notify_id="synthetic-notify"))

    def test_async_ack_precedes_execution_and_reports_wrapped_result(self):
        params = {"rule_id": "synthetic-rule"}
        handler = self.make_handler(req(app_body("get_rule_detail", params), is_async=True))
        from zone_operations import run_operation

        calls = mock.Mock()
        calls.attach_mock(handler.reply, "ack")
        calls.attach_mock(run_operation, "execute")
        calls.attach_mock(self.sdk.program_async_job_call_back, "report")
        outcome = {"errcode": 0, "name": "商家退款", "keyword": {"word_list": ["原路退回"]}}
        run_operation.return_value = outcome

        handler.do_POST()

        self.assertEqual([item[0] for item in calls.mock_calls], ["ack", "execute", "report"])
        handler.reply.assert_called_once_with("", verified=True)
        run_operation.assert_called_once_with(self.sdk, "get_rule_detail", params, debug=False)
        self.vendor.ChatDataSDK.assert_called_once_with("synthetic-corp", 17, "tt_zone_v1", "synthetic-job-context")
        report_body = self.sdk.program_async_job_call_back.call_args.args[0]
        self.assertTrue(report_body.isascii())
        reported = json.loads(report_body)
        self.assertEqual(reported["errcode"], 0)
        self.assertEqual(unwrap(reported["result"]), outcome)

    def test_v1_accepts_documented_large_batch_and_legacy_limit_stays(self):
        from zone_operations import run_operation

        run_operation.return_value = {"errcode": 0, "jobid": "synthetic-summary"}
        message_list = [{"msgid": f"synthetic-{i}", "encrypt_info": {"secret_key": "a" * 80}} for i in range(1000)]
        params = {"msg_list": message_list}
        request_body = app_body("create_summary_task", params)
        length = len(json.dumps(request_body).encode())
        handler = self.make_handler(req(request_body), length=length)
        handler.do_POST()
        handler.send_error.assert_not_called()
        self.assertEqual(unwrap(handler.reply.call_args.args[0]), {"errcode": 0, "jobid": "synthetic-summary"})
        run_operation.assert_called_once_with(self.sdk, "create_summary_task", params, debug=False)

        legacy = self.make_handler(req(request_body, ability="tt_archive_sync"), length=length)
        legacy.do_POST()
        legacy.send_error.assert_called_once_with(HTTPStatus.BAD_REQUEST)
        legacy.reply.assert_not_called()

    def test_async_event_pull_is_reported_as_failure_and_never_reads_file(self):
        handler = self.make_handler(req(app_body("event.get", {}), is_async=True, notify_id="synthetic-notify"))
        handler.do_POST()
        handler.reply.assert_called_once_with("", verified=True)
        reported = json.loads(self.sdk.program_async_job_call_back.call_args.args[0])
        self.assertEqual(reported["errcode"], -1)
        self.assertEqual(unwrap(reported["result"]), {"errcode": -1})

    def test_event_push_failure_requests_platform_retry_and_keeps_original_receipt(self):
        incoming = {"event_type": "hit_keyword", "timestamp": 1408091189,
                    "hit_keyword": {"token": "synthetic-keyword-token"}}
        self.sdk.spec_notify_app.return_value = '{"errcode":790016}'
        handler = self.make_handler(req(incoming, call_type=2))
        handler.do_POST()
        handler.reply.assert_called_once_with("", verified=True, retry="1")
        self.assertEqual(len(list(self.program.EVENT_DIR.glob("*.json"))), 1)

    def test_event_timeout_preserves_delivered_receipt_and_reuses_sdk_id_on_replay(self):
        incoming = {"event_type": "hit_keyword", "timestamp": 1408091189,
                    "hit_keyword": {"token": "synthetic-keyword-token"}}
        self.sdk.spec_notify_app.side_effect = [TimeoutError(), '{"errcode":0}']
        self.vendor.spec_gen_notify_id.side_effect = ["synthetic-first-id", "synthetic-unused-id"]
        handler = self.make_handler(req(incoming, call_type=2))
        handler.do_POST()
        handler.reply.assert_called_once_with("", verified=True, retry="1")
        self.assertEqual(unwrap(self.program.handle_request(req(app_body("event.get", {}), notify_id="synthetic-first-id"))),
                         {"errcode": 0, "event": incoming})

        for _ in range(2):
            self.assertEqual(self.program.handle_request(req(incoming, call_type=2)), "")
        self.vendor.spec_gen_notify_id.assert_called_once_with()
        self.assertEqual([json.loads(call.args[0]) for call in self.sdk.spec_notify_app.call_args_list], [
            {"notify_id": "synthetic-first-id", "notify_scene": 94},
            {"notify_id": "synthetic-first-id", "notify_scene": 94},
        ])

    def test_async_upstream_failure_stays_in_payload_of_reported_task(self):
        from zone_operations import run_operation

        run_operation.return_value = {"errcode": 790021}
        handler = self.make_handler(req(app_body("get_summary_result", {"jobid": "summary-id"}), is_async=True))
        handler.do_POST()
        handler.reply.assert_called_once_with("", verified=True)
        reported = json.loads(self.sdk.program_async_job_call_back.call_args.args[0])
        self.assertEqual(reported["errcode"], 0)
        self.assertEqual(unwrap(reported["result"]), {"errcode": 790021})

    def test_async_reporting_retries_the_same_result_without_executing_again(self):
        from zone_operations import run_operation

        run_operation.return_value = {"errcode": 0, "jobid": "synthetic-model-job"}
        self.sdk.program_async_job_call_back.side_effect = [TimeoutError(), '{"errcode":-1}', '{"errcode":0}']
        params = {"msg_list": [{"msgid": "synthetic-msg", "encrypt_info": {"secret_key": "synthetic-key"}}]}
        handler = self.make_handler(req(app_body("create_summary_task", params), is_async=True))
        with mock.patch.object(self.program.time, "sleep") as sleep:
            handler.do_POST()
        handler.reply.assert_called_once_with("", verified=True)
        self.vendor.ChatDataSDK.assert_called_once_with("synthetic-corp", 17, "tt_zone_v1", "synthetic-job-context")
        run_operation.assert_called_once()
        reports = self.sdk.program_async_job_call_back.call_args_list
        self.assertEqual(len(reports), 3)
        self.assertEqual(reports, [reports[0]] * 3)
        self.assertEqual(sleep.call_count, 2)
        self.vendor.spec_log_error.assert_not_called()

    def test_async_report_exhaustion_logs_only_a_generic_failure(self):
        from zone_operations import run_operation

        run_operation.return_value = {"errcode": 0, "response_data": "synthetic-private-result"}
        self.sdk.program_async_job_call_back.side_effect = TimeoutError("synthetic-private-details")
        handler = self.make_handler(req(app_body("get_summary_result", {"jobid": "summary-id"}), is_async=True))
        with mock.patch.object(self.program.time, "sleep"):
            handler.do_POST()
        handler.reply.assert_called_once_with("", verified=True)
        self.assertEqual(self.sdk.program_async_job_call_back.call_count, 3)
        run_operation.assert_called_once()
        self.vendor.spec_log_error.assert_called_once_with("zone async result report failed")

    def test_debug_reads_protected_credentials_and_binds_loopback(self):
        credentials = self.program.EVENT_DIR.parent / "debug-credentials.json"
        credentials.write_text('{"debug_token":"synthetic-debug","access_token":"synthetic-debug-access"}')
        credentials.chmod(0o600)
        self.vendor.spec_env.get_is_debug_mode.return_value = True
        with mock.patch.object(self.program, "ArchiveServer") as server:
            self.program.main(["--debug-config", str(credentials)])
        self.vendor.spec_open_debug_mode.assert_called_once_with("synthetic-debug", "synthetic-debug-access")
        server.assert_called_once_with(("127.0.0.1", 8080), self.program.ArchiveHandler)
        server.return_value.__enter__.return_value.serve_forever.assert_called_once_with()

    def test_debug_rejects_unprotected_or_symbolic_credentials(self):
        credentials = self.program.EVENT_DIR.parent / "debug-credentials.json"
        credentials.write_text('{"debug_token":"synthetic-debug","access_token":"synthetic-debug-access"}')
        credentials.chmod(0o640)
        with self.assertRaises(ValueError):
            self.program.enable_debug(credentials)
        credentials.chmod(0o600)
        link = credentials.with_suffix(".symlink")
        link.symlink_to(credentials)
        with self.assertRaises(ValueError):
            self.program.enable_debug(link)
        self.vendor.spec_open_debug_mode.assert_not_called()

    def test_default_startup_does_not_enable_debug(self):
        with mock.patch.object(self.program, "ArchiveServer") as server:
            self.program.main([])
        self.vendor.spec_open_debug_mode.assert_not_called()
        server.assert_called_once_with(("0.0.0.0", 8080), self.program.ArchiveHandler)
