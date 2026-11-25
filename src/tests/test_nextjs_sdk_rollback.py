from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django_tenants.test.cases import TenantTestCase

from apps.openapi.models import ApiApp
from apps.takerapi.models import TakerApp


class NextjsSdkCredentialRollbackTests(TenantTestCase):
    def test_business_credential_cannot_use_an_enabled_state_recognized_by_old_code(self):
        owner = get_user_model().objects.create_user(username="sdk-rollback-owner")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ApiApp.objects.create(
                name="SDK 回滚验收", kind="business_integration", state="enabled",
                app_key="sdk-rollback-synthetic-key",
                app_secret="sdk-rollback-synthetic-secret",
                created_by=owner,
            )

    def test_bound_taker_app_cannot_be_enabled_by_old_management_code(self):
        taker = get_user_model().objects.create_user(username="sdk-rollback-taker")
        app = TakerApp.objects.create(
            name="SDK 打手绑定回退验收", state="sdk_binding",
            app_key="sdk_rollback-synthetic-key", app_secret="sdk-rollback-synthetic-secret",
            created_by=taker, taker=taker,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            TakerApp.objects.filter(pk=app.pk).update(state="enabled")
        app.refresh_from_db()
        self.assertEqual(app.state, "sdk_binding")
