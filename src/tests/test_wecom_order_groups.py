from datetime import timedelta
from unittest import mock
import requests

from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order
from apps.wecom.group_sync import TimedSession, queue_event, run_due
from apps.wecom.models import WecomAppConfig, WecomGroupChat
from tests.p5_auth_helpers import make_logged_in_client


def event(kind="update", timestamp=100, **extra):
    return {"MsgType": "event", "Event": "change_external_chat", "ChatId": "synth-chat-a", "ChangeType": kind, "CreateTime": str(timestamp), **extra}


class GroupSyncTests(TenantTestCase):
    def setUp(self):
        WecomAppConfig.objects.create(tenant_schema=connection.schema_name, enabled=True, corp_id="corp", secret="secret")

    def due(self):
        WecomGroupChat.objects.update(retry_at=timezone.now() - timedelta(seconds=1))

    def test_http_timeout_covers_token_and_group_requests(self):
        session = TimedSession()
        with mock.patch.object(requests.Session, "request") as send:
            session.get("https://example.invalid/token")
            session.post("https://example.invalid/group", timeout=None)
        self.assertEqual([call.kwargs["timeout"] for call in send.call_args_list], [(5, 10), (5, 10)])

    def test_create_and_change_save_latest_details(self):
        queue_event(event("create"))
        self.due()
        with mock.patch("apps.wecom.group_sync.sdk.get_contacts_client") as client:
            client.return_value.post.return_value = {"errcode": 0, "group_chat": {"name": "群 123", "owner": "staff"}}
            run_due()
            client.return_value.post.assert_called_once_with(
                "externalcontact/groupchat/get", json={"chat_id": "synth-chat-a", "need_name": 1},
            )
        row = WecomGroupChat.objects.get()
        self.assertEqual(row.name, "群 123")
        self.assertEqual(row.details["owner"], "staff")
        self.assertTrue(row.pending)
        self.assertTrue(row.confirming)
        queue_event(event(timestamp=101))
        self.due()
        with mock.patch("apps.wecom.group_sync.sdk.get_contacts_client") as client:
            client.return_value.post.return_value = {"errcode": 0, "group_chat": {"name": "新群 456"}}
            run_due()
        row.refresh_from_db()
        self.assertEqual(row.name, "新群 456")

    def test_second_read_corrects_temporarily_stale_name(self):
        queue_event(event("update"))
        self.due()
        with mock.patch("apps.wecom.group_sync.sdk.get_contacts_client") as client:
            client.return_value.post.side_effect = [
                {"errcode": 0, "group_chat": {"name": "旧群"}},
                {"errcode": 0, "group_chat": {"name": "新群"}},
            ]
            run_due()
            self.due()
            run_due()
        row = WecomGroupChat.objects.get()
        self.assertEqual(row.name, "新群")
        self.assertFalse(row.pending)

    def test_refresh_discards_group_notice_but_keeps_other_details(self):
        queue_event(event("create"))
        self.due()
        details = {"name": "合成群 123", "notice": "合成公告", "owner": "staff", "member_list": [{"userid": "member"}]}
        with mock.patch("apps.wecom.group_sync.sdk.get_contacts_client") as client:
            client.return_value.post.return_value = {"errcode": 0, "group_chat": details}
            run_due()
            row = WecomGroupChat.objects.get()
            expected = {"name": "合成群 123", "owner": "staff", "member_list": [{"userid": "member"}]}
            self.assertEqual(row.details, expected)
            self.assertEqual(details["notice"], "合成公告")

            row.details = {**expected, "notice": "旧公告"}
            row.save(update_fields=["details"])
            queue_event(event(timestamp=101))
            self.due()
            run_due()
            row.refresh_from_db()
            self.assertEqual(row.details, expected)

    def test_timeout_or_secret_in_exception_is_not_logged(self):
        queue_event(event())
        self.due()
        with mock.patch("apps.wecom.group_sync.sdk.get_contacts_client") as client:
            client.return_value.post.side_effect = requests.Timeout(
                "https://example.invalid/?access_token=synthetic-token&corpsecret=synthetic-secret"
            )
            with self.assertLogs("apps.wecom.group_sync", level="WARNING") as logs:
                run_due()
        self.assertNotIn("synthetic-token", str(logs.output))
        self.assertNotIn("synthetic-secret", str(logs.output))
        self.assertTrue(WecomGroupChat.objects.get().pending)

    def test_failure_keeps_job_for_retry_and_never_accepts_error_envelope(self):
        queue_event(event())
        self.due()
        with mock.patch("apps.wecom.group_sync.sdk.get_contacts_client") as client:
            client.return_value.post.return_value = {"errcode": 60011, "errmsg": "denied"}
            run_due()
        row = WecomGroupChat.objects.get()
        self.assertTrue(row.pending)
        self.assertGreater(row.retry_at, timezone.now())
        self.assertEqual(row.name, "")

    def test_dismiss_during_fetch_cannot_be_overwritten(self):
        queue_event(event("create"))
        self.due()
        with mock.patch("apps.wecom.group_sync.sdk.get_contacts_client") as client:
            def later(*args, **kwargs):
                queue_event(event("dismiss", 101))
                return {"errcode": 0, "group_chat": {"name": "过时群"}}
            client.return_value.post.side_effect = later
            run_due()
        row = WecomGroupChat.objects.get()
        self.assertTrue(row.dismissed)
        self.assertFalse(row.pending)
        self.assertEqual(row.name, "")
        queue_event(event("update", 100))
        row.refresh_from_db()
        self.assertTrue(row.dismissed)

    def test_new_change_during_confirm_keeps_new_job(self):
        queue_event(event("update"))
        self.due()
        with mock.patch("apps.wecom.group_sync.sdk.get_contacts_client") as client:
            client.return_value.post.return_value = {"errcode": 0, "group_chat": {"name": "旧群"}}
            run_due()
            self.due()
            def newer(*args, **kwargs):
                queue_event(event("update", 101))
                return {"errcode": 0, "group_chat": {"name": "过期结果"}}
            client.return_value.post.side_effect = newer
            run_due()
        row = WecomGroupChat.objects.get()
        self.assertEqual(row.name, "旧群")
        self.assertTrue(row.pending)
        self.assertFalse(row.confirming)


class OrderGroupReadTests(TenantTestCase):
    def setUp(self):
        WecomAppConfig.objects.create(tenant_schema=connection.schema_name, enabled=True, corp_id="corp", agent_id=1, secret="secret")
        game = Game.objects.create(name="合成游戏")
        self.order = Order.objects.create(game=game, order_no="20261113000000000001", source_order_no="123456")
        self.client = make_logged_in_client(username="group-reader", groups=("客服",), nodes=("order.view_detail",))
        self.url = f"/api/wecom/orders/{self.order.order_no}/groups/"

    def test_nul_order_number_is_rejected(self):
        for order_no in ("%00", "%1f", "%7f"):
            with self.subTest(order_no=order_no):
                response = self.client.get(f"/api/wecom/orders/{order_no}/groups/")
                self.assertEqual(response.status_code, 404)

    def test_malformed_printable_order_number_returns_business_error(self):
        response = self.client.get("/api/wecom/orders/not-a-number/groups/")
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["code"], 0)

    def test_contains_full_source_and_excludes_dismissed(self):
        WecomGroupChat.objects.create(chat_id="a", name="群 123456 中文", details={"owner": "owner", "member_list": [{"name": "member", "group_nickname": "群里昵称", "userid": "member-a", "unionid": "secret-other-field"}]})
        WecomGroupChat.objects.create(chat_id="b", name="前缀123456后缀")
        WecomGroupChat.objects.create(chat_id="c", name="群 12345")
        WecomGroupChat.objects.create(chat_id="d", name="群 123456 已解散", dismissed=True)
        body = self.client.get(self.url).json()
        self.assertEqual(body["code"], 0, body)
        groups = body["data"]["groups"]
        self.assertEqual({row["chat_id"] for row in groups}, {"a", "b"})
        self.assertEqual(next(row for row in groups if row["chat_id"] == "a")["members"], [{"name": "群里昵称", "userid": "member-a"}])

    def test_old_group_snapshot_enriches_existing_members_once(self):
        WecomGroupChat.objects.create(
            chat_id="missing-roster-names", name="合成 123456",
            details={"member_list": [{"userid": "owner-old"}, {"userid": "member-old"}, {"userid": ["malformed"]}]},
        )
        result = {"errcode": 0, "group_chat": {"chat_id": "missing-roster-names", "member_list": [
            {"userid": "owner-old", "name": "群主名字"},
            {"userid": "member-old", "name": "个人名称", "group_nickname": "群聊昵称"},
            {"userid": "other", "name": "名单变化时先等正常同步"},
        ]}}
        with mock.patch("apps.wecom.group_sync.sdk.get_contacts_client") as client:
            client.return_value.post.return_value = result
            for _ in range(2):
                data = self.client.get(self.url).json()["data"]["groups"]
                self.assertEqual(data[0]["members"], [
                    {"name": "群主名字", "userid": "owner-old"},
                    {"name": "群聊昵称", "userid": "member-old"},
                    {"name": "", "userid": ""},
                ])
            client.return_value.post.assert_called_once_with(
                "externalcontact/groupchat/get", json={"chat_id": "missing-roster-names", "need_name": 1},
            )

    def test_disabling_group_sync_does_not_query_upstream_roster(self):
        WecomAppConfig.objects.update(group_sync_enabled=False)
        WecomGroupChat.objects.create(
            chat_id="disabled-roster", name="合成 123456",
            details={"member_list": [{"userid": "old-user"}]},
        )
        with mock.patch("apps.wecom.group_sync.sdk.get_contacts_client") as client:
            reply = self.client.get(self.url).json()
        self.assertEqual(reply["code"], 0)
        self.assertEqual(reply["data"]["groups"][0]["members"], [{"name": "", "userid": "old-user"}])
        client.assert_not_called()

    def test_blank_source_matches_nothing(self):
        self.order.source_order_no = " "
        self.order.save(update_fields=["source_order_no"])
        WecomGroupChat.objects.create(chat_id="a", name="任意群")
        self.assertEqual(self.client.get(self.url).json()["data"]["groups"], [])

    def test_no_node_or_game_access(self):
        denied = make_logged_in_client(username="group-no-node")
        self.assertNotEqual(denied.get(self.url).json()["code"], 0)
        no_game = make_logged_in_client(username="group-no-game", groups=("其他客服",), nodes=("order.view_detail",), games=[])
        self.assertNotEqual(no_game.get(self.url).json()["code"], 0)

    def test_other_tenant_group_never_appears(self):
        from django_tenants.utils import tenant_context
        from apps.platform.provisioning import provision_tenant

        other, _ = provision_tenant(schema_name="wecom_groups_b", name="合成租户乙", domain="wecom-groups-b.test.com")
        with tenant_context(other):
            WecomGroupChat.objects.create(chat_id="other-chat", name="其他租户群 123456")
        WecomGroupChat.objects.create(chat_id="own-chat", name="本租户群 123456")
        groups = self.client.get(self.url).json()["data"]["groups"]
        self.assertEqual([g["name"] for g in groups], ["本租户群 123456"])
