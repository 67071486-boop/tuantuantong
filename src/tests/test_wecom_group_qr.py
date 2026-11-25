from datetime import timedelta
from unittest import mock

from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from wechatpy.exceptions import WeChatClientException

from apps.business.models import Game, Order
from apps.wecom.group_qr import _verify_group, cleanup_due, get_or_create_qr
from apps.wecom.group_sync import queue_event, run_due
from apps.wecom.models import WecomAppConfig, WecomGroupChat, WecomGroupQr
from tests.p5_auth_helpers import make_logged_in_client


class GroupQrTests(TenantTestCase):
    def setUp(self):
        self.cfg = WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name, enabled=True, corp_id="synth-corp", agent_id=123, secret="synth-secret"
        )
        self.group = WecomGroupChat.objects.create(chat_id="synth-chat", name="合成 123456", details={"owner": "staff"})
        verifier = mock.patch("apps.wecom.group_qr._verify_group", return_value=True)
        self.verify = verifier.start()
        self.addCleanup(verifier.stop)

    def call(self, responses):
        with mock.patch("apps.wecom.group_qr._call", side_effect=responses) as api:
            result = get_or_create_qr(self.group)
        return result, api

    def test_create_persists_id_before_read_and_reuses_it(self):
        def remote(config, endpoint, data):
            if endpoint == "add_join_way":
                self.assertEqual(data["auto_create_room"], 0)
                self.assertEqual(data["chat_id_list"], ["synth-chat"])
                return {"errcode": 0, "config_id": "synth-config"}
            self.assertEqual(endpoint, "get_join_way")
            self.assertEqual(WecomGroupQr.objects.get(group=self.group).config_id, "synth-config")
            self.group.refresh_from_db()
            self.assertEqual(self.group.details["config_id"], "synth-config")
            return {"errcode": 0, "join_way": {"qr_code": "https://example.invalid/qr"}}
        with mock.patch("apps.wecom.group_qr._call", side_effect=remote) as api:
            with self.assertNoLogs("apps.wecom.group_qr", level="WARNING"):
                self.assertEqual(get_or_create_qr(self.group)["status"], "ready")
                self.assertEqual(get_or_create_qr(self.group)["qr_url"], "https://example.invalid/qr")
        self.assertEqual([c.args[1] for c in api.call_args_list], ["add_join_way", "get_join_way", "get_join_way"])
        self.assertEqual(WecomGroupQr.objects.get().state, "ready")

    def test_get_failure_retains_id_and_next_attempt_uses_get(self):
        result, _ = self.call([{"errcode": 0, "config_id": "cfg"}, TimeoutError()])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(WecomGroupQr.objects.get().config_id, "cfg")
        result, api = self.call([{"errcode": 0, "join_way": {"qr_code": "https://example.invalid/qr"}}])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(api.call_args.args[1], "get_join_way")

    def test_unverified_41044_keeps_id_and_stops_automatic_creation(self):
        self.create_ready()
        with self.assertLogs("apps.wecom.group_qr", level="WARNING") as logs:
            result, api = self.call([WeChatClientException(41044, "corpsecret=synth-secret https://example.invalid/qr")])
        self.assertEqual(result["status"], "uncertain")
        self.assertIn("41044", str(logs.output))
        self.assertNotIn("synth-secret", str(logs.output))
        self.assertNotIn("https://example.invalid/qr", str(logs.output))
        self.assertEqual(api.call_count, 1)
        self.assertEqual(WecomGroupQr.objects.get().config_id, "cfg")
        self.group.refresh_from_db()
        self.assertEqual(self.group.details["config_id"], "cfg")
        result, api = self.call([])
        self.assertEqual(result["status"], "uncertain")
        api.assert_not_called()

    def test_renamed_group_does_not_create_for_old_order(self):
        self.group.name = "已经改名"
        self.group.save(update_fields=["name"])
        with mock.patch("apps.wecom.group_qr._call") as api:
            self.assertEqual(get_or_create_qr(self.group, expected_source="123456")["status"], "unavailable")
            api.assert_not_called()

    def test_failed_realtime_verification_never_creates(self):
        self.verify.return_value = False
        with mock.patch("apps.wecom.group_qr._call") as api:
            self.assertEqual(get_or_create_qr(self.group, expected_source="123456")["status"], "unavailable")
            api.assert_not_called()
        self.assertEqual(WecomGroupQr.objects.get().state, "idle")

    def test_realtime_verification_requires_same_chat_and_complete_order_number(self):
        claim = {"chat_id": "synth-chat"}
        with mock.patch("apps.wecom.group_qr._call") as api:
            api.return_value = {"errcode": 0, "group_chat": {"chat_id": "synth-chat", "name": "群 123456"}}
            self.assertTrue(_verify_group(self.cfg, claim, "123456"))
            api.return_value = {"errcode": 0, "group_chat": {"chat_id": "other", "name": "群 123456"}}
            self.assertFalse(_verify_group(self.cfg, claim, "123456"))
            api.return_value = {"errcode": 0, "group_chat": {"chat_id": "synth-chat", "name": "群 12345"}}
            self.assertFalse(_verify_group(self.cfg, claim, "123456"))
            api.return_value = {"errcode": 60011, "group_chat": {"chat_id": "synth-chat", "name": "群 123456"}}
            self.assertFalse(_verify_group(self.cfg, claim, "123456"))

    def test_existing_config_does_not_show_qr_if_remote_group_was_renamed(self):
        self.create_ready()
        self.verify.side_effect = _verify_group
        with mock.patch("apps.wecom.group_qr._call", return_value={"errcode": 0, "group_chat": {"chat_id": "synth-chat", "name": "新群名不含订单"}}) as api:
            result = get_or_create_qr(self.group, expected_source="123456")
        self.assertEqual((result["status"], result["qr_url"]), ("unavailable", ""))
        self.assertEqual([item.args[1] for item in api.call_args_list], ["get"])
        self.assertEqual(WecomGroupQr.objects.get().config_id, "cfg")

    def test_config_change_during_verification_prevents_create(self):
        def change(*args):
            WecomAppConfig.objects.update(identity_revision=1, corp_id="new-corp")
            return True
        self.verify.side_effect = change
        with mock.patch("apps.wecom.group_qr._call") as api:
            self.assertEqual(get_or_create_qr(self.group, expected_source="123456")["status"], "unavailable")
            api.assert_not_called()

    def test_config_change_during_create_saves_id_but_does_not_show_qr(self):
        def remote(config, endpoint, data):
            if endpoint == "add_join_way":
                WecomAppConfig.objects.update(identity_revision=1, corp_id="new-corp")
                return {"errcode": 0, "config_id": "old-corp-cfg"}
            return {"errcode": 0, "join_way": {"qr_code": "https://example.invalid/qr"}}
        with mock.patch("apps.wecom.group_qr._call", side_effect=remote):
            result = get_or_create_qr(self.group, expected_source="123456")
        self.assertEqual((result["status"], result["qr_url"]), ("unavailable", ""))
        self.assertEqual(WecomGroupQr.objects.get().config_id, "old-corp-cfg")

    def test_created_config_is_saved_but_not_shown_if_group_dismissed_during_creation(self):
        def remote(config, endpoint, data):
            if endpoint == "add_join_way":
                WecomGroupChat.objects.filter(pk=self.group.pk).update(dismissed=True)
                return {"errcode": 0, "config_id": "saved-cfg"}
            return {"errcode": 0, "join_way": {"qr_code": "https://example.invalid/qr"}}
        with mock.patch("apps.wecom.group_qr._call", side_effect=remote):
            result = get_or_create_qr(self.group, expected_source="123456")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["qr_url"], "")
        self.assertEqual(WecomGroupQr.objects.get().config_id, "saved-cfg")

    def test_source_changed_during_creation_preserves_id_without_returning_qr(self):
        current = iter([True, False])
        result = None
        with mock.patch("apps.wecom.group_qr._call", side_effect=[{"errcode": 0, "config_id": "saved-cfg"}, {"errcode": 0, "join_way": {"qr_code": "https://example.invalid/qr"}}]):
            result = get_or_create_qr(self.group, expected_source="123456", order_still_matches=lambda: next(current))
        self.assertEqual((result["status"], result["qr_url"]), ("unavailable", ""))
        self.assertEqual(WecomGroupQr.objects.get().config_id, "saved-cfg")

    def test_explicit_failure_envelope_never_creates_configuration(self):
        result, api = self.call([{"errcode": 60011, "errmsg": "denied"}])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(api.call_count, 1)
        self.assertEqual(WecomGroupQr.objects.get().state, "idle")

    def test_explicit_wecom_rejections_are_logged_without_credentials(self):
        for code in (60011, 60020):
            with self.subTest(code=code):
                with self.assertLogs("apps.wecom.group_qr", level="WARNING") as logs:
                    result, _ = self.call([WeChatClientException(code, "access_token=synth-token&corpsecret=synth-secret https://example.invalid/qr")])
                self.assertEqual(result["status"], "unavailable")
                message = str(logs.output)
                self.assertIn(str(code), message)
                self.assertNotIn("synth-token", message)
                self.assertNotIn("synth-secret", message)
                self.assertNotIn("https://example.invalid/qr", message)
                self.assertEqual(WecomGroupQr.objects.get().state, "idle")

    def test_failed_creation_never_logs_secrets_or_success(self):
        with self.assertLogs("apps.wecom.group_qr", level="INFO") as logs:
            result, _ = self.call([TimeoutError("access_token=synth-token&corpsecret=synth-secret https://example.invalid/qr")])
        self.assertEqual(result["status"], "uncertain")
        text = str(logs.output)
        self.assertNotIn("synth-token", text)
        self.assertNotIn("synth-secret", text)
        self.assertNotIn("https://example.invalid/qr", text)
        self.assertNotIn("创建并保存成功", text)

    def test_timeout_and_malformed_success_freeze_creation(self):
        for error in (TimeoutError("access_token=synth-token"), {"errcode": 0}, {"errcode": 500}, WeChatClientException(500, "server")):
            with self.subTest(error=type(error).__name__):
                WecomGroupQr.objects.all().delete()
                result, _ = self.call([error])
                self.assertEqual(result["status"], "uncertain")
                result, api = self.call([])
                self.assertEqual(result["status"], "uncertain")
                api.assert_not_called()

    def test_malformed_creation_response_emits_fixed_safe_warning(self):
        for response in ({"errcode": 0}, {"errcode": 0, "config_id": "synthetic-secret-" * 30}):
            with self.subTest(response_type="missing" if "config_id" not in response else "oversized"):
                WecomGroupQr.objects.all().delete()
                with self.assertLogs("apps.wecom.group_qr", level="WARNING") as logs:
                    result, _ = self.call([response])
                self.assertEqual(result["status"], "uncertain")
                self.assertEqual(len(logs.output), 1)
                self.assertIn("创建响应缺少可用编号", logs.output[0])
                self.assertNotIn("synthetic-secret", logs.output[0])
                self.assertEqual(WecomGroupQr.objects.get().state, "uncertain")

    def test_config_id_persistence_failure_logs_recovery_id_and_freezes(self):
        with mock.patch("apps.wecom.group_qr._finish_create", side_effect=RuntimeError("secret=not-for-log")):
            with self.assertLogs("apps.wecom.group_qr", level="ERROR") as logs:
                result, _ = self.call([{"errcode": 0, "config_id": "recover-config"}])
        self.assertEqual(result["status"], "uncertain")
        self.assertIn("recover-config", str(logs.output))
        self.assertNotIn("not-for-log", str(logs.output))
        self.assertEqual(WecomGroupQr.objects.get().state, "uncertain")
        result, api = self.call([])
        self.assertEqual(result["status"], "uncertain")
        api.assert_not_called()

    def test_concurrent_create_does_not_issue_second_write(self):
        def remote(config, endpoint, data):
            if endpoint == "add_join_way":
                self.assertEqual(get_or_create_qr(self.group)["status"], "pending")
                return {"errcode": 0, "config_id": "cfg"}
            return {"errcode": 0, "join_way": {"qr_code": "https://example.invalid/qr"}}
        with mock.patch("apps.wecom.group_qr._call", side_effect=remote) as api:
            self.assertEqual(get_or_create_qr(self.group)["status"], "ready")
        self.assertEqual(sum(c.args[1] == "add_join_way" for c in api.call_args_list), 1)

    def test_stale_creating_is_uncertain_but_late_success_is_recorded(self):
        def remote(config, endpoint, data):
            if endpoint == "add_join_way":
                WecomGroupQr.objects.update(operation_at=timezone.now() - timedelta(minutes=3))
                self.assertEqual(get_or_create_qr(self.group)["status"], "uncertain")
                return {"errcode": 0, "config_id": "late-cfg"}
            return {"errcode": 0, "join_way": {"qr_code": "https://example.invalid/qr"}}
        with mock.patch("apps.wecom.group_qr._call", side_effect=remote):
            self.assertEqual(get_or_create_qr(self.group)["status"], "ready")
        self.assertEqual(WecomGroupQr.objects.get().config_id, "late-cfg")

    def create_ready(self, age=timedelta(days=2)):
        self.group.details = {"owner": "staff", "config_id": "cfg"}
        self.group.save(update_fields=["details"])
        return WecomGroupQr.objects.create(group=self.group, corp_id=self.cfg.corp_id, agent_id=self.cfg.agent_id,
                                            config_id="cfg", state="ready", last_viewed_at=timezone.now() - age)

    def test_last_view_extends_lifetime_and_cleanup_removes_both(self):
        row = self.create_ready()
        with mock.patch("apps.wecom.group_qr._call", return_value={"errcode": 0, "join_way": {"qr_code": "https://example.invalid/qr"}}):
            self.assertEqual(get_or_create_qr(self.group)["status"], "ready")
        with mock.patch("apps.wecom.group_qr._call") as api:
            cleanup_due()
            api.assert_not_called()
        row.refresh_from_db()
        row.last_viewed_at = timezone.now() - timedelta(days=1, seconds=1)
        row.save(update_fields=["last_viewed_at"])
        with mock.patch("apps.wecom.group_qr._call", return_value={"errcode": 0}) as api:
            cleanup_due()
        self.assertEqual(api.call_args.args[1:], ("del_join_way", {"config_id": "cfg"}))
        row.refresh_from_db()
        self.group.refresh_from_db()
        self.assertEqual(row.config_id, "")
        self.assertNotIn("config_id", self.group.details)
        self.assertEqual(self.group.details["owner"], "staff")

    def test_failed_delete_keeps_both_copies_and_retries(self):
        row = self.create_ready()
        with mock.patch("apps.wecom.group_qr._call", return_value={"errcode": 500}):
            cleanup_due()
        row.refresh_from_db()
        self.group.refresh_from_db()
        self.assertEqual((row.config_id, row.state, self.group.details["config_id"]), ("cfg", "ready", "cfg"))
        with mock.patch("apps.wecom.group_qr._call", side_effect=WeChatClientException(41044, "corpsecret=synth-secret https://example.invalid/qr")):
            with self.assertLogs("apps.wecom.group_qr", level="WARNING") as logs:
                cleanup_due()
        self.assertIn("41044", str(logs.output))
        self.assertNotIn("synth-secret", str(logs.output))
        self.assertNotIn("https://example.invalid/qr", str(logs.output))
        row.refresh_from_db()
        self.assertEqual((row.config_id, row.state), ("cfg", "uncertain"))

    def test_cleanup_does_not_modify_new_identity_snapshot_after_switch(self):
        row = self.create_ready()
        def remote(config, endpoint, data):
            WecomAppConfig.objects.update(identity_revision=1, corp_id="new-corp", agent_id=2)
            WecomGroupChat.objects.filter(pk=self.group.pk).update(identity_revision=1, details={"config_id": "new-app"})
            return {"errcode": 0}
        with mock.patch("apps.wecom.group_qr._call", side_effect=remote):
            cleanup_due()
        row.refresh_from_db()
        self.group.refresh_from_db()
        self.assertEqual((row.config_id, row.state), ("cfg", "uncertain"))
        self.assertEqual(self.group.details["config_id"], "new-app")

    def test_old_identity_candidates_do_not_starve_current_cleanup(self):
        self.create_ready(age=timedelta(days=2))
        groups = [WecomGroupChat(chat_id=f"old-{i}", name="旧群", identity_revision=1) for i in range(101)]
        WecomGroupChat.objects.bulk_create(groups)
        WecomGroupQr.objects.bulk_create([
            WecomGroupQr(group=group, corp_id="old-corp", agent_id=2, config_id=f"old-config-{i}",
                         state="ready", last_viewed_at=timezone.now() - timedelta(days=3))
            for i, group in enumerate(groups)
        ])
        with mock.patch("apps.wecom.group_qr._call", return_value={"errcode": 0}) as api:
            cleanup_due()
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[2], {"config_id": "cfg"})
        self.assertEqual(WecomGroupQr.objects.get(group=self.group).config_id, "")

    def test_reading_prevents_cleanup_and_stale_cleanup_recovers_after_new_view(self):
        row = self.create_ready()
        row.state, row.operation_at = "reading", timezone.now()
        row.save(update_fields=["state", "operation_at"])
        with mock.patch("apps.wecom.group_qr._call") as api:
            cleanup_due()
            api.assert_not_called()
        row.state, row.operation_at = "deleting", timezone.now() - timedelta(minutes=3)
        row.last_viewed_at = timezone.now()
        row.save(update_fields=["state", "operation_at", "last_viewed_at"])
        with mock.patch("apps.wecom.group_qr._call", return_value={"errcode": 0}) as api:
            cleanup_due()
            self.assertEqual(api.call_args.args[1], "del_join_way")
        row.refresh_from_db()
        self.assertEqual(row.state, "idle")

    def test_view_during_remote_delete_waits_then_creates_new(self):
        row = self.create_ready()
        def deleting(config, endpoint, data):
            self.assertEqual(get_or_create_qr(self.group)["status"], "pending")
            return {"errcode": 0}
        with mock.patch("apps.wecom.group_qr._call", side_effect=deleting):
            cleanup_due()
        row.refresh_from_db()
        self.assertEqual((row.state, row.config_id), ("idle", ""))
        result, api = self.call([{"errcode": 0, "config_id": "new-cfg"}, {"errcode": 0, "join_way": {"qr_code": "https://example.invalid/new"}}])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(api.call_args_list[0].args[1], "add_join_way")
        self.assertEqual(WecomGroupQr.objects.get().config_id, "new-cfg")

    def test_group_refresh_preserves_config_copy(self):
        self.create_ready()
        queue_event({"MsgType": "event", "Event": "change_external_chat", "ChatId": "synth-chat", "ChangeType": "update", "CreateTime": "100"})
        WecomGroupChat.objects.update(retry_at=timezone.now() - timedelta(seconds=1))
        with mock.patch("apps.wecom.group_sync.sdk.get_contacts_client") as client:
            client.return_value.post.return_value = {"errcode": 0, "group_chat": {"name": "新的群", "owner": "new"}}
            run_due()
        self.group.refresh_from_db()
        self.assertEqual(self.group.details, {"name": "新的群", "owner": "new", "config_id": "cfg"})

    def test_new_identity_callback_discards_old_group_snapshot(self):
        self.create_ready()
        WecomAppConfig.objects.update(identity_revision=1, corp_id="new-corp", agent_id=2)
        queue_event({"MsgType": "event", "Event": "change_external_chat", "ChatId": "synth-chat", "ChangeType": "update", "CreateTime": "1"}, identity_revision=1)
        self.group.refresh_from_db()
        self.assertEqual((self.group.identity_revision, self.group.name, self.group.details), (1, "", {}))
        WecomGroupChat.objects.update(retry_at=timezone.now() - timedelta(seconds=1))
        with mock.patch("apps.wecom.group_sync.sdk.get_contacts_client") as client:
            client.return_value.post.return_value = {"errcode": 0, "group_chat": {"name": "新身份群 123456"}}
            run_due()
        self.group.refresh_from_db()
        self.assertNotIn("config_id", self.group.details)
        with mock.patch("apps.wecom.group_qr._call") as api:
            self.assertEqual(get_or_create_qr(self.group)["status"], "unavailable")
            api.assert_not_called()


class OrderGroupQrApiTests(TenantTestCase):
    def setUp(self):
        game = Game.objects.create(name="合成游戏")
        self.order = Order.objects.create(game=game, order_no="20261113000000000001", source_order_no="123456")
        WecomAppConfig.objects.create(tenant_schema=connection.schema_name, enabled=True, corp_id="corp", agent_id=1, secret="secret")
        self.client = make_logged_in_client(username="qr-reader", groups=("客服",), nodes=("order.view_detail",))
        self.group = WecomGroupChat.objects.create(chat_id="synth-chat", name="合成群 123456")
        self.url = f"/api/wecom/orders/{self.order.order_no}/group-qr/"

    def test_only_post_matched_group_creates(self):
        with mock.patch("apps.wecom.group_qr.get_or_create_qr", return_value={"status": "ready", "message": "", "qr_url": "https://example.invalid/qr"}) as get:
            self.client.get(f"/api/wecom/orders/{self.order.order_no}/groups/")
            get.assert_not_called()
            body = self.client.post(self.url, {"chat_id": "synth-chat"}, content_type="application/json").json()
            self.assertEqual(body["data"]["status"], "ready")
            get.assert_called_once()

    def test_wrong_group_and_no_permission_do_not_create(self):
        WecomGroupChat.objects.create(chat_id="other", name="不匹配 9988")
        with mock.patch("apps.wecom.group_qr.get_or_create_qr") as get:
            self.assertNotEqual(self.client.post(self.url, {"chat_id": "other"}, content_type="application/json").json()["code"], 0)
            denied = make_logged_in_client(username="qr-denied")
            self.assertNotEqual(denied.post(self.url, {"chat_id": "synth-chat"}, content_type="application/json").json()["code"], 0)
            get.assert_not_called()

    def test_group_from_another_tenant_cannot_create(self):
        from django_tenants.utils import tenant_context
        from apps.platform.provisioning import provision_tenant
        other, _ = provision_tenant(schema_name="wecom_qr_b", name="合成租户乙", domain="wecom-qr-b.test.com")
        with tenant_context(other):
            WecomGroupChat.objects.create(chat_id="other-tenant-chat", name="合成群 123456")
        with mock.patch("apps.wecom.group_qr.get_or_create_qr") as get:
            response = self.client.post(self.url, {"chat_id": "other-tenant-chat"}, content_type="application/json").json()
            self.assertNotEqual(response["code"], 0)
            get.assert_not_called()

    def test_no_game_access_cannot_generate(self):
        denied = make_logged_in_client(username="qr-no-game", groups=("其他客服",), nodes=("order.view_detail",), games=[])
        with mock.patch("apps.wecom.group_qr.get_or_create_qr") as get:
            response = denied.post(self.url, {"chat_id": "synth-chat"}, content_type="application/json").json()
            self.assertNotEqual(response["code"], 0)
            get.assert_not_called()

    def test_list_cursor_invalid_inputs_are_rejected_without_500(self):
        url = f"/api/wecom/orders/{self.order.order_no}/groups/"
        for value in ("-1", "invalid", "9223372036854775808"):
            with self.subTest(value=value):
                response = self.client.get(url, {"after": value})
                self.assertNotEqual(response.status_code, 500)
                self.assertNotEqual(response.json()["code"], 0)

    def test_direct_page_cursor_keeps_order_and_game_permissions(self):
        url = f"/api/wecom/orders/{self.order.order_no}/groups/"
        no_node = make_logged_in_client(username="qr-page-no-node")
        no_game = make_logged_in_client(username="qr-page-no-game", groups=("其他客服",), nodes=("order.view_detail",), games=[])
        for client in (no_node, no_game):
            response = client.get(url, {"after": "123"}).json()
            self.assertNotEqual(response["code"], 0)
            self.assertIsNone(response["data"])

    def test_at_most_five_matches_generate_but_six_never_do(self):
        with mock.patch("apps.wecom.group_qr.get_or_create_qr", return_value={"status": "ready", "message": "", "qr_url": "https://example.invalid/qr"}) as get:
            for i in range(4):
                WecomGroupChat.objects.create(chat_id=f"synth-extra-{i}", name="合成群 123456")
            listing = self.client.get(f"/api/wecom/orders/{self.order.order_no}/groups/").json()["data"]
            self.assertFalse(listing["too_many"])
            self.assertEqual(len(listing["groups"]), 5)
            self.assertIsNone(listing["next_after"])
            self.assertEqual(self.client.post(self.url, {"chat_id": "synth-chat"}, content_type="application/json").json()["data"]["status"], "ready")
            self.assertEqual(get.call_count, 1)
            WecomGroupChat.objects.create(chat_id="synth-sixth", name="合成群 123456")
            listing = self.client.get(f"/api/wecom/orders/{self.order.order_no}/groups/").json()["data"]
            self.assertTrue(listing["too_many"])
            self.assertEqual(len(listing["groups"]), 5)
            second = self.client.get(f"/api/wecom/orders/{self.order.order_no}/groups/", {"after": listing["next_after"]}).json()["data"]
            self.assertEqual([row["chat_id"] for row in second["groups"]], ["synth-sixth"])
            self.assertTrue(second["too_many"])
            self.assertIsNone(second["next_after"])
            data = self.client.post(self.url, {"chat_id": "synth-chat"}, content_type="application/json").json()["data"]
            self.assertEqual(data["status"], "unavailable")
            self.assertIn("超过5", data["message"])
            self.assertEqual(get.call_count, 1)

    def test_six_short_order_number_matches_are_blocked(self):
        self.order.source_order_no = "1"
        self.order.save(update_fields=["source_order_no"])
        for i in range(5):
            WecomGroupChat.objects.create(chat_id=f"synth-short-{i}", name=f"群 10{i}")
        with mock.patch("apps.wecom.group_qr.get_or_create_qr") as get:
            data = self.client.post(self.url, {"chat_id": "synth-chat"}, content_type="application/json").json()["data"]
            self.assertEqual(data["status"], "unavailable")
            get.assert_not_called()

    def test_sixth_group_added_during_remote_validation_blocks_creation(self):
        for i in range(4):
            WecomGroupChat.objects.create(chat_id=f"synth-five-{i}", name="合成群 123456")
        def add_sixth(*args):
            WecomGroupChat.objects.create(chat_id="synth-sixth", name="合成群 123456")
            return True
        with mock.patch("apps.wecom.group_qr._verify_group", side_effect=add_sixth), mock.patch("apps.wecom.group_qr._call") as api:
            data = self.client.post(self.url, {"chat_id": "synth-chat"}, content_type="application/json").json()["data"]
            self.assertEqual(data["status"], "unavailable")
            api.assert_not_called()

    def test_old_identity_groups_do_not_match_after_config_change(self):
        WecomAppConfig.objects.update(identity_revision=1)
        self.assertEqual(self.client.get(f"/api/wecom/orders/{self.order.order_no}/groups/").json()["data"]["groups"], [])
        with mock.patch("apps.wecom.group_qr.get_or_create_qr") as get:
            self.assertNotEqual(self.client.post(self.url, {"chat_id": "synth-chat"}, content_type="application/json").json()["code"], 0)
            get.assert_not_called()
