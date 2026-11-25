"""Approval evidence is write-once and readable by its participants."""

from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.choices import MemberType
from apps.business.models import Game, OrderSource
from apps.core.storage import MAX_APPROVAL_EVIDENCE_BYTES
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

APPROVALS = "/api/business/approvals/"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic approval evidence"


class ApprovalEvidenceTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_tenant, _ = provision_tenant(
            schema_name="evidence_other", name="合成审批工作室", domain="evidence-other.test.local",
        )

    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(override_settings(MEDIA_ROOT=Path(directory)))
        self.game = Game.objects.create(name="合成审批证据游戏")
        self.source = OrderSource.objects.create(name="合成来源")
        admin = make_logged_in_client(username="evidence-manager", is_superuser=True)
        self.owner = make_logged_in_client(
            username="evidence-owner", groups=("罚款发起",), nodes=("home.approval_apply",),
        )
        self.approver = make_logged_in_client(
            username="evidence-approver", groups=("罚款审批",), nodes=("home.approval_penalty_approve",),
        )
        self.cc = make_logged_in_client(username="evidence-copied")
        self.outsider = make_logged_in_client(
            username="evidence-outsider", groups=("罚款发起",), nodes=("home.approval_apply",),
        )
        users = get_user_model().objects
        self.approver_id = users.get(username="evidence-approver").pk
        self.cc_id = users.get(username="evidence-copied").pk
        target = users.get(username="evidence-outsider")
        target.staff_profile.member_type = MemberType.PARTNER
        target.staff_profile.save(update_fields=["member_type"])
        self.target_id = target.pk
        settings = admin.patch(
            APPROVALS + "settings/",
            {"approver_ids": [self.approver_id], "available_cc_ids": [self.cc_id]},
            content_type="application/json",
        ).json()
        self.assertEqual(settings["code"], 0, settings)

    def issue_ticket(self):
        response = self.owner.post(
            APPROVALS + "media/ticket/", {"ext": "png"}, content_type="application/json",
        ).json()
        self.assertEqual(response["code"], 0, response)
        return response["data"]

    def submit(self, key):
        response = self.owner.post(
            APPROVALS,
            {
                "original_order_no": None,
                "target_id": self.target_id,
                "amount": "30",
                "reason": "合成罚款依据",
                "approver_id": self.approver_id,
                "cc_ids": [self.cc_id],
                "image_keys": [key],
                "order": {
                    "game": self.game.pk, "order_source": self.source.pk, "project_text": "合成罚款项目",
                },
            },
            content_type="application/json",
        ).json()
        self.assertEqual(response["code"], 0, response)
        return response["data"]["images"][0]["url"]

    def test_only_participants_can_read_and_image_cannot_be_replaced(self):
        ticket = self.issue_ticket()
        url = ticket["upload_url"]
        self.assertEqual(self.outsider.put(url, PNG, content_type="image/png").status_code, 403)
        self.assertEqual(self.owner.put(url, PNG, content_type="image/png").status_code, 204)
        image_url = self.submit(ticket["key"])
        self.assertEqual(self.outsider.get(image_url).status_code, 404)
        self.assertEqual(self.owner.put(url, PNG + b"changed", content_type="image/png").status_code, 409)

        for client in (self.owner, self.approver, self.cc):
            image = client.get(image_url)
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image["Cache-Control"], "private, no-store")
            self.assertEqual(image["Content-Type"], "image/png")
            self.assertEqual(b"".join(image.streaming_content), PNG)

        for key in (ticket["key"], ticket["key"].replace("/users/", "/./users/")):
            blob = "/api/media/blob/" + key
            self.assertEqual(self.outsider.get(blob).status_code, 403)
            self.assertEqual(self.owner.put(blob, PNG, content_type="image/png").status_code, 403)

    def test_upload_allows_maximum_size_and_rejects_invalid_or_oversized_bytes(self):
        url = self.issue_ticket()["upload_url"]
        self.assertEqual(self.owner.put(url, b"fake PNG", content_type="image/png").status_code, 400)
        maximum = PNG.ljust(MAX_APPROVAL_EVIDENCE_BYTES, b"\x00")
        self.assertEqual(self.owner.put(url, maximum + b"x", content_type="image/png").status_code, 413)
        self.assertEqual(self.owner.put(url, maximum, content_type="image/png").status_code, 204)

    def test_other_tenant_cannot_use_a_ticket_or_read_the_evidence(self):
        ticket = self.issue_ticket()
        self.assertEqual(self.owner.put(ticket["upload_url"], PNG, content_type="image/png").status_code, 204)
        image_url = self.submit(ticket["key"])
        with tenant_context(self.other_tenant):
            other = make_logged_in_client(
                "evidence-other.test.local", username="evidence-owner",
                groups=("罚款发起",), nodes=("home.approval_apply",),
            )
            self.assertEqual(other.put(ticket["upload_url"], PNG, content_type="image/png").status_code, 403)
            self.assertEqual(other.get(image_url).status_code, 404)
