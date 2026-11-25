from django.db.migrations.loader import MigrationLoader
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import WxPusherNotice
from apps.business.models import Order, Performance
from apps.rbac.models import Role


class LegacyWriterMigrationDefaultsTests(TenantTestCase):
    def test_old_models_can_insert_on_the_upgraded_schema(self):
        old_apps = MigrationLoader(None).project_state([
            ("business", "0134_register_handmade_index_batch"),
            ("rbac", "0014_add_order_platform_write_node"),
        ]).apps
        old_order = old_apps.get_model("business", "Order")
        old_performance = old_apps.get_model("business", "Performance")
        old_role = old_apps.get_model("rbac", "Role")
        game = old_apps.get_model("business", "Game").objects.create(name="兼容测试")
        group = old_apps.get_model("auth", "Group").objects.create(name="兼容测试")

        order = old_order.objects.create(game=game, order_no="20261115000000000001")
        performance = old_performance.objects.create(order=order)
        role = old_role.objects.create(group=group, name="兼容测试")

        persisted = Order.objects.get(pk=order.pk)
        self.assertEqual(persisted.split_kind, "")
        self.assertEqual(persisted.wecom_notice_journal, [])
        self.assertIs(persisted.wecom_notice_pending, False)
        self.assertEqual(Performance.objects.get(pk=performance.pk).settlement_method, "")
        self.assertEqual(Role.objects.get(pk=role.pk).dashboard_type, "")

    def test_pre_details_wxpusher_writer_can_enqueue_on_the_upgraded_schema(self):
        old_apps = MigrationLoader(None).project_state([
            ("accounts", "0034_wxpusher_preferences_and_notices"),
        ]).apps
        user = old_apps.get_model("auth", "User").objects.create(username="legacy-notice-writer")
        binding = old_apps.get_model("accounts", "WxPusherBinding").objects.create(
            user=user, app_id="9123", uid="UID_legacy_writer",
        )

        notice = old_apps.get_model("accounts", "WxPusherNotice").objects.create(
            user=user, binding=binding, kind="assigned", reference="legacy-notice-after-upgrade",
        )

        self.assertEqual(WxPusherNotice.objects.get(pk=notice.pk).details, {})
