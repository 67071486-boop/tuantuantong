"""`Role` ↔ `Group` 的同步（spec §6.2 / §10-A4）。"""
from unittest import mock

from django.contrib.auth.models import Group
from django_tenants.test.cases import TenantTestCase

from apps.rbac.models import Role
from apps.rbac.seed import seed_catalog
from apps.rbac.services import (
    RoleServiceError, create_role, delete_role, rename_role,
    update_role_permissions, update_role_sales_agent_flag,
)


class RoleServiceTests(TenantTestCase):
    def setUp(self):
        seed_catalog()

    def test_create_role_creates_same_named_group(self):
        role = create_role(name="客服", remark="r", permission_codes=["order.list"])
        self.assertEqual(role.group.name, "客服")
        self.assertTrue(Group.objects.filter(name="客服").exists())

    def test_create_role_rejects_unknown_code_and_writes_nothing(self):
        with self.assertRaises(RoleServiceError):
            create_role(name="客服", remark="", permission_codes=["nope.nope"])
        # 事务性：Group 与 Role 都不该留下
        self.assertFalse(Group.objects.filter(name="客服").exists())
        self.assertFalse(Role.objects.filter(name="客服").exists())

    def test_rename_role_renames_the_group_too(self):
        role = create_role(name="客服", remark="", permission_codes=["order.list"])
        rename_role(role, name="客服主管", remark="x")
        self.assertTrue(Group.objects.filter(name="客服主管").exists())
        self.assertFalse(Group.objects.filter(name="客服").exists())
        self.assertEqual(role.group.name, "客服主管")

    def test_rename_keeps_user_membership(self):
        """改名**保成员**：账号挂在 Group 上，改名不该把人的职位弄丢（spec §6.3）。"""
        from django.contrib.auth import get_user_model

        role = create_role(name="客服", remark="", permission_codes=["order.list"])
        user = get_user_model().objects.create_user(username="u1", password="pw-123456")
        user.groups.add(role.group)
        rename_role(role, name="代练员", remark="")
        user.refresh_from_db()
        self.assertEqual([g.name for g in user.groups.all()], ["代练员"])

    def test_rename_role_rejects_a_bare_group_occupying_the_name(self):
        """**裸 Group**（只有组、没有 Role）占用的名字也要拒——不查就是 `IntegrityError` → 500。

        裸 Group 不是假想状态：`seed.py` 先 `Group.objects.get_or_create` 再建 `Role`；
        `apps/platform/provisioning.py` 直接按 `DEFAULT_ROLES` 灌 `Group`。这些组在角色列表里
        **看不见**，管理员会把它们的名字当"未占用"拿去改名——正是 E-0010 里「这类请求」的形状。
        `Group.name` 是唯一键，所以判据必须查 `Group`（排除自己），不能只查 `Role`。
        """
        role = create_role(name="客服", remark="", permission_codes=["order.list"])
        bare = Group.objects.create(name="代练员")
        with self.assertRaises(RoleServiceError):
            rename_role(role, name="代练员", remark="")
        # 失败必须**一行都不动**：原名还在，裸 Group 也不被吞
        role.refresh_from_db()
        self.assertEqual(role.name, "客服")
        self.assertEqual(role.group.name, "客服")
        self.assertTrue(Group.objects.filter(pk=bare.pk, name="代练员").exists())
        self.assertEqual(Group.objects.count(), 2)

    def test_rename_role_rejects_a_name_held_by_another_role(self):
        """名字被另一个 `Role` 占着也要拒。

        这条刻意把那个 `Role` 的 `Group` 起成**别的名字**（`Role.name` 与 `Group.name`
        不是硬约束，`seed_builtin_roles` 的 `get_or_create` 与直接写库都可能留下这种行）——
        此时只查 `Group` 会漏，`group.save()` 成功后 `role.save()` 才撞 `Role.name` 唯一键。
        两处都查才闭合"改名不会冒出 `IntegrityError`"这条。
        """
        role = create_role(name="客服", remark="", permission_codes=["order.list"])
        other_group = Group.objects.create(name="别的组名")
        Role.objects.create(name="代练员", remark="", group=other_group)
        with self.assertRaises(RoleServiceError):
            rename_role(role, name="代练员", remark="")
        role.refresh_from_db()
        self.assertEqual(role.name, "客服")
        self.assertEqual(role.group.name, "客服")
        self.assertEqual(Role.objects.filter(name="代练员").count(), 1)
        self.assertTrue(Group.objects.filter(name="别的组名").exists())

    def test_delete_role_deletes_the_group(self):
        role = create_role(name="客服", remark="", permission_codes=["order.list"])
        delete_role(role)
        self.assertFalse(Role.objects.filter(name="客服").exists())
        self.assertFalse(Group.objects.filter(name="客服").exists())

    def test_update_permissions_replaces_the_set(self):
        role = create_role(name="客服", remark="", permission_codes=["order.list"])
        update_role_permissions(role, permission_codes=["order.list", "order.delete"])
        self.assertEqual(
            sorted(role.permissions.values_list("code", flat=True)),
            ["order.delete", "order.list"],
        )
        update_role_permissions(role, permission_codes=["order.list"])
        self.assertEqual(
            list(role.permissions.values_list("code", flat=True)), ["order.list"]
        )

    def test_update_sales_agent_flag_touches_only_that_column(self):
        """「可作为销售客服」标记的写口**只写那一列**（2026-10-08 本线）。

        与 `rename_role` 那条「管理员改过的授权不回滚」同口径：标记是**第四个**可写关注点，
        它没有权利顺手改名字 / 备注 / 权限集（判据落在 `update_fields=["can_be_sales_agent"]`）。
        """
        role = create_role(
            name="客服", remark="原备注", permission_codes=["order.list", "order.delete"]
        )
        update_role_sales_agent_flag(role, can_be_sales_agent=True)
        role.refresh_from_db()
        self.assertIs(role.can_be_sales_agent, True)
        self.assertEqual((role.name, role.remark), ("客服", "原备注"))
        self.assertEqual(role.group.name, "客服")
        self.assertEqual(
            sorted(role.permissions.values_list("code", flat=True)),
            ["order.delete", "order.list"],
        )
        # 反向也要能落库（否则"只写 True"的实现照样绿）。
        update_role_sales_agent_flag(role, can_be_sales_agent=False)
        role.refresh_from_db()
        self.assertIs(role.can_be_sales_agent, False)

    def test_update_permissions_rejects_empty(self):
        """对标原文「请至少选择一个权限」——后端也必须强制（spec §7）。"""
        role = create_role(name="客服", remark="", permission_codes=["order.list"])
        with self.assertRaises(RoleServiceError):
            update_role_permissions(role, permission_codes=[])
        self.assertEqual(role.permissions.count(), 1, "失败时不该改动已有分配")

    def test_create_role_reuses_an_existing_same_named_group(self):
        """服务函数是**唯一的**同步入口：Group 已存在时复用它，不建第二个、也不报错。"""
        Group.objects.create(name="客服")
        role = create_role(name="客服", remark="", permission_codes=["order.list"])
        self.assertEqual(role.group.name, "客服")
        self.assertEqual(Group.objects.filter(name="客服").count(), 1)

    def test_create_role_rejects_a_taken_name_instead_of_integrity_error(self):
        """`Role` 重名是**预期失败**，必须是业务异常而不是撞唯一约束的 `IntegrityError`（E-0010）。

        为什么要有这条：Task 7 的「复制」动作就走这条路——新名字已被占用时，
        用户该看到业务错误码而不是 HTML 500。同名 `Group` 复用是另一回事（上一条用例），
        所以这里刻意让已有 `Group` 叫别的名字，好让"有没有多建一个 Group"这件事可判。
        """
        Role.objects.create(name="客服", remark="", group=Group.objects.create(name="客服组"))
        with self.assertRaises(RoleServiceError):
            create_role(name="客服", remark="", permission_codes=["order.list"])
        self.assertEqual(Role.objects.filter(name="客服").count(), 1)
        self.assertFalse(Group.objects.filter(name="客服").exists(), "不留孤儿 Group")
        self.assertEqual(Group.objects.count(), 1, "已有 Group 也不该被动过")

    def test_create_role_rejects_empty_codes_and_writes_nothing(self):
        """空权限集与未知 code 一样要**原子失败**：`_resolve_nodes` 在写第一行之前就抛。"""
        with self.assertRaises(RoleServiceError):
            create_role(name="客服", remark="", permission_codes=[])
        self.assertFalse(Group.objects.filter(name="客服").exists())
        self.assertFalse(Role.objects.filter(name="客服").exists())

    def test_create_role_rolls_back_the_group_when_a_later_write_fails(self):
        """`@transaction.atomic` 得**真的**承重：Group 已建、随后任一步抛错 ⇒ Group 也要回滚。

        上面三条失败路径都在写第一行之前就校验完，所以它们**证明不了**事务回滚真的生效
        （去掉装饰器照样绿）。这里在 `Group.objects.get_or_create` 之后人为制造一个失败，
        让"装饰器在不在"这件事有判别力——否则"四个函数都在一个事务里"就只是一句注释。
        """
        with mock.patch(
            "apps.rbac.services.Role.objects.create", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                create_role(name="客服", remark="", permission_codes=["order.list"])
        self.assertFalse(Group.objects.filter(name="客服").exists(), "Group 必须随事务回滚")
