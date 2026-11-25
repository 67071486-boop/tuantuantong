from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import caches
from django.db import connection
from django.test import SimpleTestCase


class ArchiveContactBadgeTests(SimpleTestCase):
    def setUp(self):
        cache = caches["wecom_archive_badges"]
        cache.clear()
        self.addCleanup(cache.clear)
        self.config = SimpleNamespace(
            tenant_schema="studio_a", corp_id="corp", agent_id=1, identity_revision=2,
            secret="fixture-app", contacts_secret="fixture-contact",
        )

    @patch("apps.wecom.archive_contacts._contacts_client")
    def test_classifies_only_confirmed_sender_and_caches_each_contact(self, make_client):
        from apps.wecom.archive_contacts import external_sender_badges

        get = make_client.return_value.external_contact.get
        get.side_effect = [
            {"errcode": 0, "external_contact": {"external_userid": "wechat", "type": 1}},
            {"errcode": 0, "external_contact": {"external_userid": "partner", "type": 2, "corp_name": " 星河 "}},
            {"errcode": 0, "external_contact": {"external_userid": "no-name", "type": 2}},
            {"errcode": 0, "external_contact": {"external_userid": "different", "type": 1}},
        ]
        with patch.object(connection, "schema_name", "studio_a"):
            expected = {
                "wechat": {"external_kind": "wechat"},
                "partner": {"external_kind": "wecom", "corp_name": "星河"},
                "no-name": {"external_kind": "wecom"},
            }
            ids = ["wechat", "partner", "no-name", "unknown", "wechat"]
            self.assertEqual(external_sender_badges(self.config, ids), expected)
            self.assertEqual(external_sender_badges(self.config, ids), expected)
        self.assertEqual(get.call_count, 4)
        make_client.assert_called_once_with(self.config)

    @patch("apps.wecom.archive_contacts._contacts_client")
    def test_cache_is_isolated_by_tenant_even_when_ids_collide(self, make_client):
        from apps.wecom.archive_contacts import external_sender_badges

        get = make_client.return_value.external_contact.get
        get.side_effect = [
            {"errcode": 0, "external_contact": {"external_userid": "same", "type": 2, "corp_name": "甲"}},
            {"errcode": 0, "external_contact": {"external_userid": "same", "type": 1}},
        ]
        with patch.object(connection, "schema_name", "studio_a"):
            self.assertEqual(external_sender_badges(self.config, ["same"])["same"]["corp_name"], "甲")
        with patch.object(connection, "schema_name", "studio_b"):
            self.assertEqual(external_sender_badges(self.config, ["same"]), {})
            tenant_b = SimpleNamespace(**{**vars(self.config), "tenant_schema": "studio_b"})
            self.assertEqual(external_sender_badges(tenant_b, ["same"]), {"same": {"external_kind": "wechat"}})
        self.assertEqual(get.call_count, 2)
