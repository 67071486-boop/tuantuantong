import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.db import connection
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase

from tests.p5_auth_helpers import make_logged_in_client

BASE = "/api/internal-chat/"


class ServiceClient(Client):
    def _base_environ(self, **request):
        environ = super()._base_environ(**request)
        if not environ.get("HTTP_COOKIE"):
            environ.pop("HTTP_COOKIE", None)
        return environ


class ChatServiceBridgeTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from apps.platform.provisioning import provision_tenant
        cls.other_tenant, _ = provision_tenant(schema_name="bridge_other", name="桥接测试乙", domain="bridge-other.test.local")

    def setUp(self):
        self.enterContext(override_settings(DEBUG=True, TT_CHAT_ORDER_GROUPS_ENABLED=True))
        self.alice = make_logged_in_client(domain=self.domain.domain, username="bridge-alice", games=None)
        from django.contrib.auth import get_user_model
        self.alice_id = get_user_model().objects.get(username="bridge-alice").pk
        self.bob = make_logged_in_client(domain=self.domain.domain, username="bridge-bob", games=None)
        self.bob_id = get_user_model().objects.get(username="bridge-bob").pk
        self.conversation = self.data(self.alice.post(BASE + "conversations/", {"peer_id": self.bob_id}, content_type="application/json"))
        self.temp = self.enterContext(TemporaryDirectory())
        self.config_file = Path(self.temp) / "config.json"
        self.config_file.write_text(json.dumps({"service_url": "http://127.0.0.1:9050", "tenants": {
            connection.schema_name: {"key_id": "test-a", "secret": "synthetic-secret-a"},
            "bridge_other": {"key_id": "test-b", "secret": "synthetic-secret-b"},
        }}))
        self.config_file.chmod(0o600)
        self.enterContext(override_settings(TT_CHAT_SERVICE_CONFIG_FILE=str(self.config_file)))
        self.source = ServiceClient(SERVER_NAME=self.domain.domain)

    def data(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.content)
        return response.json()["data"]

    def test_export_rejects_browser_session_without_signature(self):
        response = self.alice.get(BASE + "service/snapshot/?kind=conversations")
        self.assertEqual(response.status_code, 403, response.content)

    def signed_get(self, path, *, secret="synthetic-secret-a", key_id="test-a", now=None, client=None):
        from chat_service.protocol import sign_request
        headers = sign_request(secret, key_id, "GET", BASE + path, b"", now=now)
        return (client or self.source).get(BASE + path, **{
            "HTTP_" + key.upper().replace("-", "_"): value for key, value in headers.items()
        })

    def send(self, text="切换期间新消息", client_id="bridge-message"):
        return self.data(self.alice.post(BASE + f"conversations/{self.conversation['id']}/messages/",
                                        {"text": text, "client_id": client_id}, content_type="application/json"))

    def test_signed_snapshot_keeps_direct_member_names_and_current_messages(self):
        sent = self.send()
        snapshot = self.data(self.signed_get("service/snapshot/?kind=conversations&limit=1"))
        row = snapshot["rows"][0]
        self.assertEqual(row["kind"], "direct")
        self.assertEqual([(m["local_user_id"], m["name"]) for m in row["members"]],
                         [(self.alice_id, "bridge-alice"), (self.bob_id, "bridge-bob")])
        messages = self.data(self.signed_get("service/snapshot/?kind=messages&limit=1"))
        self.assertEqual(messages["rows"][0]["message"]["text"], "切换期间新消息")
        self.assertEqual(messages["rows"][0]["message"]["id"], sent["id"])
        self.assertEqual(messages["rows"][0]["message"]["sender_local_user_id"], self.alice_id)
        self.assertGreaterEqual(snapshot["latest"], 2)

    def test_private_system_notices_are_kept_out_of_chat_service_exports(self):
        from django.contrib.auth import get_user_model
        from django_tenants.utils import tenant_context

        from apps.internal_chat.system_notifications import send_order_notification

        initial = self.data(self.signed_get("service/export/"))["latest"]
        with tenant_context(self.tenant):
            recipient = get_user_model().objects.get(pk=self.alice_id)
            send_order_notification(
                recipient, order_no="20260101010101000001", text="私有摘要", client_id="refund-bridge",
            )
        normal = self.send()
        cursor = initial
        for _ in range(2):
            page = self.data(self.signed_get(f"service/export/?after={cursor}&limit=1"))
            self.assertEqual(page["events"], [])
            self.assertTrue(page["has_more"])
            self.assertGreater(page["cursor"], cursor)
            cursor = page["cursor"]
        page = self.data(self.signed_get(f"service/export/?after={cursor}"))
        self.assertEqual([event["message"]["id"] for event in page["events"]], [normal["id"]])
        self.assertEqual(page["cursor"], page["latest"])
        messages = self.data(self.signed_get("service/snapshot/?kind=messages"))
        self.assertEqual([item["message"]["id"] for item in messages["rows"]], [normal["id"]])

    def test_remote_history_requires_auth_and_keeps_business_viewer_and_minimum_cursor(self):
        from unittest.mock import patch
        import httpx
        sent = self.send()
        captured = []
        def remote(url, *, content, headers, timeout):
            from chat_service.protocol import verify_request
            verify_request({"test-a": "synthetic-secret-a"}, headers, "POST", "/v1/read", content)
            captured.append(json.loads(content))
            return httpx.Response(200, json={"code": 0, "msg": "", "data": {
                "messages": [{**sent, "text": "来自独立聊天服务"}], "has_more": False, "has_newer": False,
            }}, request=httpx.Request("POST", url))
        with override_settings(TT_CHAT_READ_BACKEND="matrix"), patch("apps.internal_chat.matrix_bridge.httpx.post", side_effect=remote):
            page = self.data(self.alice.get(BASE + f"conversations/{self.conversation['id']}/messages/"))
        self.assertEqual(page["messages"][0]["text"], "来自独立聊天服务")
        self.assertEqual(captured[0]["viewer_id"], self.alice_id)
        self.assertEqual(captured[0]["readonly"], False)
        self.assertEqual(captured[0]["query"], {})
        self.assertGreaterEqual(captured[0]["minimum_cursor"], 2)

    def test_wrong_expired_and_other_tenant_keys_and_browser_cookie_are_rejected(self):
        import time
        for options in ({"secret": "wrong"}, {"now": int(time.time()) - 61},
                        {"secret": "synthetic-secret-b", "key_id": "test-b"}, {"client": self.alice}):
            with self.subTest(options=options):
                self.assertEqual(self.signed_get("service/export/?after=0", **options).status_code, 403)

    def test_export_rejects_tenant_parameters_unknown_fields_duplicates_and_body(self):
        for suffix in ("tenant=test", "schema_name=test", "studio=other", "limit=1&limit=2", "limit=-1", "after=no"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.signed_get("service/export/?" + suffix).status_code, 400)
        from chat_service.protocol import sign_request
        path = BASE + "service/export/"
        body = b'{"tenant":"other"}'
        headers = sign_request("synthetic-secret-a", "test-a", "GET", path, body)
        response = self.source.generic("GET", path, body, content_type="application/json", **{
            "HTTP_" + key.upper().replace("-", "_"): value for key, value in headers.items()
        })
        self.assertEqual(response.status_code, 400)

    def test_unconfigured_tenant_does_not_export(self):
        with override_settings(TT_CHAT_SERVICE_CONFIG_FILE=""):
            response = self.signed_get("service/export/?after=0")
        self.assertNotEqual(response.json()["code"], 0)
        self.assertIsNone(response.json()["data"])

    def test_incremental_pagination_keeps_later_redaction_and_preference_read_events(self):
        first = self.send("先发送再撤回", "bridge-first")
        self.send("后续保留消息", "bridge-second")
        path = BASE + f"conversations/{self.conversation['id']}/"
        self.data(self.alice.post(path + f"messages/{first['id']}/revoke/"))
        self.data(self.bob.post(path + "read/", {"seq": 2}, content_type="application/json"))
        self.data(self.bob.post(path + "preferences/", {"pinned": True, "remark": "合成备注", "marked_unread": True}, content_type="application/json"))
        cursor, events = 0, []
        while True:
            page = self.data(self.signed_get(f"service/export/?after={cursor}&limit=1"))
            events.extend(page["events"])
            if not page["has_more"]:
                break
            self.assertGreater(page["cursor"], cursor)
            cursor = page["cursor"]
        ids = [row["id"] for row in events]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertEqual([row["kind"] for row in events], ["conversation.created", "message.created", "message.created",
                         "message.revoked", "conversation.read", "conversation.preferences_changed"])
        redactions = [row for row in events if row["message"] and row["message"]["id"] == first["id"]]
        self.assertEqual(len(redactions), 2)
        for row in redactions:
            self.assertTrue(row["message"]["revoked"])
            self.assertEqual(row["message"]["text"], "")
            self.assertNotIn("image_data", row["message"])
        self.assertEqual(events[-1]["conversation"]["preferences"], [{"user_id": self.bob_id, "pinned": True,
                         "remark": "合成备注", "marked_unread": True}])
        messages = self.data(self.signed_get("service/snapshot/?kind=messages&limit=1"))
        self.assertTrue(messages["has_more"])
        second = self.data(self.signed_get(f"service/snapshot/?kind=messages&after={messages['cursor']}&limit=1"))
        self.assertEqual(second["rows"][0]["message"]["text"], "后续保留消息")
        self.assertFalse(second["has_more"])

    def test_export_image_bytes_disappear_after_revoke(self):
        import base64
        from io import BytesIO
        from PIL import Image
        image = BytesIO()
        Image.new("RGB", (2, 3), "red").save(image, format="PNG")
        encoded = base64.b64encode(image.getvalue()).decode()
        path = BASE + f"conversations/{self.conversation['id']}/"
        sent = self.data(self.alice.post(path + "messages/", {"text": "", "client_id": "bridge-image",
                         "image": {"mime": "image/png", "data": encoded}}, content_type="application/json"))
        snapshot = self.data(self.signed_get("service/snapshot/?kind=messages"))
        self.assertEqual(snapshot["rows"][0]["message"]["image_data"], {"mime": "image/png", "data": encoded})
        self.data(self.alice.post(path + f"messages/{sent['id']}/revoke/"))
        snapshot = self.data(self.signed_get("service/snapshot/?kind=messages"))
        self.assertNotIn("image_data", snapshot["rows"][0]["message"])
        self.assertNotIn("image", snapshot["rows"][0]["message"])

    def test_failed_unavailable_and_lagging_service_return_503_without_fallback(self):
        from unittest.mock import patch
        import httpx
        self.send()
        path = BASE + f"conversations/{self.conversation['id']}/messages/"
        failures = [httpx.ConnectError("offline"),
                    httpx.Response(503, json={"code": 503, "data": None}, request=httpx.Request("POST", "http://127.0.0.1/v1/read")),
                    httpx.Response(200, json={"code": 409, "data": {}}, request=httpx.Request("POST", "http://127.0.0.1/v1/read")),
                    httpx.Response(200, json={"code": 0, "data": {}}, request=httpx.Request("POST", "http://127.0.0.1/v1/read"))]
        for failure in failures:
            with self.subTest(failure=failure), override_settings(TT_CHAT_READ_BACKEND="matrix"):
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch("apps.internal_chat.matrix_bridge.httpx.post", **kwargs):
                    response = self.alice.get(path)
                self.assertEqual((response.status_code, response.json()["code"], response.json()["data"]), (503, 503, None))

    def test_outage_does_not_block_writes_and_legacy_return_keeps_new_messages_and_sync(self):
        from unittest.mock import patch
        import httpx
        with override_settings(TT_CHAT_READ_BACKEND="matrix"), patch("apps.internal_chat.matrix_bridge.httpx.post", side_effect=httpx.ConnectError("offline")):
            sent = self.send()
            sync = self.data(self.alice.get(BASE + "sync/?after=0"))
            self.assertIn(sent["id"], [row["message"]["id"] for row in sync["changes"] if row["message"]])
            conversations = self.data(self.alice.get(BASE + "conversations/"))
            self.assertEqual(conversations["conversations"][0]["last_message"]["text"], "切换期间新消息")
        with override_settings(TT_CHAT_READ_BACKEND="legacy"):
            page = self.data(self.bob.get(BASE + f"conversations/{self.conversation['id']}/messages/"))
        self.assertEqual(page["messages"], [sent])

    def test_ordinary_nonmember_is_denied_before_any_remote_read(self):
        from unittest.mock import patch
        outsider = make_logged_in_client(domain=self.domain.domain, username="bridge-outsider", games=None)
        with override_settings(TT_CHAT_READ_BACKEND="matrix"), patch("apps.internal_chat.matrix_bridge.httpx.post", side_effect=AssertionError("must not reach remote")):
            for suffix in ("messages/", "messages/search/?q=合成", "messages/1/image/"):
                response = outsider.get(BASE + f"conversations/{self.conversation['id']}/" + suffix)
                self.assertNotEqual(response.json()["code"], 0)

    def test_same_primary_keys_across_tenants_cannot_be_read_with_other_key(self):
        from django.contrib.auth import get_user_model
        from django_tenants.utils import tenant_context
        from apps.internal_chat.models import Change, ChangeCursor, Conversation, Message
        sent = self.send("只属于甲的内容", "tenant-a")
        with tenant_context(self.other_tenant):
            user = get_user_model().objects.create_user(pk=self.alice_id, username="bridge-other-alice")
            peer = get_user_model().objects.create_user(pk=self.bob_id, username="bridge-other-bob")
            conversation = Conversation.objects.create(pk=self.conversation["id"], first_user=user, second_user=peer, latest_seq=1)
            message = Message.objects.create(pk=sent["id"], conversation=conversation, sender=user, seq=1,
                                             client_id="tenant-b", text="只属于乙的内容")
            change = Change.objects.create(conversation=conversation, message=message, kind=Change.Kind.MESSAGE_CREATED)
            ChangeCursor.objects.update_or_create(pk=1, defaults={"latest": change.pk})
        other_source = ServiceClient(SERVER_NAME="bridge-other.test.local")
        denied = self.signed_get("service/snapshot/?kind=messages", client=other_source)
        self.assertEqual(denied.status_code, 403)
        own = self.data(self.signed_get("service/snapshot/?kind=messages", client=other_source,
                                      secret="synthetic-secret-b", key_id="test-b"))
        self.assertEqual([row["message"]["text"] for row in own["rows"]], ["只属于乙的内容"])
        source = self.data(self.signed_get("service/snapshot/?kind=messages"))
        self.assertEqual([row["message"]["text"] for row in source["rows"]], ["只属于甲的内容"])
        denied_reverse = self.signed_get("service/snapshot/?kind=messages", secret="synthetic-secret-b", key_id="test-b")
        self.assertEqual(denied_reverse.status_code, 403)

    @override_settings(DEPOSIT_ENFORCED=False)
    def test_order_readonly_preview_and_reassignment_use_current_business_permissions(self):
        from unittest.mock import patch
        from django.contrib.auth import get_user_model
        import httpx
        from apps.business.models import Game, Order, OrderStatus
        number = "20261119000000000481"
        Order.objects.create(game=Game.objects.create(name="桥接订单游戏"), order_no=number,
                             status=OrderStatus.UNASSIGNED, total_price=100, hall_price=50)
        publisher = make_logged_in_client(domain=self.domain.domain, username="bridge-publisher", is_superuser=True)
        viewer = make_logged_in_client(domain=self.domain.domain, username="bridge-viewer", groups=("查看订单",), nodes=("order.view_content",))
        worker = make_logged_in_client(domain=self.domain.domain, username="bridge-worker", groups=("代练员",))
        replacement = make_logged_in_client(domain=self.domain.domain, username="bridge-replacement", groups=("代练员",))
        worker_id = get_user_model().objects.get(username="bridge-worker").pk
        replacement_id = get_user_model().objects.get(username="bridge-replacement").pk
        root = f"/api/business/orders/{number}/"
        self.data(publisher.post(root + "throw-to-hall/", {}, content_type="application/json"))
        group = self.data(publisher.get(BASE + f"orders/{number}/"))
        self.data(publisher.post(root + "assign/", {"user_id": worker_id}, content_type="application/json"))
        message_path = BASE + f"conversations/{group['id']}/messages/"
        sent = self.data(publisher.post(message_path, {"text": "真实订单消息", "client_id": "order-message"}, content_type="application/json"))
        captured = []
        def remote(url, *, content, headers, timeout):
            captured.append(json.loads(content))
            return httpx.Response(200, json={"code": 0, "data": {"messages": [sent], "has_more": False, "has_newer": False}}, request=httpx.Request("POST", url))
        with override_settings(TT_CHAT_READ_BACKEND="matrix"), patch("apps.internal_chat.matrix_bridge.httpx.post", side_effect=remote):
            self.assertEqual(self.data(viewer.get(BASE + f"orders/{number}/messages/"))["messages"], [sent])
            self.assertTrue(captured[-1]["readonly"])
            self.assertEqual(captured[-1]["viewer_id"], get_user_model().objects.get(username="bridge-viewer").pk)
            self.assertNotEqual(viewer.get(message_path).json()["code"], 0)
            self.data(worker.get(message_path))
            self.assertFalse(captured[-1]["readonly"])
            self.data(publisher.post(root + "assign/", {"user_id": replacement_id}, content_type="application/json"))
            count = len(captured)
            self.assertNotEqual(worker.get(message_path).json()["code"], 0)
            self.assertNotEqual(worker.get(BASE + f"orders/{number}/messages/").json()["code"], 0)
            self.assertEqual(len(captured), count)
            self.data(replacement.get(message_path))
        snapshots = self.data(self.signed_get("service/snapshot/?kind=conversations"))["rows"]
        order = next(row for row in snapshots if row["id"] == group["id"])
        previous = next(member for member in order["members"] if member["local_user_id"] == worker_id)
        self.assertIsNotNone(previous["left_at"])
        self.assertEqual(previous["order_role"], "")
        self.assertTrue(any(member["local_user_id"] == replacement_id and member["left_at"] is None and member["order_role"] == "worker" for member in order["members"]))

    def test_remote_search_and_image_keep_original_query_and_response_shape(self):
        import base64
        from io import BytesIO
        from unittest.mock import patch
        import httpx
        from PIL import Image
        image = BytesIO()
        Image.new("RGB", (2, 3), "blue").save(image, format="PNG")
        encoded = base64.b64encode(image.getvalue()).decode()
        sent = self.send()
        queries = []
        def remote(url, *, content, headers, timeout):
            payload = json.loads(content)
            queries.append(payload)
            data = {"messages": [sent], "count": 1, "has_more": False} if payload["operation"] == "search" else {"mime": "image/png", "data": encoded}
            return httpx.Response(200, json={"code": 0, "data": data}, request=httpx.Request("POST", url))
        root = BASE + f"conversations/{self.conversation['id']}/messages/"
        with override_settings(TT_CHAT_READ_BACKEND="matrix"), patch("apps.internal_chat.matrix_bridge.httpx.post", side_effect=remote):
            result = self.data(self.alice.get(root + "search/", {"q": "查询", "sender_id": self.alice_id, "before": 100}))
            self.assertEqual(result, {"messages": [sent], "count": 1, "has_more": False})
            result = self.data(self.alice.get(root + f"{sent['id']}/image/"))
        self.assertEqual(queries[0]["query"]["q"], "查询")
        self.assertEqual(queries[0]["query"]["sender_id"], self.alice_id)
        self.assertEqual(queries[0]["query"]["before"], 100)
        with Image.open(BytesIO(base64.b64decode(result["data"]))) as loaded:
            loaded.load()
            self.assertEqual(loaded.size, (2, 3))
        self.assertEqual(queries[1]["query"]["message_id"], sent["id"])

    def test_app_notification_history_uses_the_same_remote_reader(self):
        from datetime import timedelta
        from unittest.mock import patch
        import httpx
        from django.contrib.auth.models import Group
        from django.utils import timezone
        from apps.accounts.models import AppToken
        from apps.business.peer_sync import hash_token
        from apps.business.roles import WORKER
        sent = self.send()
        group, _ = Group.objects.get_or_create(name=WORKER)
        group.user_set.add(self.bob_id)
        raw = "synthetic-bridge-app-token"
        AppToken.objects.create(user_id=self.bob_id, key_hash=hash_token(raw), expires_at=timezone.now() + timedelta(minutes=5))
        app = Client(SERVER_NAME=self.domain.domain, HTTP_AUTHORIZATION=f"Bearer {raw}")
        reply = httpx.Response(200, json={"code": 0, "data": {"messages": [{**sent, "text": "App远端消息"}],
                               "has_more": False, "has_newer": False}}, request=httpx.Request("POST", "http://127.0.0.1/v1/read"))
        with override_settings(TT_CHAT_READ_BACKEND="matrix"), patch("apps.internal_chat.matrix_bridge.httpx.post", return_value=reply):
            page = self.data(app.get(BASE + f"app/notification-conversations/{self.conversation['id']}/messages/"))
        self.assertEqual(page["messages"][0]["text"], "App远端消息")
        self.assertEqual(page["self_member_id"], self.bob_id)
        self.assertEqual(page["peer_name"], "bridge-alice")

    def test_missing_and_unsafe_remote_configuration_returns_retryable_failure(self):
        from unittest.mock import patch
        path = BASE + f"conversations/{self.conversation['id']}/messages/"
        with override_settings(TT_CHAT_READ_BACKEND="matrix", TT_CHAT_SERVICE_CONFIG_FILE=""), patch("apps.internal_chat.matrix_bridge.httpx.post", side_effect=AssertionError("must not call")):
            response = self.alice.get(path)
        self.assertEqual(response.status_code, 503)
        config = json.loads(self.config_file.read_text())
        config["service_url"] = "http://public-service.example.invalid"
        self.config_file.write_text(json.dumps(config))
        with override_settings(TT_CHAT_READ_BACKEND="matrix"), patch("apps.internal_chat.matrix_bridge.httpx.post", side_effect=AssertionError("must not call")):
            response = self.alice.get(path)
        self.assertEqual(response.status_code, 503)
        self.config_file.chmod(0o644)
        self.assertEqual(self.signed_get("service/export/").status_code, 503)

    def test_remote_missing_revoked_image_keeps_original_not_found_result(self):
        from unittest.mock import patch
        import httpx
        response = httpx.Response(404, json={"code": 404, "msg": "not found", "data": None},
                                  request=httpx.Request("POST", "http://127.0.0.1/v1/read"))
        with override_settings(TT_CHAT_READ_BACKEND="matrix"), patch("apps.internal_chat.matrix_bridge.httpx.post", return_value=response):
            result = self.alice.get(BASE + f"conversations/{self.conversation['id']}/messages/1/image/")
        self.assertEqual((result.status_code, result.json()["code"], result.json()["data"]), (200, 1002, None))

    def test_tenant_legacy_override_keeps_other_tenant_on_matrix_in_same_process(self):
        from unittest.mock import patch
        from django_tenants.utils import tenant_context
        import httpx
        self.send("甲工作室继续读取本地日志", "tenant-rollback-a")
        with tenant_context(self.other_tenant):
            alice = make_logged_in_client(domain="bridge-other.test.local", username="rollback-other-alice", games=None)
            from django.contrib.auth import get_user_model
            make_logged_in_client(domain="bridge-other.test.local", username="rollback-other-bob", games=None)
            bob_id = get_user_model().objects.get(username="rollback-other-bob").pk
            conversation = self.data(alice.post(BASE + "conversations/", {"peer_id": bob_id}, content_type="application/json"))
            sent = self.data(alice.post(BASE + f"conversations/{conversation['id']}/messages/", {
                "text": "乙本地日志", "client_id": "tenant-rollback-b",
            }, content_type="application/json"))
        config = json.loads(self.config_file.read_text())
        config["tenants"][self.tenant.schema_name]["read_backend"] = "legacy"
        config["tenants"]["bridge_other"]["read_backend"] = "matrix"
        self.config_file.write_text(json.dumps(config))
        requests = []
        def remote(url, *, content, headers, timeout):
            from chat_service.protocol import verify_request
            key_id = verify_request({"test-b": "synthetic-secret-b"}, headers, "POST", "/v1/read", content)
            requests.append(key_id)
            return httpx.Response(200, json={"code": 0, "data": {"messages": [{**sent, "text": "乙仍读取远端"}],
                                  "has_more": False, "has_newer": False}}, request=httpx.Request("POST", url))
        with override_settings(TT_CHAT_READ_BACKEND="matrix"), patch("apps.internal_chat.matrix_bridge.httpx.post", side_effect=remote), tenant_context(self.tenant):
            page = self.data(self.alice.get(BASE + f"conversations/{self.conversation['id']}/messages/", {
                "read_backend": "matrix", "tenant": "bridge_other",
            }))
            self.assertEqual(page["messages"][0]["text"], "甲工作室继续读取本地日志")
            self.assertEqual(requests, [])
            page = self.data(alice.get(BASE + f"conversations/{conversation['id']}/messages/", {"read_backend": "legacy"}))
            self.assertEqual(page["messages"][0]["text"], "乙仍读取远端")
            self.assertEqual(requests, ["test-b"])
            cross_cookie = self.alice.get(BASE + f"conversations/{conversation['id']}/messages/", HTTP_HOST="bridge-other.test.local")
            self.assertEqual(cross_cookie.status_code, 401)
            self.assertEqual(requests, ["test-b"])

    def test_unknown_tenant_read_backend_is_unavailable_and_global_legacy_ignores_bad_config(self):
        self.send("全局回切后仍保留消息", "global-rollback")
        config = json.loads(self.config_file.read_text())
        path = BASE + f"conversations/{self.conversation['id']}/messages/"
        from unittest.mock import patch
        for backend in ("unknown", None, 0):
            config["tenants"][self.tenant.schema_name]["read_backend"] = backend
            self.config_file.write_text(json.dumps(config))
            with self.subTest(backend=backend), override_settings(TT_CHAT_READ_BACKEND="matrix"), patch(
                "apps.internal_chat.matrix_bridge.httpx.post", side_effect=AssertionError("must not call remote"),
            ):
                self.assertEqual(self.alice.get(path).status_code, 503)
        self.config_file.write_text("invalid json")
        self.config_file.chmod(0o644)
        with override_settings(TT_CHAT_READ_BACKEND="legacy"), patch(
            "apps.internal_chat.matrix_bridge.httpx.post", side_effect=AssertionError("must not call remote"),
        ):
            page = self.data(self.alice.get(path))
        self.assertEqual(page["messages"][0]["text"], "全局回切后仍保留消息")

    def test_remote_lag_waits_for_catchup_with_fixed_body_and_minimum_cursor(self):
        from unittest.mock import patch
        import httpx
        sent = self.send("等待同步的消息", "read-lag-first")
        initial_cursor = self.data(self.signed_get("service/export/"))["latest"]
        clock = [100.0]
        calls = []
        def remote(url, *, content, headers, timeout):
            calls.append({"body": content, "timeout": timeout})
            clock[0] += 0.4
            if len(calls) == 1:
                self.send("等待期间另有消息入账", "read-lag-newer")
                return httpx.Response(503, json={"code": 503, "msg": "Matrix mirror has not caught up", "data": None},
                                      request=httpx.Request("POST", url))
            return httpx.Response(200, json={"code": 0, "data": {"messages": [sent], "has_more": False, "has_newer": False}},
                                  request=httpx.Request("POST", url))
        def wait(seconds):
            clock[0] += seconds
        with override_settings(TT_CHAT_READ_BACKEND="matrix"), patch(
            "apps.internal_chat.matrix_bridge.monotonic", side_effect=lambda: clock[0],
        ), patch("apps.internal_chat.matrix_bridge.sleep", side_effect=wait), patch(
            "apps.internal_chat.matrix_bridge.httpx.post", side_effect=remote,
        ):
            page = self.data(self.alice.get(BASE + f"conversations/{self.conversation['id']}/messages/"))
        self.assertEqual(page["messages"], [sent])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["body"], calls[1]["body"])
        self.assertEqual(json.loads(calls[1]["body"])["minimum_cursor"], initial_cursor)
        self.assertLess(calls[1]["timeout"], calls[0]["timeout"])
        self.assertLessEqual(clock[0] - 100, 5)
        self.assertGreater(self.data(self.signed_get("service/export/"))["latest"], initial_cursor)

    def test_permanent_remote_lag_stops_at_total_deadline_without_legacy_fallback(self):
        from unittest.mock import patch
        import httpx
        self.send("不能假装成远端返回的本地消息", "read-permanent-lag")
        clock = [100.0]
        timeouts = []
        def remote(url, *, content, headers, timeout):
            timeouts.append(timeout)
            clock[0] += min(0.07, timeout)
            return httpx.Response(503, json={"code": 503, "msg": "Matrix mirror has not caught up", "data": None},
                                  request=httpx.Request("POST", url))
        def wait(seconds):
            clock[0] += seconds
        with override_settings(TT_CHAT_READ_BACKEND="matrix"), patch(
            "apps.internal_chat.matrix_bridge.monotonic", side_effect=lambda: clock[0],
        ), patch("apps.internal_chat.matrix_bridge.sleep", side_effect=wait), patch(
            "apps.internal_chat.matrix_bridge.httpx.post", side_effect=remote,
        ):
            response = self.alice.get(BASE + f"conversations/{self.conversation['id']}/messages/")
        self.assertEqual((response.status_code, response.json()["code"], response.json()["data"]), (503, 503, None))
        self.assertGreater(len(timeouts), 1)
        self.assertTrue(all(0 < timeout <= 5 for timeout in timeouts))
        self.assertEqual(timeouts, sorted(timeouts, reverse=True))
        self.assertLessEqual(clock[0] - 100, 5.000001)
        self.assertGreaterEqual(clock[0] - 100, 4.8)

    def test_dependency_failures_and_permission_errors_are_not_retried(self):
        from unittest.mock import patch
        import httpx
        request = httpx.Request("POST", "http://127.0.0.1/v1/read")
        failures = [
            (httpx.Response(503, json={"code": 503, "msg": "Chat dependency unavailable", "data": None}, request=request), 503),
            (httpx.ConnectError("unreachable"), 503),
            (httpx.Response(403, json={"code": 403, "msg": "Conversation membership required", "data": None}, request=request), 403),
        ]
        for failure, status in failures:
            kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
            with self.subTest(status=status, failure=failure), override_settings(TT_CHAT_READ_BACKEND="matrix"), patch(
                "apps.internal_chat.matrix_bridge.httpx.post", **kwargs,
            ) as remote, patch("apps.internal_chat.matrix_bridge.sleep", side_effect=AssertionError("must not wait")):
                response = self.alice.get(BASE + f"conversations/{self.conversation['id']}/messages/")
            self.assertEqual(response.status_code, status)
            self.assertEqual(remote.call_count, 1)
