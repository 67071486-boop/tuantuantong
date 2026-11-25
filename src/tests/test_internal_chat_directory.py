from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.roles import SERVICE, SUPERVISOR, TENANT_ADMIN, WORKER
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

BASE = "/api/internal-chat/"


class InternalChatDirectoryTests(TenantTestCase):
    def setUp(self):
        self.enterContext(override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True))

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_tenant, _ = provision_tenant(
            schema_name="directory_other", name="对照工作室", domain="directory-other.test.local",
        )

    def account(self, username, role, domain=None):
        client = make_logged_in_client(
            domain=domain or self.domain.domain, username=username, groups=(role,), games=None,
        )
        return client, get_user_model().objects.get(username=username).pk

    def data(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def save(self, admin, primary, backups, **kwargs):
        return admin.put(BASE + "settings/", {
            "primary_contact_id": primary, "backup_contact_ids": backups,
        }, content_type="application/json", **kwargs)

    def start(self, client, peer_id):
        return client.post(BASE + "conversations/", {"peer_id": peer_id}, content_type="application/json")

    def test_admin_configures_ordered_active_staff_directory(self):
        admin, admin_id = self.account("directory-admin", TENANT_ADMIN)
        worker, worker_id = self.account("directory-worker", WORKER)
        _, primary = self.account("directory-primary", "财务")
        _, backup_a = self.account("directory-backup-a", SUPERVISOR)
        _, backup_b = self.account("directory-backup-b", SERVICE)
        _, disabled = self.account("directory-disabled", SERVICE)
        _, deleted = self.account("directory-deleted", SERVICE)
        StaffProfile.objects.create(user_id=deleted, deleted_at=timezone.now())
        get_user_model().objects.get(pk=worker_id).groups.add(Group.objects.get(name=SERVICE))
        get_user_model().objects.filter(pk=disabled).update(is_active=False)

        initial = self.data(admin.get(BASE + "settings/"))
        self.assertIsNone(initial["primary_contact_id"])
        self.assertEqual(initial["backup_contact_ids"], [])
        self.assertEqual({item["id"] for item in initial["candidates"]}, {admin_id, primary, backup_a, backup_b})
        denied_read = worker.get(BASE + "settings/")
        denied_write = self.save(worker, primary, [])
        self.assertEqual((denied_read.status_code, denied_read.json()["code"]), (403, 403))
        self.assertEqual((denied_write.status_code, denied_write.json()["code"]), (403, 403))

        directory = self.data(self.save(admin, primary, [backup_b, backup_a]))
        self.assertEqual(directory["primary_contact_id"], primary)
        self.assertEqual(directory["backup_contact_ids"], [backup_b, backup_a])
        self.assertEqual(directory, self.data(admin.get(BASE + "settings/")))
        for invalid in [
            (worker_id, []), (primary, [disabled]), (primary, [deleted]), (primary, [primary]), (primary, [backup_a, backup_a]),
        ]:
            error = self.save(admin, *invalid)
            self.assertEqual((error.json()["code"], error.json()["data"]), (1000, None))
        self.assertEqual(directory, self.data(admin.get(BASE + "settings/")))
        result = self.data(worker.get(BASE + "contacts/"))
        self.assertIs(result["worker_directory_only"], True)
        self.assertEqual(
            [(row["id"], row["is_primary"]) for row in result["contacts"]],
            [(primary, True), (backup_b, False), (backup_a, False)],
        )
        self.assertEqual(self.data(self.start(worker, primary))["peer"]["id"], primary)
        self.assertEqual(result["contacts"][1:], self.data(worker.get(BASE + "contacts/?q=backup"))["contacts"])
        get_user_model().objects.filter(pk=primary).update(is_active=False)
        self.assertEqual(
            self.data(worker.get(BASE + "contacts/"))["contacts"], result["contacts"][1:],
        )
        self.assertIsNone(self.data(admin.get(BASE + "settings/"))["primary_contact_id"])
        self.assertEqual(self.data(self.save(admin, None, []))["backup_contact_ids"], [])
        self.assertEqual(self.data(worker.get(BASE + "contacts/"))["contacts"], [])

    def test_directory_settings_audits_successful_changes_only(self):
        admin, admin_id = self.account("audit-directory-admin", TENANT_ADMIN)
        worker, worker_id = self.account("audit-directory-worker", WORKER)
        _, primary = self.account("audit-directory-primary", SERVICE)
        _, backup = self.account("audit-directory-backup", SUPERVISOR)
        logs = OperationLog.objects.filter(action="system.internal_chat_directory_update")

        self.assertEqual(self.save(worker, primary, []).status_code, 403)
        self.assertNotEqual(self.save(admin, worker_id, []).json()["code"], 0)
        self.assertFalse(logs.exists())

        self.data(self.save(admin, primary, [backup]))
        self.data(self.save(admin, primary, [backup]))
        row = logs.get()
        self.assertEqual(
            (row.actor, row.actor_id, row.log_type, row.target_kind, row.target_id),
            ("audit-directory-admin", admin_id, 40, "chat_directory", "1"),
        )
        self.assertEqual(row.field_name, "首选客服、备用客服")
        self.assertEqual((row.target_name, row.from_value, row.to_value, row.reason), ("", "", "", ""))

        self.data(self.save(admin, None, [backup]))
        self.assertEqual(logs.count(), 2)
        self.assertEqual(logs.first().field_name, "首选客服")

    def test_directory_write_rolls_back_when_audit_cannot_be_saved(self):
        admin, _ = self.account("audit-directory-admin", TENANT_ADMIN)
        _, contact = self.account("audit-directory-contact", SERVICE)
        with (
            patch("apps.internal_chat.views.record", side_effect=RuntimeError("audit failed")),
            self.assertRaisesMessage(RuntimeError, "audit failed"),
        ):
            self.save(admin, contact, [])
        self.assertIsNone(self.data(admin.get(BASE + "settings/"))["primary_contact_id"])

    def test_new_worker_conversations_enforce_directory_and_existing_staff_conversation_allows_reply(self):
        admin, _ = self.account("directory-admin", TENANT_ADMIN)
        worker, worker_id = self.account("directory-worker", WORKER)
        _, second_worker = self.account("directory-other-worker", WORKER)
        _, listed = self.account("directory-listed", SERVICE)
        staff, unlisted = self.account("directory-unlisted", SERVICE)

        self.assertEqual(self.start(worker, listed).status_code, 403)
        self.data(self.save(admin, listed, []))
        for peer in (unlisted, second_worker):
            denied = self.start(worker, peer)
            self.assertEqual((denied.status_code, denied.json()["code"]), (403, 403))
        self.assertEqual(self.data(worker.get(BASE + "conversations/"))["conversations"], [])
        self.assertEqual(self.data(worker.get(BASE + "contacts/?q=unlisted"))["contacts"], [])
        draft = self.data(self.start(worker, listed))
        self.assertEqual(draft["peer"]["id"], listed)
        self.data(self.save(admin, None, []))
        denied = worker.post(
            BASE + f"conversations/{draft['id']}/messages/", {"text": "空会话不能绕过名单", "client_id": "draft-first"},
            content_type="application/json",
        )
        self.assertEqual((denied.status_code, denied.json()["code"]), (403, 403))

        self.assertIs(self.data(staff.get(BASE + "contacts/"))["worker_directory_only"], False)
        conversation = self.data(self.start(staff, worker_id))
        messages = BASE + f"conversations/{conversation['id']}/messages/"
        self.data(staff.post(messages, {"text": "客服主动联系", "client_id": "staff-first"}, content_type="application/json"))
        self.assertEqual(self.data(self.start(worker, unlisted))["id"], conversation["id"])
        self.data(worker.post(messages, {"text": "打手在原会话回复", "client_id": "worker-reply"}, content_type="application/json"))
        self.assertEqual(
            [row["text"] for row in self.data(staff.get(messages))["messages"]],
            ["客服主动联系", "打手在原会话回复"],
        )

    def test_directory_and_admin_credentials_are_isolated_between_workspaces(self):
        admin, _ = self.account("directory-local-admin", TENANT_ADMIN)
        local_worker, _ = self.account("directory-local-worker", WORKER)
        _, local_staff = self.account("directory-local-support", SERVICE)
        self.data(self.save(admin, local_staff, []))

        other_domain = "directory-other.test.local"
        with tenant_context(self.other_tenant):
            other_admin, _ = self.account("directory-other-admin", TENANT_ADMIN, other_domain)
            other_worker, _ = self.account("directory-other-worker", WORKER, other_domain)
            _, other_staff = self.account("directory-other-support", SERVICE, other_domain)
            self.data(self.save(other_admin, other_staff, []))

        self.assertEqual(self.data(admin.get(BASE + "contacts/?q=other-support"))["contacts"], [])
        cross = Client(SERVER_NAME=other_domain)
        cross.cookies = admin.cookies.copy()
        denied_read = cross.get(BASE + "settings/")
        denied_write = self.save(cross, None, [])
        self.assertEqual((denied_read.status_code, denied_read.json()["code"]), (401, 401))
        self.assertEqual((denied_write.status_code, denied_write.json()["code"]), (401, 401))
        denied_search = cross.get(BASE + "contacts/?q=other-support")
        self.assertEqual((denied_search.status_code, denied_search.json()["code"]), (401, 401))
        self.assertEqual(
            [row["name"] for row in self.data(other_worker.get(BASE + "contacts/"))["contacts"]],
            ["directory-other-support"],
        )
        self.assertEqual(
            [row["name"] for row in self.data(local_worker.get(BASE + "contacts/"))["contacts"]],
            ["directory-local-support"],
        )

    def test_employee_search_matches_name_and_username_beyond_first_page(self):
        staff, _ = self.account("directory-staff", SERVICE)
        user_model = get_user_model()
        user_model.objects.bulk_create([user_model(username=f"directory-page-{i}") for i in range(100)])
        target = user_model.objects.create(username="directory-match-target", first_name="测试姓名")

        first_page = self.data(staff.get(BASE + "contacts/"))["contacts"]
        self.assertEqual(len(first_page), 100)
        self.assertNotIn(target.pk, {row["id"] for row in first_page})
        for query in ("试姓名", "MATCH-TARGET"):
            with self.subTest(query=query):
                result = self.data(staff.get(BASE + "contacts/", {"q": query}))["contacts"]
                self.assertEqual(result, [{"id": target.pk, "name": "测试姓名", "avatar_url": None}])
