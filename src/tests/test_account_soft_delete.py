"""「删除用户」= **软删（可恢复）** 的端点层用例（Task 8）。

## 为什么是软删

业务方要「删除用户」这颗按钮真的能用。**硬删在数据层做不了**：`Performance.user` 与
`Order.sales_user` 都是 `on_delete=PROTECT`（代码注释原文「PROTECT：删用户不得带走历史订单」），
只要这人接过单或当过销售，`user.delete()` 会被数据库直接拒绝。所以两个新动作是
`POST /api/accounts/<pk>/delete/`（写 `deleted_at` + 置 `is_active=False`）与
`POST /api/accounts/<pk>/restore/`（清 `deleted_at`，**不动** `is_active`）。

## 判据纪律（本文件的写法约束）

- 断言打在**数据库行**上，不打 `assertLogs`（E-0009）；
- 判「请求被拒」看**业务码**，不看 `response.data`（E-0063：`api_response()` 的 `.data`
  永远是信封，`is not None` 恒真）；
- 跨租户那一条用**用户名**指认，不用主键（两个 schema 的自增序列各自从 1 开始）。
"""
import threading

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import (
    get_tenant_domain_model,
    get_tenant_model,
    tenant_context,
)

from apps.accounts import guards
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import EmpType, Game, Order, Performance
from apps.business.roles import TENANT_ADMIN
from apps.platform.provisioning import provision_tenant, seed_tenant
from tests.p5_auth_helpers import make_logged_in_client
from tests.test_account_api import (
    CREATE_REQUIRED_PROFILE_FIELDS,
    SeededTenantTestCase,
    make_account,
)

User = get_user_model()


class SoftDeleteTestCase(SeededTenantTestCase):
    """公共夹具：一个租户管理员客户端 + 一个可删的普通账号。"""

    def setUp(self):
        self.client = make_logged_in_client(username="acct-admin")
        self.actor = User.objects.get(username="acct-admin")
        self.actor.groups.add(Group.objects.get(name=TENANT_ADMIN))
        self.target = make_account("victim-1", role="客服", game=self.game)

    # ── 小工具：避免每条用例各写一遍信封判据 ──────────────────────────────

    def delete(self, pk):
        return self.client.post(f"/api/accounts/{pk}/delete/")

    def restore(self, pk):
        return self.client.post(f"/api/accounts/{pk}/restore/")

    def profile_of(self, user):
        return StaffProfile.objects.get(user=user)

    def log_rows(self, action, *, target_name=None):
        qs = OperationLog.objects.filter(action=action)
        if target_name is not None:
            qs = qs.filter(target_name=target_name)
        return qs


# ---------------------------------------------------------------------------
# 1–2 / 5 / 9：删 / 恢复 / 幂等 / 留痕
# ---------------------------------------------------------------------------


class SoftDeleteBehaviourTests(SoftDeleteTestCase):
    def test_delete_sets_deleted_at_and_is_active_false_in_one_transaction(self):
        """`delete` ⇒ `deleted_at` 非空 **且** `is_active` 为 `False`（两笔都在）。"""
        response = self.delete(self.target.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)

        self.target.refresh_from_db()
        self.assertIsNotNone(self.target.staff_profile.deleted_at)
        self.assertIs(self.target.is_active, False)
        # 软删的人不该还能登录：`is_active=False` 就是那条判据（Django 的会话认证直接拒）。
        self.assertFalse(self.target.is_active)

    def test_restore_clears_deleted_at_and_keeps_the_account_inactive(self):
        """`restore` ⇒ `deleted_at` 为 `None`，而 `is_active` **仍是 `False`**（恢复 ≠ 启用）。"""
        self.delete(self.target.pk)
        response = self.restore(self.target.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)

        self.target.refresh_from_db()
        self.assertIsNone(self.target.staff_profile.deleted_at)
        self.assertIs(
            self.target.is_active,
            False,
            "「恢复」不得顺手把人启用——要启用得去点「禁用/启用」（brief 改动 3 第 2 条）",
        )

    def test_delete_twice_is_idempotent_and_logs_only_once(self):
        """连删两次都成功，但**留痕只有一行**（第二次不产生新日志行）。"""
        self.assertEqual(self.delete(self.target.pk).json()["code"], 0)
        first_stamp = self.profile_of(self.target).deleted_at
        self.assertEqual(self.log_rows("user.delete", target_name="victim-1").count(), 1)

        second = self.delete(self.target.pk)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["code"], 0)
        self.assertEqual(
            self.log_rows("user.delete", target_name="victim-1").count(),
            1,
            "重复删除不得再写一行留痕（第二次什么都没改）",
        )
        # `deleted_at` 也不得被第二次请求改写（那会让「删除时间」变成一个漂移的值）。
        self.assertEqual(self.profile_of(self.target).deleted_at, first_stamp)

    def test_restore_twice_is_idempotent_and_logs_only_once(self):
        """没删的再恢复 ⇒ 成功，但同样**不重复留痕**。"""
        first = self.restore(self.target.pk)
        self.assertEqual(first.json()["code"], 0)
        self.assertEqual(
            self.log_rows("user.restore", target_name="victim-1").count(),
            0,
            "没删过的人「恢复」什么都没改，不该留痕",
        )

        self.delete(self.target.pk)
        self.assertEqual(self.restore(self.target.pk).json()["code"], 0)
        self.assertEqual(self.log_rows("user.restore", target_name="victim-1").count(), 1)
        self.assertEqual(self.restore(self.target.pk).json()["code"], 0)
        self.assertEqual(
            self.log_rows("user.restore", target_name="victim-1").count(),
            1,
            "重复恢复不得再写一行留痕",
        )

    def test_delete_writes_the_operation_log_row(self):
        """留痕：`action='user.delete'`，`target_name` 是被删的人，`log_type` 取登记表。"""
        self.delete(self.target.pk)
        row = self.log_rows("user.delete", target_name="victim-1").get()
        self.assertEqual(row.action, "user.delete")
        self.assertEqual(row.target_name, "victim-1")
        self.assertEqual(row.target_kind, "user")
        self.assertEqual(row.target_id, str(self.target.pk))
        # 操作人是**认证态**，不是请求体（R20）。
        self.assertEqual(row.actor, "acct-admin")
        # summary 由模板渲染，句首逐字为对标那句；⭐ 审计日志线（spec §3.3）在它**前面**
        # 加了 `【显示名】` 前缀（该夹具用户没有 `first_name` ⇒ 显示名 = 登录账号）。
        self.assertTrue(
            row.summary.startswith("【acct-admin】删除系统用户："),
            f"summary 没有照抄对标的句首：{row.summary!r}",
        )

    def test_restore_writes_the_operation_log_row(self):
        self.delete(self.target.pk)
        self.restore(self.target.pk)
        row = self.log_rows("user.restore", target_name="victim-1").get()
        self.assertEqual(row.log_type, 30)  # LogType.USER（照抄对标的分类码）
        self.assertTrue(row.summary.strip(), "恢复动作必须有非空文案（新拟）")


# ---------------------------------------------------------------------------
# 3–4：自删 / 最后一个管理员
# ---------------------------------------------------------------------------


class SoftDeleteGuardTests(SoftDeleteTestCase):
    def test_delete_self_is_rejected_1004_and_changes_nothing(self):
        """删自己 ⇒ 1004（复用 `SELF_LOCKOUT`），且 `deleted_at` 没变、`is_active` 也没变。"""
        response = self.delete(self.actor.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1004)
        # 文案要能说明是「不能删除自己」（brief 改动 3 第 3 条）。
        self.assertIn("删除", response.json()["msg"])
        self.assertIn("自己", response.json()["msg"])

        self.actor.refresh_from_db()
        self.assertIs(self.actor.is_active, True)
        # `acct-admin`（`make_logged_in_client` 造的）**没有档案行** ⇒ 判据要容忍「没有行」
        # 与「有行但 deleted_at 为空」两种形态（两者都表示「没被删」）。
        profile = StaffProfile.objects.filter(user=self.actor).first()
        self.assertTrue(
            profile is None or profile.deleted_at is None,
            "被拒的自删请求不得留下任何 deleted_at",
        )
        self.assertEqual(self.log_rows("user.delete", target_name="acct-admin").count(), 0)

    def test_delete_another_active_admin_is_allowed_when_one_survives(self):
        """删**别的**活跃管理员（请求者自己还在）⇒ 放行，**不能**过度拦截。"""
        other = make_account("admin-2", role=TENANT_ADMIN)
        self.assertEqual(self.delete(other.pk).json()["code"], 0)
        other.refresh_from_db()
        self.assertIsNotNone(other.staff_profile.deleted_at)

    def test_delete_invariant_rejects_losing_the_last_active_admin(self):
        """判据层：删掉后**一个活跃管理员都不剩** ⇒ 1003（复用 `LAST_ADMIN`）。

        ⚠️ **更新的可达性口径（线 D）**：原文写「这一支在**端点层不可达**——
        `permission_classes = [IsAccountAdmin]` 要求请求者本身是活跃管理员，于是
        `active_admin_count(exclude_pk=target.pk)` 至少算上请求者自己」。
        线 D 把账号端点的准入换成 `IsAccountManager`（持账号管理节点即可，
        **不要求是管理员**）之后，那个前提不再成立 ⇒ 这一支**端点层可达**了：
        端点层用例是同一个文件里的
        `SoftDeleteGuardWithAnHRActorTests::test_hr_deleting_the_last_active_admin_is_rejected_1003`。
        ⚠️ 本条**保留**（判据层直接调用仍有价值：它把分支算术与端点装配解耦），
        但「端点层不可达」那句已作废，⛔ 不要再引用它。
        """
        # 让 `actor` 不是管理员（判据层可直接调用），此时目标就是唯一的活跃管理员。
        self.actor.groups.clear()
        User.objects.filter(username="admin").update(is_active=False)
        blocked = guards.check_delete_invariant(self.actor, self.target)
        self.assertIsNone(blocked, "目标不是管理员 ⇒ 不该被 LAST_ADMIN 拦")

        only_admin = make_account("only-admin", role=TENANT_ADMIN)
        User.objects.filter(username="acct-admin").update(is_active=False)
        blocked = guards.check_delete_invariant(self.actor, only_admin)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.data["code"], 1003)


#: ⭐ 线 D的「人事」形态：**恰好**这三个账号管理节点，⛔ 不持「系统管理员」。
HR_NODES = ("system_user.list", "system_user.add_edit", "system_user.change_status")

#: 自建职位名（`Role.name` 是 `max_length=5` ⇒ 三个字可用）。
HR_ROLE = "人事岗"


class SoftDeleteGuardWithAnHRActorTests(SeededTenantTestCase):
    """⭐ 线 D：**人事**（持账号管理节点、**不是**系统管理员）当请求者时，守卫照样生效。

    ⚠️ 这类请求者是线 D 之前**不存在**的形态：那时账号端点的准入是 `IsAccountAdmin`，
    请求者本身必是活跃管理员，而 `active_admin_count(exclude_pk=target.pk)` 把请求者算在内
    ⇒「删完还剩 0 个」在端点层永远不成立（`guards.check_delete_invariant` 的 docstring
    与 `SoftDeleteGuardTests.test_delete_invariant_rejects_losing_the_last_active_admin`
    都登记过这条边界）。线 D 把准入换成 `IsAccountManager` 之后，**这一支变成端点层可达**
    ⇒ 必须有端点层用例：否则一个把 `check_delete_invariant` 整个删掉的实现照样全绿。
    """

    def setUp(self):
        # `SeededTenantTestCase` 已灌基线（`admin` + 6 个内置角色 + 1 款游戏）。
        self.last_admin = User.objects.get(username="admin")
        self.hr_client = make_logged_in_client(
            username="hr-actor", groups=(HR_ROLE,), nodes=HR_NODES
        )
        self.hr = User.objects.get(username="hr-actor")

    def _not_deleted(self, user):
        """该账号**没有**被软删（「没有档案行」与「有行但 `deleted_at` 为空」都算没删）。"""
        profile = StaffProfile.objects.filter(user=user).first()
        return profile is None or profile.deleted_at is None

    def test_the_fixture_has_exactly_one_active_admin_and_the_hr_is_not_one(self):
        """夹具自证：库里活跃管理员**恰好一个**，且人事**不是**管理员。

        少了这条，「删掉最后一个管理员被拒」可能是「删了一个普通账号被拒」凑出来的。
        """
        from apps.accounts.guards import active_admin_count
        from apps.accounts.permissions import is_account_admin, is_account_manager

        self.assertEqual(active_admin_count(), 1)
        self.assertIs(self.last_admin.is_active, True)
        self.assertFalse(is_account_admin(self.hr))
        # 反向控制：他确实过得了**新**的准入判据（否则本类测的是别的东西）。
        self.assertTrue(is_account_manager(self.hr))

    def test_hr_deleting_the_last_active_admin_is_rejected_1003(self):
        """人事删「最后一个系统管理员」⇒ 既有业务码 **1003**，且终态一行都没动。"""
        response = self.hr_client.post(f"/api/accounts/{self.last_admin.pk}/delete/")
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 1003, body)
        # 终态打在**数据库行**上：没被软删、也没被停用。
        self.last_admin.refresh_from_db()
        self.assertIs(self.last_admin.is_active, True)
        self.assertTrue(self._not_deleted(self.last_admin), "被拒的删除留下了 deleted_at")
        # 也没有写留痕（`user.delete` 那一行）。
        self.assertEqual(
            OperationLog.objects.filter(
                action="user.delete", target_name="admin"
            ).count(),
            0,
        )

        # 正向控制：同一个请求换成**普通账号**（不是管理员）⇒ 放行，证明上面的 1003
        # 不是「人事什么都不能删」凑出来的。
        normal = make_account("hr-deletable", role="客服", game=self.game)
        self.assertEqual(self.hr_client.post(f"/api/accounts/{normal.pk}/delete/").json()["code"], 0)
        self.assertFalse(self._not_deleted(normal))

    def test_hr_deleting_itself_is_rejected_1004(self):
        """人事删自己 ⇒ 既有业务码 **1004**（`SELF_LOCKOUT`），终态不动。"""
        response = self.hr_client.post(f"/api/accounts/{self.hr.pk}/delete/")
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 1004, body)
        self.hr.refresh_from_db()
        self.assertIs(self.hr.is_active, True)
        self.assertTrue(self._not_deleted(self.hr), "被拒的自删留下了 deleted_at")


# ---------------------------------------------------------------------------
# 6：列表默认排除已删 / `?deleted=true` / `is_active` 语义未被抢
# ---------------------------------------------------------------------------


class SoftDeleteListTests(SoftDeleteTestCase):
    def usernames(self, params=None):
        return sorted(
            i["username"] for i in self.client.get("/api/accounts/", params or {}).json()["data"]["items"]
        )

    def test_default_list_hides_deleted_accounts(self):
        """默认列表**看不到**已删的人。"""
        self.assertEqual(self.delete(self.target.pk).json()["code"], 0)
        self.assertNotIn("victim-1", self.usernames())

    def test_deleted_true_shows_only_deleted_accounts(self):
        """`?deleted=true` **只看得到已删的人**（供恢复入口用）。"""
        self.assertEqual(self.delete(self.target.pk).json()["code"], 0)
        listed = self.usernames({"deleted": "true"})
        self.assertEqual(listed, ["victim-1"])

    def test_is_active_false_still_means_disabled_not_deleted(self):
        """旧的 `is_active` 语义**没被 `deleted` 抢走**：`?is_active=false` 只看禁用的人。"""
        make_account("disabled-1", role="客服", game=self.game)
        User.objects.filter(username="disabled-1").update(is_active=False)
        self.assertEqual(self.delete(self.target.pk).json()["code"], 0)

        listed = self.usernames({"is_active": "false"})
        self.assertIn("disabled-1", listed)
        self.assertNotIn("victim-1", listed, "已删的人不该混进「只看禁用」里")

    def test_account_without_a_profile_row_is_not_excluded(self):
        """**没有档案行**的账号（如 `seed_tenant` 灌的 `admin`）不得被默认排除。

        ⚠️ 这条是 `exclude(staff_profile__deleted_at__isnull=False)` 与
        `filter(staff_profile__deleted_at__isnull=True)` 的**唯一**区别：
        后者会把没有档案行的人一起排掉（JOIN 不到 ⇒ 两个方向都不匹配）。
        """
        admin = User.objects.get(username="admin")
        self.assertFalse(
            StaffProfile.objects.filter(user=admin).exists(),
            "前提：seed 的 admin 没有档案行（夹具坏了这条用例就没有判别力）",
        )
        self.assertIn("admin", self.usernames())

    def test_deleted_account_is_not_retrievable_by_default(self):
        """默认详情也看不到已删的人（`get_queryset()` 与列表同源）⇒ 1002。"""
        self.assertEqual(self.delete(self.target.pk).json()["code"], 0)
        response = self.client.get(f"/api/accounts/{self.target.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1002)


# ---------------------------------------------------------------------------
# 7 / 8：不存在 / 跨租户 / 只注册 POST / 服务端管理字段
# ---------------------------------------------------------------------------


class SoftDeleteEdgeCaseTests(SoftDeleteTestCase):
    def test_delete_unknown_id_returns_1002_not_500(self):
        response = self.delete(999999)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1002)

    def test_restore_unknown_id_returns_1002_not_500(self):
        response = self.restore(999999)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1002)

    def test_delete_and_restore_are_post_only(self):
        """两个动作**只注册 POST**（与本类既有的 405 边界一致，D3）。"""
        for pk in (self.target.pk,):
            for method in (self.client.get, self.client.put, self.client.patch, self.client.delete):
                with self.subTest(method=method.__name__):
                    self.assertEqual(method(f"/api/accounts/{pk}/delete/").status_code, 405)
                    self.assertEqual(method(f"/api/accounts/{pk}/restore/").status_code, 405)

    def test_patch_deleted_at_is_rejected_not_silently_ignored(self):
        """T2 移交必修 1：`deleted_at` 只能由两个新端点写。

        `PATCH {"deleted_at": …}` ⇒ 200 + 业务码 **1000**（服务端管理字段**拒绝**，不是静默 no-op）。
        """
        stamp = "2026-01-02T03:04:05Z"
        response = self.client.patch(
            f"/api/accounts/{self.target.pk}/",
            {"deleted_at": stamp},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        self.assertIsNone(self.profile_of(self.target).deleted_at)


class SoftDeleteCrossTenantTests(SoftDeleteTestCase):
    """跨租户：用租户 A 的凭据删/恢复**租户 B 的用户名** ⇒ 必须找不到。

    ⚠️ 判据用**用户名**不用主键：两个 schema 的自增序列各自从 1 开始，主键会重叠
    （AGENTS.md §4 的既有先例）。
    """

    B_USERNAME = "tenant-b-victim"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name="acct_softdel_b", name="软删租户 B", domain="acct-softdel-b.test.local"
        )
        seed_tenant(cls.tenant_b)

    def test_tenant_a_cannot_delete_or_restore_tenant_b_user(self):
        with tenant_context(self.tenant_b):
            # ⚠️ pk **显式指派**一个 A 侧绝不会用到的号：默认自增会让 B 的这个号**恰好等于**
            #    A 的 `acct-admin`（两个 schema 的序列各自从 1 开始），请求就会先命中
            #    「删自己」的 1004 分支，这条用例便测不到跨租户（本轮实测踩到过）。
            b_user = User.objects.create_user(
                pk=9001, username=self.B_USERNAME, password="pw-123456"
            )
            b_user.groups.add(Group.objects.get(name="客服"))
            StaffProfile.objects.create(user=b_user)
            b_pk = b_user.pk

        # A 侧的反向前提：这个用户名在 A 的 schema 里查不到（否则本用例没有判别力）。
        self.assertFalse(User.objects.filter(username=self.B_USERNAME).exists())

        # 用 B 的**主键**发请求：A 里同号行（若有）也绝不是 B 的那个人。
        delete_response = self.delete(b_pk)
        restore_response = self.restore(b_pk)
        for response in (delete_response, restore_response):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["code"],
                1002,
                "A 的凭据不得动到 B 的人（判据：业务码 1002「目标不存在」）",
            )

        # B 的那一行**一个字段都没变**——这是本条用例的真正落点。
        with tenant_context(self.tenant_b):
            b_user.refresh_from_db()
            self.assertIsNone(StaffProfile.objects.get(user=b_user).deleted_at)
            self.assertIs(b_user.is_active, True)


# ---------------------------------------------------------------------------
# ⭐ T6（spec §2.7）：软删账号**重新新增 = 恢复**（判据 11）
# ---------------------------------------------------------------------------


class SoftDeleteRecreateReviveTests(SoftDeleteTestCase):
    """「新增子账户」撞上**软删**账号 ⇒ 恢复那一行而不是报 1001（spec §2.7，判据 11）。

    断言打在**数据库行**上（E-0009）；「同一个 pk」是本组判据的承重点：
    同一行接回来了，历史订单 / 绩效的 `PROTECT` 外键**由构造保证**全部接回。
    """

    def _create(self, username, **extra):
        return self.client.post(
            "/api/accounts/",
            data={
                **CREATE_REQUIRED_PROFILE_FIELDS,
                "username": username,
                "role": "客服",
                "games": [self.game.pk],
                **extra,
            },
            content_type="application/json",
        ).json()

    def _soft_delete(self, user):
        code = self.delete(user.pk).json()["code"]
        self.assertEqual(code, 0, "夹具前置：软删必须成功")

    def test_recreate_a_soft_deleted_username_revives_the_same_row(self):
        """判据 11-a：软删后同名重新新增 ⇒ 200、**同一 pk**、deleted_at 清空、is_active=True、
        表单值已应用、返回**新的**初始密码且能登录。"""
        original = self.target
        original_pk = original.pk
        self._soft_delete(original)

        body = self._create("victim-1", display_name="复活的打手", alipay_account="synth-ali-1")
        self.assertEqual(body["code"], 0, body)
        revived = User.objects.get(username="victim-1")
        # ⭐ 同一行：不是新建的另一个 pk（历史外键因此由构造接回）。
        self.assertEqual(revived.pk, original_pk)
        self.assertEqual(User.objects.filter(username="victim-1").count(), 1)
        profile = StaffProfile.objects.get(user=revived)
        self.assertIsNone(profile.deleted_at)
        self.assertIs(revived.is_active, True)
        # 表单值覆盖（PATCH 语义应用在既有行上）。
        self.assertEqual(revived.first_name, "复活的打手")
        self.assertEqual(profile.alipay_account, "synth-ali-1")
        # 新的初始密码：响应里那份能登录（旧口令作废）。
        new_password = body["data"]["initial_password"]
        self.assertTrue(revived.check_password(new_password))

    def test_recreate_writes_restore_and_create_audit_rows(self):
        """判据 11-e（择形回报）：恢复路径 = 一条 `user.restore` + 一条 `user.create`
        + 逐游戏 `game_permission_add`（actor = 本次操作的管理员）。"""
        self._soft_delete(self.target)
        body = self._create("victim-1")
        self.assertEqual(body["code"], 0, body)
        rows = OperationLog.objects.filter(target_name="victim-1").order_by("id")
        # 第一条 `user.delete` 是**夹具**（setUp 前置的软删）自己写的，也该在。
        self.assertEqual(
            [row.action for row in rows],
            ["user.delete", "user.restore", "user.create", "user.game_permission_add"],
        )
        # `user.restore` 的 reason 与 restore 端点同款（= 用户名），行 [0] 是夹具的删除快照。
        self.assertEqual(rows[1].reason, "victim-1")
        # actor 是本次操作的人（不是被复活的人）。
        self.assertTrue(all(row.actor_id == self.actor.pk for row in rows))

    def test_an_in_use_username_is_still_rejected_1001(self):
        """判据 11-b：在用的同名 ⇒ 1001 不变（软删放行不放「活人」）。"""
        make_account("alive-1", role="客服", game=self.game)
        body = self._create("alive-1")
        self.assertEqual(body["code"], 1001, body)
        self.assertEqual(body["msg"], "登录账号已存在")
        # 终态：没有第二行、原行没被动。
        self.assertEqual(User.objects.filter(username="alive-1").count(), 1)

    def test_a_fresh_username_creates_a_new_row_as_before(self):
        """判据 11-c：全新用户名 ⇒ 普通 create 不变（新 pk、无 restore 留痕）。"""
        body = self._create("brand-new-1")
        self.assertEqual(body["code"], 0, body)
        user = User.objects.get(username="brand-new-1")
        self.assertNotEqual(user.pk, self.target.pk)
        self.assertIsNone(StaffProfile.objects.get(user=user).deleted_at)
        self.assertFalse(
            OperationLog.objects.filter(action="user.restore", target_name="brand-new-1").exists()
        )

    def test_historical_performance_row_points_at_the_revived_pk(self):
        """判据 11-d：恢复后**历史绩效行**仍指向同一个 pk（PROTECT 外键接回同一行）。"""
        game = self.game
        order = Order.objects.create(game=game, order_no="20261016000000000001")
        perf = Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=self.target,
        )
        self._soft_delete(self.target)
        body = self._create("victim-1")
        self.assertEqual(body["code"], 0, body)

        perf.refresh_from_db()
        self.assertEqual(perf.user_id, self.target.pk)
        # 同一行还有个**数量级**的旁证：库里没有第二份 victim。
        self.assertEqual(User.objects.filter(username="victim-1").count(), 1)

    def test_minimal_payload_revival_overwrites_absent_fields_with_defaults(self):
        """钉子（2026-10-16 修复轮，spec §2.7 改准后的语义）：**最小载荷**复活
        ⇒ 不炸、缺席的带 `default=` 字段**按新增默认值覆盖**旧值、历史绩效仍指同 pk。

        ⚠️ 语义出处：create 的序列化器**非 partial**，带 `default=` 的字段在请求缺席时
        也进 `validated_data` ⇒ 恢复 = 以本次表单为准、缺席字段按新增默认值覆盖
        （业务方拍板「重新入职以本次表单为准」——真实表单全键提交，UI 等价；
        API 直调最小载荷 = 旧支付宝/手机号等重置为默认值，**有意为之**）。
        ⛔ 这条钉住的是**实现语义本身**：把它改成「缺席键保持库值」必须同次改
        `apps/accounts/views.py` 恢复分支的注释与 spec §2.7，不能只改一边。
        """
        original_pk = self.target.pk
        # 先给档案放上**非默认**的旧值（直接写库是夹具，判据不打在这里）。
        profile = StaffProfile.objects.get(user=self.target)
        profile.alipay_account = "SYNTH-OLD-ALI"
        profile.mobile = "13800000000"
        profile.save(update_fields=["alipay_account", "mobile"])
        # 历史绩效行（与 11-d 同款构造）：恢复后必须仍指这个 pk。
        order = Order.objects.create(game=self.game, order_no="20261016000000000002")
        perf = Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=self.target,
        )

        self._soft_delete(self.target)
        # 最小载荷 = `_create` 的 required 键（username / role / games +
        # CREATE_REQUIRED_PROFILE_FIELDS），**不带**任何支付宝 / 手机号键。
        body = self._create("victim-1")
        self.assertEqual(body["code"], 0, body)

        revived = User.objects.get(username="victim-1")
        self.assertEqual(revived.pk, original_pk)
        revived_profile = StaffProfile.objects.get(user=revived)
        self.assertIsNone(revived_profile.deleted_at)
        self.assertIs(revived.is_active, True)
        # 默认值覆盖：旧值被重置成序列化器的 `default=""`（不是保持旧值）。
        self.assertEqual(revived_profile.alipay_account, "")
        self.assertEqual(revived_profile.mobile, "")
        # 历史绩效仍指同一 pk（PROTECT 外键接回同一行）。
        perf.refresh_from_db()
        self.assertEqual(perf.user_id, original_pk)


class SoftDeleteRecreateConcurrencyTests(TransactionTestCase):
    """判据 11-f：两个并发 create 撞**同一软删名** ⇒ 恰好一个成功，另一个 1001。

    ⚠️ 形态与 `tests/test_account_concurrency.py` 同一先例：`TransactionTestCase`
    + 真线程（`TenantTestCase` 的外层事务会让并发根本不发生）。
    """

    DOMAIN = "tenant.test.com"
    THREAD_JOIN_TIMEOUT = 15.0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model()(schema_name="test")
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(tenant=cls.tenant, domain=cls.DOMAIN)
        cls.domain.save()
        if cls.DOMAIN not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS += [cls.DOMAIN]
        connection.set_tenant(cls.tenant)
        seed_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        if cls.DOMAIN in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(cls.DOMAIN)
        super().tearDownClass()

    def setUp(self):
        self.assertTrue(
            connection.get_autocommit(),
            "本用例没跑在 autocommit 上——检查基类是否被换成了 TenantTestCase",
        )
        connection.set_tenant(self.tenant)
        self.game = Game.objects.create(name="并发游戏甲")
        # 一台已软删的「victim」（删除动作的两笔写入在此直写，纯夹具）：
        # 两个管理员马上要同时按这个名字重新新增。
        victim = User.objects.create_user(username="conc-victim", password="pw-old")
        StaffProfile.objects.create(
            user=victim, deleted_at=timezone.now(),
        )
        victim.is_active = False
        victim.save(update_fields=["is_active"])
        self.victim_pk = victim.pk
        # 两个管理员客户端（每条线程一个）。
        for name in ("conc-alpha", "conc-beta"):
            client = make_logged_in_client(username=name, domain=self.DOMAIN)
            user = User.objects.get(username=name)
            user.groups.add(Group.objects.get(name=TENANT_ADMIN))
            setattr(self, f"client_{name}", client)

    def _post_create(self, client, results, key):
        def worker():
            try:
                close_old_connections()
                connection.set_tenant(self.tenant)
                response = client.post(
                    "/api/accounts/",
                    data={
                        **CREATE_REQUIRED_PROFILE_FIELDS,
                        "username": "conc-victim",
                        "role": "客服",
                        "games": [self.game.pk],
                    },
                    content_type="application/json",
                )
                results[key] = response.json()
            except Exception as exc:  # noqa: BLE001 —— 结果要回主线程断言
                results[key] = {"code": "thread-error", "msg": f"{type(exc).__name__}: {exc}"}
            finally:
                connection.close()

        thread = threading.Thread(target=worker)
        thread.start()
        return thread

    def test_two_concurrent_creates_on_one_soft_deleted_name_elect_one_winner(self):
        results: dict = {}
        threads = [
            self._post_create(getattr(self, f"client_{name}"), results, name)
            for name in ("conc-alpha", "conc-beta")
        ]
        for thread in threads:
            thread.join(timeout=self.THREAD_JOIN_TIMEOUT)
        self.assertFalse(any(t.is_alive() for t in threads), "并发 create 挂住了")

        codes = sorted(body.get("code") for body in results.values())
        # ⭐ 恰好一胜一拒：赢家 0，输家 1001（行锁串行化后，后到的在锁内复查见「在用」）。
        self.assertEqual(codes, [0, 1001], results)
        # 终态：还是那一行（pk 不变）、已复活、只留一份。
        victim = User.objects.get(username="conc-victim")
        self.assertEqual(victim.pk, self.victim_pk)
        self.assertIs(victim.is_active, True)
        self.assertIsNone(StaffProfile.objects.get(user=victim).deleted_at)
        self.assertEqual(User.objects.filter(username="conc-victim").count(), 1)
