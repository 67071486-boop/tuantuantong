from datetime import timedelta
from unittest import mock

from django.core import signing
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.models import Game, Order
from apps.platform.provisioning import provision_tenant
from apps.wecom.archive import ArchiveError, run_due
from apps.wecom.archive_order_session import ORDER_ARCHIVE_VIEWER_SESSION_KEY
from apps.wecom.archive_views import ARCHIVE_VIEWER_SESSION_KEY
from apps.wecom.models import WecomAppConfig, WecomArchiveMessage, WecomArchiveOrderGroup, WecomGroupChat
from tests.p5_auth_helpers import P2_TEST_PASSWORD, make_logged_in_client
from tests.test_wecom_archive_sync import message, sdk_page


class OrderArchiveTests(TenantTestCase):
    def setUp(self):
        self.config = WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name, enabled=True, corp_id="synthetic-corp",
            agent_id=1, secret="synthetic-secret", identity_revision=4,
        )
        self.game = Game.objects.create(name="合成游戏")
        self.order = Order.objects.create(game=self.game, order_no="20261113000000000001")
        self.viewer = make_logged_in_client(username="archive-viewer", groups=("客服",), nodes=("order.view_detail",))
        # Read the logged-in account ID as JSON maps the site's account to the WeCom identity.
        self.viewer_id = str(self.viewer.session["_auth_user_id"])
        viewer_settings = override_settings(WECOM_ARCHIVE_VIEWERS={self.viewer_id: "synth-staff"})
        viewer_settings.enable()
        self.addCleanup(viewer_settings.disable)
        program_settings = override_settings(
            WECOM_ARCHIVE_TENANT_SCHEMA=connection.schema_name, WECOM_ARCHIVE_PROGRAM_ID="synthetic-program",
        )
        program_settings.enable()
        self.addCleanup(program_settings.disable)
        self.number = self.order.order_no
        self.base = f"/api/wecom/orders/{self.number}/"
        self.search_names = self.enterContext(mock.patch(
            "apps.wecom.archive_views.search_chat_ids", return_value=([], True),
        ))
        self.lookup_client = self.enterContext(mock.patch("apps.wecom.archive_views._contacts_client"))
        self.upstream_group = self.lookup_client.return_value.external_contact.get_group_chat_info
        self.upstream_group.side_effect = self.remote_group_details
        self.badges = self.enterContext(mock.patch("apps.wecom.archive_views.external_sender_badges", return_value={}))
        self.session_claim = {
            "user_id": self.viewer_id, "wecom_userid": "synth-staff", "corp_id": self.config.corp_id,
            "identity_revision": self.config.identity_revision, "tenant_schema": connection.schema_name,
            "agent_id": self.config.agent_id, "expires_at": (timezone.now() + timedelta(minutes=1)).timestamp(),
        }

    def remote_group_details(self, chat_id):
        group = WecomGroupChat.objects.filter(chat_id=chat_id).first()
        if group is None:
            return {"errcode": 84020}
        return {"errcode": 0, "group_chat": {"chat_id": chat_id, "name": group.name, **group.details}}

    def verify(self, **overrides):
        session = self.viewer.session
        session[ARCHIVE_VIEWER_SESSION_KEY] = {**self.session_claim, **overrides}
        session.save()

    def verify_order(self, **overrides):
        session = self.viewer.session
        claim = {**self.session_claim, "expires_at": int(self.session_claim["expires_at"])}
        session[ORDER_ARCHIVE_VIEWER_SESSION_KEY] = {**claim, **overrides}
        session.save()

    def set_legacy_source(self):
        Order.objects.filter(pk=self.order.pk).update(source_order_no="SYN-RENEW-02")
        self.order.refresh_from_db()

    def remember_group(self, chat_id, *, base=None):
        self.verify_order()
        base = base or self.base + "chat/"
        choice, = self.viewer.get(base + "groups/").json()["data"]["groups"]
        self.assertEqual(choice["chat_id"], chat_id)
        self.assertTrue(choice["remember"])
        result = self.viewer.post(base + "groups/", {"chat_id": chat_id}, content_type="application/json", headers={
            "X-Wecom-Chat-Proof": choice["proof"],
        }).json()
        self.assertEqual(result["code"], 0)

    def remember(self, chat_id, proof):
        return self.viewer.post(
            self.base + "chat/groups/", {"chat_id": chat_id}, content_type="application/json",
            headers={"X-Wecom-Chat-Proof": proof},
        ).json()

    def group(self, chat_id, name, **overrides):
        fields = {
            "identity_revision": self.config.identity_revision,
            "details": {"member_list": [{"type": 1, "userid": "synth-staff"}]},
        }
        return WecomGroupChat.objects.create(chat_id=chat_id, name=name, **{**fields, **overrides})

    def archived(self, msg_id, chat_id, **overrides):
        fields = {
            "identity_revision": self.config.identity_revision, "sent_at": timezone.now(),
            "encrypted_secret_key": "synthetic-encrypted-key", "public_key_version": 1,
        }
        return WecomArchiveMessage.objects.create(msg_id=msg_id, chat_id=chat_id, **{**fields, **overrides})

    def groups(self):
        return self.viewer.get(self.base + "chat/groups/").json()

    def messages(self, chat_id, *, proof="", **params):
        return self.viewer.get(
            self.base + "chat/messages/", {"chat_id": chat_id, **params}, headers={"X-Wecom-Chat-Proof": proof},
        )

    def test_order_tab_is_available_without_a_wecom_viewer_binding(self):
        self.group("available", f"试单 {self.number} 群")
        with override_settings(WECOM_ARCHIVE_VIEWERS={}):
            self.assertTrue(self.viewer.get(self.base + "groups/").json()["data"]["archive_available"])
            self.assertTrue(self.groups()["data"]["enabled"])
            self.assertFalse(self.viewer.get("/api/wecom/archive/access/").json()["data"]["enabled"])
        with override_settings(WECOM_ARCHIVE_PROGRAM_ID=""):
            self.assertNotIn("archive_available", self.viewer.get(self.base + "groups/").json()["data"])
            self.assertFalse(self.groups()["data"]["enabled"])

    def test_synced_hit_opens_for_an_unbound_order_viewer_while_group_details_are_pending(self):
        self.group("pending", "", pending=True, details={"owner": "another-employee"})
        self.archived("synced-source-mention", "pending")
        self.upstream_group.side_effect = ArchiveError("group details not ready")
        with override_settings(WECOM_ARCHIVE_VIEWERS={}), mock.patch(
            "apps.wecom.archive_views.search_message_ids", return_value=(["synced-source-mention"], True),
        ), mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"):
            found = self.groups()["data"]
            self.assertTrue(found["complete"])
            candidate, = found["groups"]
            self.assertEqual((candidate["chat_id"], candidate["match"]), ("pending", "message"))
            self.assertNotIn("requires_confirmation", candidate)
            self.assertTrue(candidate["remember"])
            proof = candidate["proof"]
            self.assertNotEqual(self.messages("pending").json()["code"], 0)
            self.assertTrue(self.messages("pending", proof=proof).json()["data"]["requires_login"])
            self.assertNotEqual(self.remember("pending", proof)["code"], 0)
            self.verify_order(wecom_userid="another-workspace-employee")
            visible = self.messages("pending", proof=proof).json()["data"]
            self.assertEqual([row["msgid"] for row in visible["messages"]], ["synced-source-mention"])
            self.assertEqual(self.remember("pending", proof)["code"], 0)
            self.assertEqual(self.messages("pending").json()["data"]["messages"], visible["messages"])
            WecomGroupChat.objects.filter(chat_id="pending").update(pending_dismissal=True)
            self.assertNotEqual(self.messages("pending").json()["code"], 0)
        self.upstream_group.assert_not_called()

    def test_opened_original_and_renewal_remember_same_group_after_rename(self):
        original = Order.objects.create(
            game=self.game, order_no="20261113000000000009", source_order_no="SYN-ORIGINAL",
        )
        original_base = f"/api/wecom/orders/{original.order_no}/chat/"
        group = self.group("continuing-group", f"群 {original.order_no}", details={"owner": "other-staff"})
        self.archived("original-history", group.chat_id)
        self.upstream_group.side_effect = self.remote_group_details
        self.verify_order()

        def open_chat(base, *, proof=""):
            return self.viewer.get(base + "messages/", {"chat_id": group.chat_id}, headers={
                "X-Wecom-Chat-Proof": proof,
            }).json()

        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"):
            self.remember_group(group.chat_id, base=original_base)
            self.assertEqual(open_chat(original_base)["data"]["messages"][0]["msgid"], "original-history")
            WecomGroupChat.objects.filter(pk=group.pk).update(name=f"群 {self.number}")
            self.remember_group(group.chat_id)
            self.assertEqual(open_chat(self.base + "chat/")["data"]["messages"][0]["msgid"], "original-history")
            WecomGroupChat.objects.filter(pk=group.pk).update(name="此群的新昵称")
            with mock.patch("apps.wecom.archive_views.search_message_ids", return_value=([], True)) as search:
                for base in (original_base, self.base + "chat/"):
                    found = self.viewer.get(base + "groups/").json()["data"]["groups"]
                    self.assertEqual([(row["chat_id"], row["match"]) for row in found], [(group.chat_id, "linked")])
                    read = open_chat(base, proof=found[0]["proof"])
                    self.assertEqual(read["data"]["messages"][0]["msgid"], "original-history")
                search.assert_not_called()

            WecomGroupChat.objects.filter(pk=group.pk).update(dismissed=True)
            self.assertNotEqual(open_chat(original_base)["code"], 0)

    def test_remembering_an_unambiguous_legacy_hit_requires_csrf_and_preserves_rename_access(self):
        self.set_legacy_source()
        group = self.group("legacy-pending", "", pending=True, details={"owner": "other-staff"})
        self.archived("legacy-message", group.chat_id)
        self.upstream_group.side_effect = ArchiveError("details not ready")

        def search(config, number, **kwargs):
            return (["legacy-message"] if number == self.order.source_order_no else []), True

        with override_settings(WECOM_ARCHIVE_VIEWERS={}), mock.patch(
            "apps.wecom.archive_views.search_message_ids", side_effect=search,
        ), mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"):
            payload = self.groups()["data"]
            self.assertTrue(payload["complete"])
            candidate, = payload["groups"]
            self.assertEqual((candidate["chat_id"], candidate["remember"]), (group.chat_id, True))
            self.verify_order(wecom_userid="unbound-employee")
            proof = candidate["proof"]
            history = self.messages(group.chat_id, proof=proof).json()["data"]["messages"]
            self.assertEqual([row["msgid"] for row in history], ["legacy-message"])

            csrf_enforced = Client(SERVER_NAME="tenant.test.com", enforce_csrf_checks=True)
            csrf_enforced.cookies = self.viewer.cookies.copy()
            no_csrf = csrf_enforced.post(
                self.base + "chat/groups/", {"chat_id": group.chat_id}, content_type="application/json",
                headers={"X-Wecom-Chat-Proof": proof},
            )
            self.assertEqual(no_csrf.status_code, 403)
            self.assertFalse(WecomArchiveOrderGroup.objects.exists())
            for _ in range(2):
                self.assertEqual(self.remember(group.chat_id, proof)["data"], {"chat_id": group.chat_id})

            association = WecomArchiveOrderGroup.objects.get()
            event = OperationLog.objects.get(action="order.wecom_chat_link", order_no=self.number)
            self.assertEqual(event.to_value, str(association.pk))
            WecomGroupChat.objects.filter(pk=group.pk).update(name="改名后的群", pending=False)
            linked, = self.groups()["data"]["groups"]
            self.assertEqual((linked["match"], linked["name"]), ("linked", "改名后的群"))
            self.assertEqual(self.messages(group.chat_id).json()["data"]["messages"], history)
            Order.objects.filter(pk=self.order.pk).update(source_order_no="DIFFERENT-SOURCE")
            self.assertNotEqual(self.messages(group.chat_id).json()["code"], 0)
        self.upstream_group.assert_not_called()

    def test_legacy_name_ignores_recycled_duplicates_and_rejects_live_duplicates(self):
        self.set_legacy_source()
        source = self.order.source_order_no
        group = self.group("legacy-name", f"客户群 {source}")
        self.group("legacy-partial", f"客户群 {source}-OTHER")
        self.archived("legacy-history", group.chat_id)
        self.verify()
        other = Order.objects.create(game=self.game, order_no="20261113000000000003", source_order_no=f"\t{source}　")
        self.enterContext(mock.patch("apps.wecom.archive_views.search_message_ids", return_value=([], True)))
        self.assertEqual(self.groups()["data"]["groups"], [])
        Order.objects.filter(pk=other.pk).update(deleted_at=timezone.now())
        candidates = self.groups()["data"]["groups"]
        self.assertEqual([item["chat_id"] for item in candidates], [group.chat_id])
        candidate, = candidates
        self.assertTrue(candidate["remember"])
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"):
            visible = self.messages(group.chat_id, proof=candidate["proof"]).json()["data"]
        self.assertEqual([message["msgid"] for message in visible["messages"]], ["legacy-history"])
        second = self.group("another-legacy-name", f"另一个客户群 {source}")
        ambiguous = self.groups()["data"]["groups"]
        self.assertEqual({row["chat_id"] for row in ambiguous}, {group.chat_id, second.chat_id})
        self.assertTrue(all(row["proof"] and not row.get("remember") for row in ambiguous))
        for row in ambiguous:
            self.assertNotEqual(self.remember(row["chat_id"], row["proof"])["code"], 0)
        self.assertNotEqual(self.remember(second.chat_id, candidate["proof"])["code"], 0)
        self.assertFalse(WecomArchiveOrderGroup.objects.exists())

    def test_direct_match_rechecks_eligible_names_without_searching_messages(self):
        self.group("match", f"试单 {self.number} 群")
        self.group("owner", f"群 {self.number} 老板", details={"owner": "synth-staff"})
        self.group("dismissed", f"{self.number} 已解散", dismissed=True)
        self.group("pending", f"{self.number} 待刷新", pending=True)
        self.group("old-config", f"{self.number} 历史企业", identity_revision=3)
        self.group("external", f"{self.number} 非员工", details={"member_list": [{"type": 2, "userid": "synth-staff"}]})
        with mock.patch("apps.wecom.archive_views.search_message_ids") as search:
            body = self.groups()
        search.assert_not_called()
        self.search_names.assert_called_once()
        self.assertEqual(body["code"], 0)
        self.assertEqual({x["chat_id"] for x in body["data"]["groups"]}, {"match", "owner", "external"})
        self.assertTrue(all(x["proof"] and x["match"] == "name" and not x.get("remember") for x in body["data"]["groups"]))

    def test_order_history_does_not_require_admin_or_group_membership_even_before_remembering(self):
        self.group("outside", f"客户群 {self.number}", details={"owner": "someone-else"})
        self.archived("outside-history", "outside")

        with override_settings(WECOM_ARCHIVE_VIEWERS={}), mock.patch(
            "apps.wecom.archive_views.decrypt_message_key", return_value="display-key",
        ):
            candidate, = self.groups()["data"]["groups"]
            self.assertEqual(candidate["chat_id"], "outside")
            self.verify_order(wecom_userid="unbound-employee")
            visible = self.messages("outside", proof=candidate["proof"]).json()
            self.assertEqual([row["msgid"] for row in visible["data"]["messages"]], ["outside-history"])
            self.assertFalse(WecomArchiveOrderGroup.objects.exists())

    def test_independent_search_without_saved_order_verifies_identity_and_proof(self):
        keyword = "无订单合成关键词"
        self.assertFalse(Order.objects.filter(source_order_no=keyword).exists())
        self.group("keyword-hit", "旧群没有改名")
        self.group("unrelated", "无关群", details={"owner": "someone-else"})
        self.archived("keyword-message", "keyword-hit")
        self.archived("unrelated-message", "unrelated")
        search_url = "/api/wecom/archive/search/"
        messages_url = "/api/wecom/archive/messages/"

        with mock.patch(
            "apps.wecom.archive_search.search_message_ids",
            return_value=(["unrelated-message", "keyword-message"], True),
        ) as search, mock.patch("apps.wecom.archive_search.search_chat_ids", return_value=([], True)):
            unauthenticated = self.viewer.post(search_url, {"q": keyword}, content_type="application/json")
            self.assertEqual(unauthenticated.status_code, 200)
            self.assertTrue(unauthenticated.json()["data"]["requires_login"])
            search.assert_not_called()
            self.verify()
            response = self.viewer.post(search_url, {"q": keyword}, content_type="application/json")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response.json()["code"], 0)
        groups = response.json()["data"]["groups"]
        self.assertEqual([(g["chat_id"], g["match"]) for g in groups], [("keyword-hit", "message")])
        self.assertEqual(groups[0]["anchor_msgid"], "keyword-message")

        proof = groups[0]["proof"]
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key") as decrypt:
            self.assertNotEqual(self.viewer.get(messages_url, {"chat_id": "keyword-hit"}).json()["code"], 0)
            self.assertNotEqual(self.viewer.get(
                messages_url, {"chat_id": "unrelated"}, headers={"X-Wecom-Chat-Proof": proof},
            ).json()["code"], 0)
            history = self.viewer.get(
                messages_url, {"chat_id": "keyword-hit", "around": "keyword-message"},
                headers={"X-Wecom-Chat-Proof": proof},
            ).json()["data"]
        self.assertEqual([item["msgid"] for item in history["messages"]], ["keyword-message"])
        decrypt.assert_called_once()

    def test_independent_search_does_not_reveal_hits_outside_membership(self):
        self.group("outside-scope", "不在当前成员范围", details={"owner": "someone-else"})
        self.archived("hidden-hit", "outside-scope")
        self.verify()

        with mock.patch("apps.wecom.archive_search.search_chat_ids", return_value=([], True)), mock.patch(
            "apps.wecom.archive_search.search_message_ids",
            side_effect=[([], True), (["hidden-hit", "not-in-local-index"], True)],
        ):
            results = [
                self.viewer.post("/api/wecom/archive/search/", {"q": "合成关键字"}, content_type="application/json").json()
                for _ in range(2)
            ]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["data"]["groups"], [])

    def test_independent_search_checks_known_member_groups_first(self):
        self.verify()
        self.group("known-member", "旧群")
        self.archived("member-hit", "known-member")
        for n in range(2):
            group_id = f"outside-{n}"
            self.group(group_id, "别的工作群", details={"owner": "someone-else"})
            self.archived(f"outside-hit-{n}", group_id)
        with mock.patch("apps.wecom.archive_search.search_chat_ids", return_value=([], True)), mock.patch(
            "apps.wecom.archive_search.search_message_ids",
            return_value=(["outside-hit-0", "outside-hit-1", "member-hit"], True),
        ), mock.patch("apps.wecom.archive_search._GROUP_LIMIT", 2):
            found = self.viewer.post(
                "/api/wecom/archive/search/", {"q": "合成关键词"}, content_type="application/json",
            ).json()["data"]["groups"]
        self.assertEqual([row["chat_id"] for row in found], ["known-member"])

    def test_independent_search_uses_inclusive_date_range_for_message_hits(self):
        self.verify()
        start_date = timezone.localdate() - timedelta(days=65)
        end_date = start_date + timedelta(days=30)
        requested = {"q": "合成关键字", "start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
        url = "/api/wecom/archive/search/"
        with mock.patch("apps.wecom.archive_search.search_chat_ids", return_value=([], True)), mock.patch(
            "apps.wecom.archive_search.search_message_ids", return_value=([], True),
        ) as search:
            self.assertEqual(self.viewer.post(url, requested, content_type="application/json").json()["code"], 0)
            too_wide = {**requested, "end_date": (end_date + timedelta(days=1)).isoformat()}
            response = self.viewer.post(url, too_wide, content_type="application/json")
        self.assertNotEqual(response.json()["code"], 0)
        search.assert_called_once()
        span = search.call_args.kwargs
        self.assertEqual(span["end_time"] - span["start_time"], 31 * 86400)

    def test_independent_chat_opens_at_matched_message_and_pages_both_directions(self):
        self.group("preview-group", "原来的群")
        now = timezone.now()
        earliest = now - timedelta(minutes=10)
        for index in range(7):
            self.archived(f"synthetic-history-{index}", "preview-group", sent_at=earliest + timedelta(minutes=index))
        self.verify()
        headers = {}
        with mock.patch("apps.wecom.archive_search.search_chat_ids", return_value=([], True)), mock.patch(
            "apps.wecom.archive_search.search_message_ids", return_value=(["synthetic-history-3"], True),
        ):
            response = self.viewer.post(
                "/api/wecom/archive/search/", {"q": "合成关键字"}, content_type="application/json",
            ).json()
            found = response["data"]["groups"][0]
            headers["X-Wecom-Chat-Proof"] = found["proof"]

        messages_url = "/api/wecom/archive/messages/"
        base = {"chat_id": "preview-group"}
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="synth-key"), mock.patch(
            "apps.wecom.archive_search._MESSAGE_PAGE_SIZE", 3,
        ), mock.patch("apps.wecom.archive_views._MESSAGE_PAGE_SIZE", 3):
            invalid = self.viewer.get(messages_url, {**base, "around": "synthetic-history-4"}, headers=headers).json()
            self.assertNotEqual(invalid["code"], 0)
            middle = self.viewer.get(messages_url, {**base, "around": found["anchor_msgid"]}, headers=headers).json()["data"]
            older = self.viewer.get(messages_url, {**base, "before": middle["next_before"]}, headers=headers).json()["data"]
            newer = self.viewer.get(messages_url, {**base, "after": middle["next_after"]}, headers=headers).json()["data"]
        self.assertEqual([row["msgid"] for row in middle["messages"]], [
            "synthetic-history-4", "synthetic-history-3", "synthetic-history-2",
        ])
        self.assertEqual([row["msgid"] for row in older["messages"]], ["synthetic-history-1", "synthetic-history-0"])
        self.assertEqual([row["msgid"] for row in newer["messages"]], ["synthetic-history-6", "synthetic-history-5"])

    def test_independent_search_proof_cannot_move_between_tenants(self):
        self.group("shared-chat-id", "合成可查群")
        self.archived("tenant-a-visible-text", "shared-chat-id")
        self.verify()
        with mock.patch("apps.wecom.archive_search.search_chat_ids", return_value=([], True)), mock.patch(
            "apps.wecom.archive_search.search_message_ids", return_value=([], True),
        ):
            query = {"q": "合成可查群"}
            search_url = "/api/wecom/archive/search/"
            messages_url = "/api/wecom/archive/messages/"
            source = self.viewer.post(search_url, query, content_type="application/json").json()["data"]["groups"][0]
            other, _ = provision_tenant(
                schema_name="wecom_search_b", name="合成工作室乙", domain="wecom-search-b.test.local",
            )
            with tenant_context(other), override_settings(ALLOWED_HOSTS=["wecom-search-b.test.local"]):
                WecomAppConfig.objects.create(
                    tenant_schema=other.schema_name, enabled=True, corp_id=self.config.corp_id,
                    agent_id=self.config.agent_id, secret="synthetic-secret", identity_revision=self.config.identity_revision,
                )
                self.group("shared-chat-id", "合成可查群")
                self.archived("tenant-b-hidden-text", "shared-chat-id")
                authorized = make_logged_in_client(domain="wecom-search-b.test.local", is_superuser=True)
                other_user_id = authorized.session["_auth_user_id"]
                session = authorized.session
                session[ARCHIVE_VIEWER_SESSION_KEY] = {
                    **self.session_claim, "tenant_schema": other.schema_name, "user_id": other_user_id,
                }
                session.save()
                with override_settings(
                    WECOM_ARCHIVE_TENANT_SCHEMA=other.schema_name,
                    WECOM_ARCHIVE_VIEWERS={other_user_id: "synth-staff"},
                ), mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="synth-key"):
                    moved = authorized.get(messages_url, {"chat_id": "shared-chat-id"}, headers={
                        "X-Wecom-Chat-Proof": source["proof"],
                    }).json()
                    self.assertNotEqual(moved["code"], 0)
                    self.assertNotIn("tenant-b-hidden-text", str(moved))
                    own = authorized.post(search_url, query, content_type="application/json").json()["data"]["groups"][0]
                    control = authorized.get(messages_url, {"chat_id": "shared-chat-id"}, headers={
                        "X-Wecom-Chat-Proof": own["proof"],
                    }).json()["data"]
                    self.assertEqual([row["msgid"] for row in control["messages"]], ["tenant-b-hidden-text"])

    def test_system_number_only_matches_the_whole_token(self):
        self.group("prefixed", f"另一单 A{self.number}")
        self.group("suffixed", f"另一个订单 {self.number}7")
        self.group("right", f"团团通{self.number}客户群")
        with mock.patch("apps.wecom.archive_views.search_message_ids") as search:
            body = self.groups()
        search.assert_not_called()
        self.assertEqual([item["chat_id"] for item in body["data"]["groups"]], ["right"])
        self.verify_order()
        self.assertNotEqual(self.messages("prefixed").json()["code"], 0)

    def test_historical_group_name_proof_denied_if_never_opened_and_already_renamed(self):
        self.search_names.return_value = (["historical", "partial"], True)
        details = {
            "historical": {"chat_id": "historical", "name": f"历史订单群 {self.number}", "owner": "other-staff"},
            "partial": {"chat_id": "partial", "name": f"其他订单群 1{self.number}", "owner": "synth-staff"},
        }
        self.upstream_group.side_effect = lambda chat_id: {"errcode": 0, "group_chat": details[chat_id]}
        self.archived("historical-original-message", "historical")
        with mock.patch("apps.wecom.archive_views.search_message_ids") as search:
            payload = self.groups()["data"]
        self.assertTrue(payload["complete"])
        self.assertEqual([(row["chat_id"], row["match"]) for row in payload["groups"]], [("historical", "name")])
        self.assertEqual(self.search_names.call_args.args, (self.config, self.number))
        search.assert_not_called()
        self.assertFalse(WecomGroupChat.objects.exists())

        proof = payload["groups"][0]["proof"]
        self.assertTrue(proof)
        self.verify_order()
        details["historical"]["name"] = "已改名的群 34567"
        with mock.patch("apps.wecom.archive_views.decrypt_message_key") as decrypt, mock.patch(
            "apps.wecom.archive_views.search_message_ids", return_value=([], True),
        ):
            self.assertNotEqual(self.messages("historical", proof=proof).json()["code"], 0)
            self.assertNotEqual(self.remember("historical", proof)["code"], 0)
        decrypt.assert_not_called()

    def test_empty_near_search_widens_and_finds_message_four_days_after_creation(self):
        now = timezone.now()
        created_at = now - timedelta(days=5)
        mention_at = int((created_at + timedelta(days=4)).timestamp())
        Order.objects.filter(pk=self.order.pk).update(created_at=created_at)
        self.group("original", "历史原单群")
        self.archived("day-four-mention", "original")

        def search_by_time(config, source, *, start_time, end_time, deadline):
            self.assertLessEqual(end_time - start_time, 31 * 86400)
            return (["day-four-mention"] if start_time <= mention_at <= end_time else []), True

        with mock.patch("apps.wecom.archive_views.search_message_ids", side_effect=search_by_time) as search:
            payload = self.groups()["data"]
        self.assertEqual(search.call_count, 2)
        near, wide = [entry.kwargs for entry in search.call_args_list]
        self.assertLess(near["end_time"], mention_at)
        self.assertLessEqual(wide["start_time"], near["start_time"])
        self.assertGreaterEqual(wide["end_time"], mention_at)
        self.assertTrue(payload["complete"])
        self.assertEqual([(row["chat_id"], row["match"]) for row in payload["groups"]], [("original", "message")])

    def test_widened_empty_search_reports_partial_work_on_timeout(self):
        with mock.patch(
            "apps.wecom.archive_views.search_message_ids", side_effect=[([], True), ArchiveError("time spent")],
        ) as search:
            payload = self.groups()["data"]
        self.assertEqual(search.call_count, 2)
        self.assertEqual(payload["groups"], [])
        self.assertFalse(payload["complete"])

    def test_expired_budget_after_near_search_does_not_report_complete(self):
        with mock.patch(
            "apps.wecom.archive_views.search_message_ids", return_value=([], True),
        ) as search, mock.patch("apps.wecom.archive_views.monotonic", side_effect=[1, 1, 1, 100]):
            payload = self.groups()["data"]
        search.assert_called_once()
        self.assertEqual(payload["groups"], [])
        self.assertFalse(payload["complete"])

    def test_official_name_search_rechecks_stale_snapshot_but_ignores_pending_dismissal(self):
        self.group("new-owner", "未更新的旧群名", details={"owner": "former-owner"})
        self.group("pending", f"待解散群 {self.number}", pending_dismissal=True)
        self.search_names.return_value = (["new-owner", "pending"], True)
        self.upstream_group.side_effect = lambda chat_id: {
            "errcode": 0, "group_chat": {"chat_id": chat_id, "name": f"历史订单群 {self.number}", "owner": "synth-staff"},
        }
        self.archived("current-owner-message", "new-owner")
        payload = self.groups()["data"]
        self.assertEqual([row["chat_id"] for row in payload["groups"]], ["new-owner"])
        self.upstream_group.assert_called_once_with("new-owner")
        self.verify()
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"):
            self.assertEqual(self.remember("new-owner", payload["groups"][0]["proof"])["code"], 0)
            message_page = self.messages("new-owner").json()
        self.assertEqual(message_page["data"]["messages"][0]["msgid"], "current-owner-message")

    def test_stale_cached_title_does_not_hide_a_different_synced_chat(self):
        self.group("stale", f"旧订单群 {self.number}", details={"name": "已经改成其他订单"})
        self.search_names.return_value = (["stale"], True)
        self.archived("synced-order-hit", "new-group")
        with mock.patch(
            "apps.wecom.archive_views.search_message_ids", return_value=(["synced-order-hit"], True),
        ), mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"):
            candidate, = self.groups()["data"]["groups"]
            self.assertEqual((candidate["chat_id"], candidate["match"]), ("new-group", "message"))
            self.verify_order()
            page = self.messages("new-group", proof=candidate["proof"]).json()["data"]
        self.assertEqual([row["msgid"] for row in page["messages"]], ["synced-order-hit"])
        self.upstream_group.assert_called_once_with("stale")

    def test_live_system_title_wins_before_lower_priority_source_message(self):
        self.set_legacy_source()
        self.group("live-title", "本地还没有群名", details={"name": f"线上订单群 {self.number}"})
        self.archived("misleading-source-hit", "source-mention-chat")
        self.search_names.side_effect = lambda config, number, **kwargs: (
            ["live-title"] if number == self.number else [], True,
        )
        with mock.patch(
            "apps.wecom.archive_views.search_message_ids", return_value=(["misleading-source-hit"], True),
        ) as search:
            candidate, = self.groups()["data"]["groups"]
        self.assertEqual((candidate["chat_id"], candidate["match"]), ("live-title", "name"))
        search.assert_not_called()

    def test_verified_cached_name_is_viewable_but_cannot_be_saved_during_remote_name_search_outage(self):
        self.group("cached", f"同步过的订单群 {self.number}")
        self.archived("cached-chat-message", "cached")
        self.search_names.side_effect = ArchiveError("name lookup unavailable")
        result = self.groups()["data"]
        self.assertFalse(result["complete"])
        candidate, = result["groups"]
        self.assertEqual(candidate["chat_id"], "cached")
        self.assertFalse(candidate.get("remember"))
        self.verify_order()
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"):
            messages = self.messages("cached", proof=candidate["proof"]).json()["data"]["messages"]
        self.assertEqual([row["msgid"] for row in messages], ["cached-chat-message"])

    def test_a_source_keyword_cannot_match_chat_titles_or_messages(self):
        keyword = "合成客服对接"
        Order.objects.filter(pk=self.order.pk).update(source_order_no=keyword)
        self.group("unrelated-title", f"{keyword}群")
        self.archived("unrelated-keyword-hit", "unrelated-support-group")

        def search(config, number, **kwargs):
            return (["unrelated-keyword-hit"] if number == keyword else []), True

        with mock.patch("apps.wecom.archive_views.search_message_ids", side_effect=search) as lookup:
            result = self.groups()["data"]
        self.assertEqual(result["groups"], [])
        self.assertTrue(lookup.called)
        self.assertTrue(all(call.args[1] == self.number for call in lookup.call_args_list))

    def test_saved_legacy_link_is_disabled_if_source_number_becomes_duplicated(self):
        legacy = "历史自定义单号"
        Order.objects.filter(pk=self.order.pk).update(source_order_no=legacy)
        self.group("legacy-saved", "已改过的群名")
        self.archived("legacy-content", "legacy-saved")
        WecomArchiveOrderGroup.objects.create(
            order=self.order, config=self.config, identity_revision=self.config.identity_revision,
            program_id="synthetic-program", matched_order_no=legacy, chat_id="legacy-saved",
        )
        self.verify_order()
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"), mock.patch(
            "apps.wecom.archive_views.search_message_ids", return_value=([], True),
        ):
            available = self.messages("legacy-saved").json()["data"]["messages"]
            self.assertEqual([row["msgid"] for row in available], ["legacy-content"])
            Order.objects.create(game=self.game, order_no="20261113000000000002", source_order_no=f" {legacy} ")
            self.assertNotEqual(self.messages("legacy-saved").json()["code"], 0)
            self.assertEqual(self.groups()["data"]["groups"], [])

    def test_unavailable_group_name_search_does_not_prevent_message_search(self):
        self.search_names.side_effect = ArchiveError("group name search failed")
        self.group("original", "历史原单群")
        self.archived("known-mention", "original")
        with mock.patch("apps.wecom.archive_views.search_message_ids", return_value=(["known-mention"], True)):
            payload = self.groups()["data"]
        self.assertFalse(payload["complete"])
        candidate, = payload["groups"]
        self.assertEqual(candidate["chat_id"], "original")
        self.assertTrue(candidate["proof"])
        self.assertFalse(candidate.get("remember"))

    def test_wide_message_search_keeps_near_hit_and_disallows_automatic_remember_when_ambiguous(self):
        self.archived("near-match", "near")
        self.archived("wide-match", "wide")
        with mock.patch(
            "apps.wecom.archive_views.search_message_ids",
            side_effect=[(["near-match"], True), (["near-match", "wide-match"], True)],
        ):
            result = self.groups()["data"]
        self.assertTrue(result["complete"])
        self.assertEqual({row["chat_id"] for row in result["groups"]}, {"near", "wide"})
        self.assertTrue(all(row["proof"] and not row.get("remember") for row in result["groups"]))

    def test_stale_title_can_use_synced_message_only_for_the_same_chat(self):
        self.group("archived-chat", f"试点群 {self.number}")
        self.archived("order-hit", "archived-chat")
        self.archived("other-chat-hit", "other-chat")
        candidate, = self.groups()["data"]["groups"]
        proof = candidate["proof"]
        self.verify_order()
        self.upstream_group.side_effect = ArchiveError("group lookup unavailable")
        with mock.patch("apps.wecom.archive_views.search_message_ids", return_value=(["other-chat-hit"], True)):
            self.assertNotEqual(self.messages("archived-chat", proof=proof).json()["code"], 0)
        with mock.patch(
            "apps.wecom.archive_views.search_message_ids", return_value=(["order-hit"], True),
        ), mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"):
            visible = self.messages("archived-chat", proof=proof).json()["data"]["messages"]
            self.assertEqual([row["msgid"] for row in visible], ["order-hit"])
            self.assertNotEqual(self.remember("archived-chat", proof)["code"], 0)

    def test_renewal_finds_known_chats_via_message_ids_and_reports_partial_index(self):
        self.group("original", "原订单群 SYN-ORIGINAL")
        self.group("another", "另一个原订单群")
        self.group("outside", "非本人群", details={"owner": "another-employee"})
        self.archived("renewed-mention", "original")
        self.archived("renewed-confirm", "original")
        self.archived("another-mention", "another")
        self.archived("outside-mention", "outside")
        self.archived("old-message", "original", identity_revision=3)
        with mock.patch(
            "apps.wecom.archive_views.search_message_ids",
            return_value=(["renewed-mention", "renewed-confirm", "another-mention", "outside-mention", "old-message"], True),
        ) as search:
            body = self.groups()["data"]
        args, kwargs = search.call_args_list[0]
        self.assertEqual(args, (self.config, self.number))
        self.assertLess(kwargs["start_time"], self.order.created_at.timestamp())
        self.assertGreaterEqual(kwargs["end_time"], int(self.order.created_at.timestamp()))
        self.assertLessEqual(kwargs["end_time"] - kwargs["start_time"], 6 * 86400)
        self.assertFalse(body["complete"])
        self.assertEqual({item["chat_id"] for item in body["groups"]}, {"original", "another", "outside"})
        self.assertTrue(all(
            item["match"] == "message" and item["proof"] and not item.get("remember")
            for item in body["groups"]
        ))
        self.verify_order()
        selected = next(row for row in body["groups"] if row["chat_id"] == "original")
        self.assertNotEqual(self.remember("original", selected["proof"])["code"], 0)
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"):
            visible = self.messages("original", proof=selected["proof"]).json()["data"]["messages"]
        self.assertEqual({row["msgid"] for row in visible}, {"renewed-mention", "renewed-confirm"})
        self.upstream_group.assert_not_called()

    def test_historical_group_opens_from_synced_message_without_local_group_snapshot(self):
        self.archived("historical-mention", "historical-chat")
        self.upstream_group.side_effect = ArchiveError("group details unavailable")
        with mock.patch("apps.wecom.archive_views.search_message_ids", return_value=(["historical-mention"], True)):
            payload = self.groups()["data"]
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["groups"][0]["chat_id"], "historical-chat")
        proof = payload["groups"][0]["proof"]
        self.assertFalse(WecomGroupChat.objects.filter(chat_id="historical-chat").exists())

        self.verify_order()
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key") as decrypt:
            self.assertEqual(self.remember("historical-chat", proof)["code"], 0)
            body = self.messages("historical-chat").json()
            self.assertEqual(body["data"]["messages"][0]["msgid"], "historical-mention")
            self.assertEqual(self.messages("historical-chat").json()["code"], 0)
        self.upstream_group.assert_not_called()
        self.assertEqual(decrypt.call_count, 2)

    def test_indexed_hit_needs_no_remote_group_budget(self):
        self.archived("historical-mention", "historical-chat")
        with mock.patch("apps.wecom.archive_views.search_message_ids", return_value=(["historical-mention"], True)):
            with mock.patch("apps.wecom.archive_views.monotonic", side_effect=[1, 1, 1, 100, 100]):
                payload = self.groups()["data"]
        self.assertFalse(payload["complete"])
        known, = payload["groups"]
        self.assertEqual(known["match"], "message")
        self.assertTrue(known["proof"])
        self.assertFalse(known.get("remember"))
        self.upstream_group.assert_not_called()

    def test_remote_title_change_vetoes_still_valid_local_snapshot(self):
        self.group("match", f"试点群 {self.number}")
        self.archived("must-stay-secret", "match")
        proof = self.groups()["data"]["groups"][0]["proof"]
        self.upstream_group.side_effect = None
        self.upstream_group.return_value = {
            "errcode": 0, "group_chat": {"chat_id": "match", "name": "另一张续单", "owner": "someone-else"},
        }
        self.verify_order()
        with mock.patch("apps.wecom.archive_views.decrypt_message_key") as decrypt, mock.patch(
            "apps.wecom.archive_views.search_message_ids", return_value=([], True),
        ):
            self.assertNotEqual(self.messages("match", proof=proof).json()["code"], 0)
            self.assertNotEqual(self.remember("match", proof)["code"], 0)
        decrypt.assert_not_called()

    def test_outage_not_misreported_as_empty_results(self):
        with mock.patch("apps.wecom.archive_views.search_message_ids", side_effect=ArchiveError("secret details")):
            body = self.groups()
        self.assertNotEqual(body["code"], 0)
        self.assertNotIn("secret details", body["msg"])

    def test_message_proof_is_scoped_to_order_chat_and_synced_anchor(self):
        self.group("original", "原订单群")
        self.group("other-chat", "另一个工作群")
        self.archived("mention", "original")
        with mock.patch("apps.wecom.archive_views.search_message_ids", return_value=(["mention"], True)):
            proof = self.groups()["data"]["groups"][0]["proof"]
        self.verify()
        self.assertNotEqual(self.messages("original").json()["code"], 0)
        self.assertNotEqual(self.messages("other-chat", proof=proof).json()["code"], 0)
        other_order = Order.objects.create(
            game=self.game, order_no="20261113000000000002", source_order_no=self.order.source_order_no,
        )
        denied = self.viewer.post(
            f"/api/wecom/orders/{other_order.order_no}/chat/groups/", {"chat_id": "original"},
            content_type="application/json", headers={"X-Wecom-Chat-Proof": proof},
        )
        self.assertNotEqual(denied.json()["code"], 0)
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key") as decrypt:
            self.assertEqual(self.remember("original", proof)["code"], 0)
            allowed = self.messages("original").json()
        decrypt.assert_called_once()
        self.assertEqual(allowed["data"]["messages"][0]["msgid"], "mention")
        WecomArchiveOrderGroup.objects.all().delete()
        WecomArchiveMessage.objects.filter(msg_id="mention").delete()
        self.assertNotEqual(self.remember("original", proof)["code"], 0)

    def test_order_chat_proof_cannot_be_reused_in_another_session_for_the_same_employee(self):
        self.group("session-chat", f"试单 {self.number} 群")
        self.archived("session-chat-content", "session-chat")
        candidate, = self.groups()["data"]["groups"]
        browser = Client(SERVER_NAME="tenant.test.com")
        logged_in = browser.post(
            "/api/auth/login", {"username": "archive-viewer", "password": P2_TEST_PASSWORD},
            content_type="application/json",
        )
        self.assertEqual(logged_in.json()["code"], 0)
        self.assertEqual(browser.session["_auth_user_id"], self.viewer_id)
        self.assertNotEqual(browser.session.session_key, self.viewer.session.session_key)
        session = browser.session
        session[ORDER_ARCHIVE_VIEWER_SESSION_KEY] = {
            **self.session_claim, "expires_at": int(self.session_claim["expires_at"]),
        }
        session.save()
        control, = browser.get(self.base + "chat/groups/").json()["data"]["groups"]
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"):
            stolen = browser.get(
                self.base + "chat/messages/", {"chat_id": "session-chat"},
                headers={"X-Wecom-Chat-Proof": candidate["proof"]},
            ).json()
            own = browser.get(
                self.base + "chat/messages/", {"chat_id": "session-chat"},
                headers={"X-Wecom-Chat-Proof": control["proof"]},
            ).json()
        self.assertNotEqual(stolen["code"], 0)
        self.assertEqual([row["msgid"] for row in own["data"]["messages"]], ["session-chat-content"])

    def test_order_chat_read_proof_stays_usable_for_paging_after_the_remember_window(self):
        self.group("session-chat", f"试单 {self.number} 群")
        now = timezone.now()
        self.archived("older-message", "session-chat", sent_at=now - timedelta(minutes=5))
        self.archived("latest-message", "session-chat", sent_at=now)
        older_timestamp = signing.b62_encode(int(now.timestamp()) - 20 * 60)
        with mock.patch("django.core.signing.TimestampSigner.timestamp", return_value=older_timestamp):
            candidate, = self.groups()["data"]["groups"]
        self.verify_order()
        proof = candidate["proof"]
        self.assertNotEqual(self.remember("session-chat", proof)["code"], 0)
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"), mock.patch(
            "apps.wecom.archive_views._MESSAGE_PAGE_SIZE", 1,
        ):
            page = self.messages("session-chat", proof=proof).json()["data"]
            older = self.messages("session-chat", proof=proof, before=page["next_before"]).json()["data"]
        self.assertEqual([row["msgid"] for row in page["messages"]], ["latest-message"])
        self.assertEqual([row["msgid"] for row in older["messages"]], ["older-message"])

    def test_display_key_requires_native_same_corp_login_and_response_is_not_cacheable(self):
        self.group("match", f"试点群 {self.number}")
        self.remember_group("match")
        self.verify_order(expires_at=1)
        row = self.archived("display-id", "match")
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key") as decrypt:
            self.assertTrue(self.messages("match").json()["data"]["requires_login"])
            self.verify_order(corp_id="another-corp")
            self.assertTrue(self.messages("match").json()["data"]["requires_login"])
            self.verify_order(identity_revision=3)
            self.assertTrue(self.messages("match").json()["data"]["requires_login"])
            self.verify_order(expires_at=1)
            self.assertTrue(self.messages("match").json()["data"]["requires_login"])
            self.verify_order()
            response = self.messages("match")
        decrypt.assert_called_once_with(row)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response.json()["data"]["messages"], [
            {"msgid": "display-id", "secret_key": "display-key", "sent_at": row.sent_at.isoformat()},
        ])

    def test_sync_sender_is_available_after_native_order_login(self):
        self.group("match", f"试点群 {self.number}")
        self.remember_group("match")
        self.verify_order(expires_at=1)
        self.archived("old-index", "match")
        senders = {
            "self-message": {"type": 1, "id": "synth-staff", "is_self": False},
            "customer-message": {"type": 2, "id": "synth-staff", "is_self": True},
            "robot-message": {"type": 3, "id": "robot-1"},
            "unknown-type-message": {"type": 4, "id": "unknown"},
            "malformed-message": {"type": 1, "id": "x" * 257},
        }
        batch = [{**message(msgid, chat_id="match"), "sender": sender} for msgid, sender in senders.items()]
        with mock.patch("apps.wecom.archive._call_program", return_value=sdk_page("next", batch)):
            self.assertEqual(run_due(), len(batch))

        self.assertEqual(self.messages("match").json()["data"]["messages"], [])
        self.badges.assert_not_called()
        self.verify_order()
        self.badges.return_value = {"synth-staff": {"external_kind": "wecom", "corp_name": "合成外企"}}
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"):
            response = self.messages("match").json()
        self.badges.assert_called_once()
        self.assertEqual(response["code"], 0)
        self.assertEqual({item["msgid"]: item.get("sender") for item in response["data"]["messages"]}, {
            "self-message": {"type": 1, "id": "synth-staff", "is_self": True},
            "customer-message": {
                "type": 2, "id": "synth-staff", "is_self": False, "external_kind": "wecom", "corp_name": "合成外企",
            },
            "robot-message": {"type": 3, "id": "robot-1", "is_self": False},
            "unknown-type-message": None,
            "malformed-message": None,
            "old-index": None,
        })

    def test_app_disabled_during_contact_lookup_returns_no_display_keys(self):
        self.group("match", f"试点群 {self.number}")
        self.remember_group("match")
        self.archived("still-private", "match", sender_id="external-one", sender_type=2)
        self.verify()

        def disable_app(*args):
            WecomAppConfig.objects.filter(pk=self.config.pk).update(enabled=False)
            return {}

        self.badges.side_effect = disable_app
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="must-stay-private"):
            response = self.messages("match")
        self.badges.assert_called_once()
        self.assertNotEqual(response.json()["code"], 0)
        self.assertNotIn("must-stay-private", response.content.decode())

    def test_page_cursor_cannot_cross_chat_and_timestamps_sort_safely(self):
        self.group("match", f"试点群 {self.number}")
        self.remember_group("match")
        self.archived("unrelated", "different-group")
        before = WecomArchiveMessage.objects.get(msg_id="unrelated").pk
        now = timezone.now()
        self.archived("first", "match", sent_at=now)
        second = self.archived("second", "match", sent_at=now)
        self.verify()
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"), mock.patch(
            "apps.wecom.archive_views._MESSAGE_PAGE_SIZE", 1,
        ):
            self.assertNotEqual(self.messages("match", before=before).json()["code"], 0)
            first_page = self.messages("match").json()["data"]
            last_page = self.messages("match", before=first_page["next_before"]).json()["data"]
        self.assertEqual(first_page["messages"][0]["msgid"], "second")
        self.assertEqual(first_page["next_before"], second.pk)
        self.assertEqual(last_page["messages"][0]["msgid"], "first")
        self.assertIsNone(last_page["next_before"])

    def test_order_permission_and_tenant_boundaries(self):
        no_game = make_logged_in_client(username="archive-no-game", groups=("限游戏",), nodes=("order.view_detail",), games=[])
        self.assertNotEqual(no_game.get(self.base + "chat/groups/").json()["code"], 0)
        other, _ = provision_tenant(schema_name="wecom_archive_b", name="合成租户乙", domain="wecom-archive-b.test.com")
        with tenant_context(other):
            self.group("outside", f"合成租户乙 {self.number}")
            self.archived("outside-message", "outside")
            self.group("inside", "租户乙同群标识")
            self.archived("other-tenant-message", "inside", sender_id="other-tenant-sender", sender_type=1)
        self.group("inside", f"合成租户甲 {self.number}")
        self.archived("own-tenant-message", "inside", sender_id="synth-staff", sender_type=1)
        proof = self.groups()["data"]["groups"][0]["proof"]
        self.remember_group("inside")
        self.verify_order()
        body = self.groups()["data"]
        self.assertEqual([item["chat_id"] for item in body["groups"]], ["inside"])
        response = self.messages("outside")
        self.assertNotEqual(response.json()["code"], 0)
        self.assertNotIn("outside-message", response.content.decode())
        with mock.patch("apps.wecom.archive_views.decrypt_message_key", return_value="display-key"):
            response = self.messages("inside").json()
        self.assertEqual(response["code"], 0)
        self.assertEqual([message["msgid"] for message in response["data"]["messages"]], ["own-tenant-message"])

        with tenant_context(other), override_settings(ALLOWED_HOSTS=["wecom-archive-b.test.com"]):
            WecomAppConfig.objects.create(
                pk=self.config.pk, tenant_schema=other.schema_name, enabled=True, corp_id=self.config.corp_id,
                agent_id=self.config.agent_id, secret="synthetic-secret", identity_revision=self.config.identity_revision,
            )
            game = Game.objects.create(name="合成另一工作室游戏")
            Order.objects.create(pk=self.order.pk, game=game, order_no=self.number)
            tenant_b_viewer = make_logged_in_client(
                domain="wecom-archive-b.test.com", groups=("客服",), nodes=("order.view_detail",),
            )
            session = tenant_b_viewer.session
            session[ORDER_ARCHIVE_VIEWER_SESSION_KEY] = {
                **self.session_claim, "user_id": session["_auth_user_id"], "tenant_schema": other.schema_name,
                "expires_at": int(self.session_claim["expires_at"]),
            }
            session.save()
            with override_settings(WECOM_ARCHIVE_TENANT_SCHEMA=other.schema_name, WECOM_ARCHIVE_VIEWERS={}):
                candidate, = tenant_b_viewer.get(self.base + "chat/groups/").json()["data"]["groups"]
                self.assertEqual(candidate["chat_id"], "outside")
                moved = tenant_b_viewer.get(
                    self.base + "chat/messages/", {"chat_id": "inside"}, headers={"X-Wecom-Chat-Proof": proof},
                ).json()
        self.assertNotEqual(moved["code"], 0)
