from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business.models import Game, Order, OrderScreenshot, OrderStatus
from tests.p5_auth_helpers import make_logged_in_client


class OrderScreenshotModelTests(TenantTestCase):
    """⚠️ 租户层测试必须用 `TenantTestCase`（⛔ 不是 `django.test.TestCase`）——
    本仓租户表只存在于租户 schema，普通 TestCase 连 `Order` 都建不出来。"""

    def setUp(self):
        # ⚠️ 造 `Order` 的最小形状（照 `tests/test_audit_coverage.py` 的 `AuditRowShapeTests.setUp`——
        #    ⛔ 这里**不写 `文件:行号`**：那种形状会被 `tests/test_doc_line_references.py` 的
        #    「注释引用总数」冻结网计入，每加一处都要重取那个常量，得不偿失）：
        #    `game` 是**唯一必填 FK**（server/category/project 可空）；
        #    `order_no` 虽然 `editable=False`，仍可**显式传入**（那个 flag 只影响表单）。
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game, order_no="20260925000000000001",
            status=OrderStatus.IN_PROGRESS, hall_price=Decimal("88.00"),
        )
        make_logged_in_client(username="media-model-user")
        self.user = User.objects.get(username="media-model-user")

    def test_object_key_is_unique(self):
        OrderScreenshot.objects.create(
            order=self.order, object_key="demo/orders/x/1.jpg", uploaded_by=self.user)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OrderScreenshot.objects.create(
                    order=self.order, object_key="demo/orders/x/1.jpg", uploaded_by=self.user)

    def test_status_defaults_to_pending(self):
        row = OrderScreenshot.objects.create(
            order=self.order, object_key="demo/orders/x/2.jpg", uploaded_by=self.user)
        # ⚠️ 期望值写**字面值**，⛔ 不写 `OrderScreenshot.STATUS_PENDING`——那是自指断言
        #    （期望与 `default=STATUS_PENDING` 同源，把常量改掉该用例仍绿）。
        #    本仓同款要求见 `E-0024` 与 `tests/test_p4_models.py` 的 `FieldShapeTests`。
        self.assertEqual(row.status, "pending")


class OrderScreenshotFieldShapeTests(TenantTestCase):
    """把 spec §5.1 的**逐列契约**钉成字面值。

    ⚠️ 为什么必须有：`makemigrations --check` 只保证「模型 ↔ 迁移一致」，
    **不保证「spec ↔ 模型」**——把 `remark` 从 50 改成 64 会顺带生成一个新迁移且全套全绿。
    """

    def test_column_shape_matches_spec(self):
        from apps.business.models import OrderScreenshot as M

        fields = {f.name: f for f in M._meta.get_fields()}
        self.assertEqual(fields["object_key"].max_length, 255)
        self.assertTrue(fields["object_key"].unique)
        self.assertEqual(fields["original_name"].max_length, 255)
        self.assertEqual(fields["content_type"].max_length, 64)
        # 备注宽度 = 对标那张表单的 `maxlength=50`（spec §3.2），⛔ 别放宽
        self.assertEqual(fields["remark"].max_length, 50)
        self.assertEqual(fields["order"].remote_field.on_delete.__name__, "CASCADE")
        self.assertEqual(fields["uploaded_by"].remote_field.on_delete.__name__, "PROTECT")
        self.assertEqual([c[0] for c in fields["img_type"].choices], [0, 1])
        # ⚠️ 字面值：状态取值是对外契约（前端与审计都按字符串读）
        self.assertEqual([c[0] for c in fields["status"].choices], ["pending", "ready"])
        self.assertTrue(fields["created_at"].auto_now_add)
        self.assertTrue(fields["ready_at"].null)



class StaffProfileMediaKeysTests(TenantTestCase):
    def test_two_blank_columns_exist(self):
        user = User.objects.create_user(username="media-profile-user", password="x")
        profile = StaffProfile.objects.create(user=user)
        self.assertEqual(profile.avatar_key, "")
        self.assertEqual(profile.wechat_qr_key, "")
