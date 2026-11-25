"""`StaffProfile` 的模型级约束（spec §4.2 / §4.3）。"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode, MemberType
from apps.accounts.models import StaffProfile


class StaffProfileModelTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        # ⚠️ 必须在 setUpClass 里建第二个租户：`TenantTestCase` 把每个测试包在外层事务里，
        #    在测试方法内 `provision_tenant`（建 schema = DDL）会与之相冲。
        #    这沿用 `tests/test_p2_isolation.py` 的既有形状。
        super().setUpClass()
        from apps.platform.provisioning import provision_tenant

        cls.tenant_b, _ = provision_tenant(
            schema_name="acct_model_b", name="租户 B", domain="acct-model-b.test.local"
        )

    def _user(self, username="m1"):
        return get_user_model().objects.create_user(username=username, password="pw-123456")

    def test_defaults_match_the_reference_form(self):
        profile = StaffProfile.objects.create(user=self._user())
        # 对标物实测默认值：userType=0、autoBrokerageType=20
        # ⛔ **`bussType` 已不是对标的默认值**（业务方裁定，D-0086）：
        #    对标是 `0 代练+陪玩`，我方改成 `1 专做代练`。这里钉**字面值 1**，
        #    不写 `BusinessType.BOOSTING`——否则常量被改坏时这条断言不会红（E-0024 自指断言）。
        # ⛔ `no_sms_on_intake` 那一列已被删除（同一次裁定），故不再有它的默认值断言。
        self.assertEqual(profile.business_type, 1)
        self.assertEqual(profile.member_type, MemberType.INTERNAL)
        self.assertEqual(profile.commission_mode, CommissionMode.ORDER_PERCENT)
        self.assertEqual(profile.commission_value, Decimal("0.00"))
        self.assertEqual(profile.deposit_amount, Decimal("0.00"))

    def test_sort_and_settle_and_soft_delete_defaults(self):
        """三个新列的默认值（「排序值」/「禁止结算」/「删除时间」）。

        ⚠️ 布尔值用 `assertIs(x, False)` 而不是 `assertFalse`：后者对 `0` / `""` / `None`
        也通过，于是「列是布尔且默认 False」这条断言会被「列其实是别的东西」骗过。
        `deleted_at` 的 `None` 同理用 `assertIsNone`（`null=True` 的语义是「没删」）。
        """
        profile = StaffProfile.objects.create(user=self._user("m-new-cols"))
        self.assertEqual(profile.order_by, 0)
        self.assertIs(profile.settle_forbidden, False)
        self.assertIsNone(profile.deleted_at)

    def test_deleted_sms_switch_is_gone_from_the_model(self):
        """「录的单不发短信」是**彻底删**（不是藏起来）：模型上不能再有这一列。

        判据打在 `_meta` 上而不是 `hasattr(profile, ...)`：`hasattr` 对一个还没跑迁移的
        开发库会因为列还在而返回真——那样这条断言就变成了「迁移跑没跑」的检测，
        与本次要钉的「字段已从模型消失」不是一回事。
        """
        field_names = {f.name for f in StaffProfile._meta.get_fields()}
        self.assertNotIn("no_sms_on_intake", field_names)

    def test_grab_price_range_ordered_constraint_rejects_inverted_range(self):
        user = self._user("m2")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                StaffProfile.objects.create(
                    user=user,
                    grab_price_min=Decimal("100.00"),
                    grab_price_max=Decimal("50.00"),
                )

    def test_grab_price_range_allows_missing_bounds(self):
        # 只有一端有值时必须放行（约束是三段式，不是「两端都必填」）
        p1 = StaffProfile.objects.create(user=self._user("m3"), grab_price_min=Decimal("10.00"))
        p2 = StaffProfile.objects.create(user=self._user("m4"), grab_price_max=Decimal("20.00"))
        self.assertIsNone(p1.grab_price_max)
        self.assertIsNone(p2.grab_price_min)
        # 边界是同值放行（约束是 `>=` 不是 `>`）
        same = StaffProfile.objects.create(
            user=self._user("m5"),
            grab_price_min=Decimal("30.00"),
            grab_price_max=Decimal("30.00"),
        )
        self.assertEqual(same.grab_price_min, same.grab_price_max)

    def test_profile_is_per_tenant(self):
        """StaffProfile 是租户表：写在 A 里的行在 B 里不可见（AGENTS.md §4）。"""
        from django_tenants.utils import tenant_context

        StaffProfile.objects.create(user=self._user("only-a"))
        self.assertTrue(StaffProfile.objects.filter(user__username="only-a").exists())
        with tenant_context(self.tenant_b):
            # 阳性对照：先证明 B 自己写的行在 B 里查得到，
            # 否则下面的否定断言可能只是因为这张表在当前 schema 里什么都查不到。
            StaffProfile.objects.create(user=self._user("only-b"))
            self.assertTrue(StaffProfile.objects.filter(user__username="only-b").exists())
            self.assertFalse(StaffProfile.objects.filter(user__username="only-a").exists())
