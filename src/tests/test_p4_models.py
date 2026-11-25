"""P4-2：两张新表（`OrderRefund` / `PerformanceAdjustment`）+ `Performance.commission_rate`。

四组判据，每组都有会红的变体（逐组实测见任务报告「变异记录表」）：

1. **金额可为负的哨兵**（D-0015）：`amount = -1.00` 必须落库成功，**且** `full_clean()` 不拒。
2. **四条 `PROTECT`**（spec §3.7）：删被引用的 `Order` / `Performance` / `OrderRefund`
   → `ProtectedError`。
3. **`request_id` 唯一**：同值两次 → `IntegrityError`。
4. ⭐ **Ruling H 的反向哨兵**（D-0028）：两张新模型的 `Meta.permissions` 为空、
   `BIZ_PERMISSIONS` 仍是**字面值** 2 项，且不含 `refund` / `settle`。
5. ⭐ **字段形状哨兵**：用 `_meta.get_field()` 逐字段钉住 brief 的宽度与
   可空性，并钉住「`request_id` 只有一个唯一机制」——`makemigrations --check` **不管**这一维度。

⚠️ **为什么除了落库还要 `full_clean()`**：`objects.create()` **不跑 validators**
（Django 的字段校验只在 `full_clean()` / `ModelForm` / DRF 序列化器里跑），
所以只断言落库成功的话，「给 `amount` 加 `MinValueValidator(0)`」这种变异**测不出来**——
而 D-0015 明确把它与 `CHECK` 并列禁止。这条 `full_clean()` 断言就是为它加的判别力。

⚠️ 本文件**不测任何 Django codename**（P4 的闸门已推迟到 RBAC 基座，见 D-0028 / Ruling H）。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, models, transaction
from django.db.models.deletion import ProtectedError
from django_tenants.test.cases import TenantTestCase

from apps.business import permissions
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderRefund,
    Performance,
    PerformanceAdjustment,
    SettleStatus,
)


class P4ModelsTestCase(TenantTestCase):
    """P4-2 的公共夹具：`Game → Order → Performance`（brief 指定的形状）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game, order_no="20260920000000000001"
        )
        self.perf = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN
        )

    def make_refund(self, **overrides):
        payload = {
            "order": self.order,
            "kind": OrderRefund.Kind.PARTIAL,
            "amount": Decimal("10.00"),
            "reason": "客户申请部分退款",
            "request_id": "req-p4-2-001",
        }
        payload.update(overrides)
        return OrderRefund.objects.create(**payload)

    def make_adjustment(self, **overrides):
        payload = {
            "performance": self.perf,
            "kind": PerformanceAdjustment.Kind.MANUAL,
            "amount": Decimal("10.00"),
            "reason": "手工冲抵",
        }
        payload.update(overrides)
        return PerformanceAdjustment.objects.create(**payload)


class NegativeAmountSentinelTests(P4ModelsTestCase):
    """§9 #18 ① 的哨兵：**金额允许为负**（D-0015 / spec §3.9）。

    任何形式的 `≥ 0`（`CheckConstraint` / `MinValueValidator`）都会让本组变红。
    """

    def test_create_with_negative_amounts_succeeds(self):
        refund = self.make_refund(amount=Decimal("-1.00"))
        adjustment = self.make_adjustment(amount=Decimal("-1.00"))
        refund.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(refund.amount, Decimal("-1.00"))
        self.assertEqual(adjustment.amount, Decimal("-1.00"))

    def test_negative_amounts_pass_full_clean(self):
        """`full_clean()` 也不得拒负数——这条专门抓 `MinValueValidator`。

        `objects.create()` 不跑 validators，所以只有这条能证明「没有加校验器」。
        """
        refund = self.make_refund(amount=Decimal("-1.00"))
        adjustment = self.make_adjustment(amount=Decimal("-1.00"))
        refund.full_clean()       # 抛 ValidationError 即失败
        adjustment.full_clean()


class ProtectOnDeleteTests(P4ModelsTestCase):
    """spec §3.7：四条 `PROTECT` 逐条实测（删被引用者必须抛 `ProtectedError`）。"""

    def test_order_with_refund_cannot_be_deleted(self):
        # ⚠️ **必须用一条干净的订单**：夹具那条 `self.order` 上还挂着 `Performance`
        #    （`Performance.order` 同样是 `PROTECT`，会先抛）——不隔离的话本用例对
        #    `OrderRefund.order` 的删除策略**零判别力**（变异实测：改成 `CASCADE` 照样绿）。
        order = Order.objects.create(
            game=self.game, order_no="20260920000000000099"
        )
        self.make_refund(order=order)
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                order.delete()

    def test_order_with_performance_cannot_be_deleted(self):
        """`Performance.order` 的 `PROTECT`（P3a 已有）——本条是回归护栏。"""
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                self.order.delete()

    def test_performance_with_adjustment_cannot_be_deleted(self):
        self.make_adjustment()
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                self.perf.delete()

    def test_refund_referenced_by_adjustment_cannot_be_deleted(self):
        refund = self.make_refund()
        self.make_adjustment(refund=refund)
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                refund.delete()


class RequestIdUniquenessTests(P4ModelsTestCase):
    def test_duplicate_request_id_is_rejected(self):
        self.make_refund(request_id="req-dup")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_refund(request_id="req-dup", amount=Decimal("5.00"))

    def test_distinct_request_ids_are_accepted(self):
        """反向对照：换一个 `request_id` 必须能建——否则上面那条可能只是「第二次都失败」。"""
        self.make_refund(request_id="req-a")
        self.make_refund(request_id="req-b")
        self.assertEqual(OrderRefund.objects.count(), 2)


class RulingHNoPermissionTests(P4ModelsTestCase):
    """⭐ 反向哨兵：**不得声明任何 `Meta.permissions`、不得动 `BIZ_PERMISSIONS`**。

    Ruling H / D-0028：P4 的权限闸门等 RBAC 基座，走 `has_node(code)`；
    这里新增 Django codename 会凭空造出第二套判据。
    """

    def test_new_models_declare_no_meta_permissions(self):
        self.assertEqual(list(OrderRefund._meta.permissions), [])
        self.assertEqual(list(PerformanceAdjustment._meta.permissions), [])

    def test_biz_permissions_is_still_exactly_two(self):
        # ⚠️ 期望值是**字面值** 2，不是 `len(BIZ_PERMISSIONS)` 之类的自指表达式
        #    （自指断言在常量被改坏时不会红，见 E-0024）。
        self.assertEqual(len(permissions.BIZ_PERMISSIONS), 2)

    def test_biz_permissions_contains_no_p4_codename(self):
        for entry in permissions.BIZ_PERMISSIONS:
            self.assertNotIn("refund", entry.lower(), entry)
            self.assertNotIn("settle", entry.lower(), entry)


class CommissionRateColumnTests(P4ModelsTestCase):
    def test_commission_rate_does_not_break_one_main_taker_per_order(self):
        """加列后 `uniq_order_main_taker` 仍成立（第二条打手行必须被拒）。

        两个人 ⇒ 只可能撞部分唯一索引，不会与 `(order, user)` 那条混。
        """
        u1 = User.objects.create_user(username="taker-commission-1")
        u2 = User.objects.create_user(username="taker-commission-2")
        self.perf.user = u1
        self.perf.commission_rate = Decimal("12.34")
        self.perf.save()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Performance.objects.create(
                    order=self.order,
                    user=u2,
                    emp_type=EmpType.MAIN,
                    commission_rate=Decimal("50.00"),
                )

    def test_commission_rate_is_nullable_decimal_column(self):
        # 复用夹具那条打手绩效：同一订单再建一条 `MAIN` 会撞 `uniq_order_main_taker`。
        self.perf.refresh_from_db()
        self.assertIsNone(self.perf.commission_rate)
        self.perf.commission_rate = "12.34"
        self.perf.save()
        self.perf.refresh_from_db()
        self.assertIsInstance(self.perf.commission_rate, Decimal)
        self.assertEqual(str(self.perf.commission_rate), "12.34")


class AdjustmentDefaultsTests(P4ModelsTestCase):
    def test_settle_status_defaults_to_unsettled(self):
        adjustment = self.make_adjustment()
        adjustment.refresh_from_db()
        # 判据是既有枚举的**值**（字面值「未结账」），证明复用 SettleStatus 而非新建枚举。
        self.assertEqual(adjustment.settle_status, SettleStatus.UNSETTLED)
        self.assertEqual(adjustment.settle_status, "未结账")
        self.assertIsNone(adjustment.settled_at)

    def test_amount_defaults_to_zero_when_omitted(self):
        adjustment = PerformanceAdjustment.objects.create(
            performance=self.perf,
            kind=PerformanceAdjustment.Kind.MANUAL,
            reason="不带金额",
        )
        adjustment.refresh_from_db()
        self.assertIsInstance(adjustment.amount, Decimal)
        self.assertEqual(adjustment.amount, Decimal("0.00"))

    def test_amount_is_a_decimal_column_after_reload(self):
        """只断 `str()` 是空洞的——必须 `refresh_from_db()` 后断类型（P2/E-0024 的教训）。"""
        refund = self.make_refund(amount="99.90")
        refund.refresh_from_db()
        self.assertIsInstance(refund.amount, Decimal)
        self.assertEqual(str(refund.amount), "99.90")

    def test_kind_choices_are_the_exact_literal_values(self):
        # ⭐ P4-M2追加 `REVERSAL`（退单回池冲销上一轮的累计退款额）——
        #    本哨兵的作用就是「新增取值时当场变红、逼实现者在此处做决定」。
        #    ⛔ 既有两个取值一个都没动（它们是对外契约）。
        self.assertEqual(
            {k.value for k in OrderRefund.Kind}, {"PARTIAL", "FULL", "REVERSAL", "SET"}
        )
        self.assertEqual(
            {k.value for k in PerformanceAdjustment.Kind},
            {"PARTIAL_REFUND", "FULL_REFUND", "MANUAL"},
        )


class FieldShapeTests(P4ModelsTestCase):
    """字段形状哨兵（修复轮 1 / M3）。

    `makemigrations --check` 只保证「模型 ↔ 迁移」一致，**不保证「brief ↔ 模型」**：
    后来者把 `reason` 改成 `max_length=64`、`amount` 改成 `decimal_places=0`，
    Django 会生成迁移、`--check` 通过、其余用例全绿——整类漂移是**静默的**。
    ⚠️ 期望值一律写**字面值**（不引用生产常量，自指断言不会红，见 E-0024）。
    """

    def test_orderrefund_field_shapes(self):
        meta = OrderRefund._meta
        amount = meta.get_field("amount")
        self.assertEqual((amount.max_digits, amount.decimal_places), (12, 2))
        self.assertIs(amount.null, False)
        for name, expected in (
            ("reason", 255), ("operator", 64), ("request_id", 64), ("kind", 16)
        ):
            self.assertEqual(meta.get_field(name).max_length, expected, name)

    def test_performanceadjustment_field_shapes(self):
        meta = PerformanceAdjustment._meta
        amount = meta.get_field("amount")
        self.assertEqual((amount.max_digits, amount.decimal_places), (12, 2))
        for name, expected in (
            ("reason", 255), ("operator", 64), ("settle_status", 8), ("kind", 16)
        ):
            self.assertEqual(meta.get_field(name).max_length, expected, name)
        self.assertIs(meta.get_field("refund").null, True)

    def test_performance_commission_rate_field_shape(self):
        field = Performance._meta.get_field("commission_rate")
        self.assertEqual((field.max_digits, field.decimal_places), (5, 2))
        self.assertIs(field.null, True)

    def test_request_id_has_exactly_one_unique_mechanism(self):
        """① 的机械哨兵：字段层**不** unique，唯一性只在具名 `Meta.constraints` 上。

        两者同时存在会在这列上建出两棵唯一索引（复评 I1 / 任务报告「实现期发现 1」）。
        """
        self.assertIs(OrderRefund._meta.get_field("request_id").unique, False)
        named = {c.name: c for c in OrderRefund._meta.constraints}
        self.assertIn("uniq_order_refund_request_id", named)
        constraint = named["uniq_order_refund_request_id"]
        self.assertIsInstance(constraint, models.UniqueConstraint)
        self.assertEqual(list(constraint.fields), ["request_id"])
