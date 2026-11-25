"""`apps.rbac` 两个模型的约束（spec §3）。"""
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django_tenants.test.cases import TenantTestCase

from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role


class RbacModelTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from apps.platform.provisioning import provision_tenant

        cls.tenant_b, _ = provision_tenant(
            schema_name="rbac_model_b", name="租户 B", domain="rbac-model-b.test.local"
        )

    def _node(self, code="order.list", benchmark_id=14, **kw):
        return PermissionNode.objects.create(
            code=code, benchmark_id=benchmark_id, name="订单管理列表",
            note="订单管理列表", url="/Order/OrderList",
            kind=NodeKind.PAGE, level=1, **kw,
        )

    def test_role_name_max_length_is_five(self):
        """5 是**照抄对标**的 maxlength，不是我们的偏好（spec §3）。"""
        self.assertEqual(Role._meta.get_field("name").max_length, 5)
        self.assertEqual(Role._meta.get_field("remark").max_length, 50)

    def test_role_name_rejects_six_chars_on_full_clean(self):
        role = Role(name="客服主管长长", group=Group.objects.create(name="g-six"))
        with self.assertRaises(ValidationError):
            role.full_clean()

    def test_role_name_is_unique_per_tenant(self):
        Group.objects.create(name="dup-role")
        Role.objects.create(name="dup-r", group=Group.objects.get(name="dup-role"))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Role.objects.create(
                    name="dup-r", group=Group.objects.create(name="dup-role-2")
                )

    def test_permission_node_code_is_unique(self):
        self._node()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._node(benchmark_id=99)

    def test_permission_node_benchmark_id_is_unique(self):
        self._node()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._node(code="order.delete")

    def test_nodes_and_roles_are_per_tenant(self):
        """两个模型都是租户表：写在 A 里的行在 B 里不可见（AGENTS.md §4）。

        ⚠️ **不要用目录里真实存在的 code 做夹具**（如 `order.list`）：Task 3 把
        `seed_rbac` 接进 `seed_tenant` 之后，租户 B 在 `provision_tenant` 时**已经**拿到
        目录里的每一个点，那句 `create(code="order.list")` 会撞唯一约束——本用例会在
        Task 3 落地后**由绿转红**（执行前冲突扫描 C3 实测）。这里用两个**只属于本测试**的 code。
        """
        from django_tenants.utils import tenant_context

        PermissionNode.objects.create(
            code="tenant_a_only_node", name="只属于 A 的点", note="", url="",
            kind=NodeKind.PAGE, level=1,
        )
        Group.objects.create(name="only-a-group")
        Role.objects.create(name="onlyA", group=Group.objects.get(name="only-a-group"))

        with tenant_context(self.tenant_b):
            # 阳性对照：先证明 B 自己写得进去、查得到（否则否定断言可能只是表是空的）
            PermissionNode.objects.create(
                code="tenant_b_only_node", name="只属于 B 的点", note="", url="",
                kind=NodeKind.PAGE, level=1,
            )
            self.assertTrue(PermissionNode.objects.filter(code="tenant_b_only_node").exists())
            self.assertFalse(PermissionNode.objects.filter(code="tenant_a_only_node").exists())
            self.assertFalse(Role.objects.filter(name="onlyA").exists())
