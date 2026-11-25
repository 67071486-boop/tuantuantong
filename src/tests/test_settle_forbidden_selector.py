"""Task 9：读取层 `apps.accounts.selectors.settle_forbidden_user_ids`。

它是「**结账 / 扣点两处拦截判据**的唯一来源」（结账与扣点都调它，见
`tests/test_settle_api.py::SettleForbiddenTests` 与 `tests/test_adjust_api.py`）。
⚠️ 范围写死（E-0011）：`apps.accounts.selectors.list_accounts` 里还有一处
`settle_forbidden=` 的**列表筛选**，那不是拦截判据，不在本句覆盖范围内。
本文件只测读取层本身：只回被冻的那些、空输入回空集、不存在的 id 忽略、**不跨租户**。

⚠️ **跨租户那条的判据纪律**（`AGENTS.md` §5）：
两个 schema 的主键序列**各自从 1 开始**，所以不能拿「B 里那个主键的行」当断言对象。
本文件的做法与 `tests/test_account_isolation.py::test_forbid_self_service_does_not_cross_tenants`
同源：两侧建**同名**账号、取值相反，判据用**用户名**反查「这一侧的那一行到底冻没冻」；
同时 B 侧再冻一个**自己**的人当**正向对照**——否则「拿 A 的 id 在 B 读到空集」
可能由「这个函数在 B 里根本不工作 / B 里一条冻结行都没有」凑出来。
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import StaffProfile
from apps.accounts.selectors import settle_forbidden_user_ids
from apps.platform.provisioning import provision_tenant

User = get_user_model()

#: 租户 A = `TenantTestCase` 的 `test` schema（域名与既有隔离测试同口径）。
DOMAIN_A = "tenant.test.com"
#: 租户 B 的域名/库名。刻意与 A 及既有隔离文件都不同：`Domain` 上有唯一约束。
DOMAIN_B = "settle-forbidden-b.test.local"
SCHEMA_B = "settle_forbidden_b"

#: 两侧**同名**的账号（判别力来自两侧取值相反，不是名字的有无）。
SAME_NAME = "settle-iso-same"

#: 一个**不存在**的 user id（int4 上界附近，任何真实行的自增都到不了）。
MISSING_ID = 2_000_000_000


class SettleForbiddenUserIdsTests(TenantTestCase):
    """读取层的正向/边界语义（改动 1 的 docstring 逐条对应用例）。"""

    def setUp(self):
        self.frozen = User.objects.create_user(username="sul-frozen")
        StaffProfile.objects.create(user=self.frozen, settle_forbidden=True)
        self.unfrozen = User.objects.create_user(username="sul-unfrozen")
        StaffProfile.objects.create(user=self.unfrozen, settle_forbidden=False)
        # ⚠️ 没有档案行的人：**不是**「被冻」，也不是「未冻」——读取层对他只能回「不在集合里」。
        self.without_profile = User.objects.create_user(username="sul-no-profile")

    def test_only_the_frozen_ids_come_back(self):
        result = settle_forbidden_user_ids(
            [self.frozen.pk, self.unfrozen.pk, self.without_profile.pk]
        )

        # 断言写**集合**（不是列表）：调用方的用法是 `in` 与判空，返回类型也是判据的一部分。
        self.assertEqual(result, {self.frozen.pk})

    def test_empty_input_returns_an_empty_set(self):
        self.assertEqual(settle_forbidden_user_ids([]), set())

    def test_none_returns_the_whole_tenant_frozen_set(self):
        """`user_ids=None`（2026-11-07「打手范围」筛选加的默认参）回**全租户**被禁集。

        判别力：再冻第二个人、且把未禁与没档案行的一起留在夹具里——
        None 分支若漏加 `settle_forbidden=True` 以外的过滤或语义写成 `[]`（空集），
        本条与 `test_empty_input_returns_an_empty_set` 至少一条红。
        """
        another = User.objects.create_user(username="sul-frozen-2")
        StaffProfile.objects.create(user=another, settle_forbidden=True)

        self.assertEqual(settle_forbidden_user_ids(None), {self.frozen.pk, another.pk})
        # 未禁与没档案行的人不在返回值里（与名单分支同一口径）。
        self.assertNotIn(self.unfrozen.pk, settle_forbidden_user_ids(None))
        self.assertNotIn(self.without_profile.pk, settle_forbidden_user_ids(None))

    def test_an_id_with_no_row_is_ignored(self):
        # 前置哨兵：`MISSING_ID` 真的不存在（否则这条用例可能在测别的东西）。
        self.assertFalse(StaffProfile.objects.filter(user_id=MISSING_ID).exists())

        self.assertEqual(
            settle_forbidden_user_ids([self.frozen.pk, MISSING_ID]), {self.frozen.pk}
        )


class SettleForbiddenCrossTenantTests(TenantTestCase):
    """A 冻结的人，在 B 用**同一个 id** 调读取层 ⇒ 不受影响（`AGENTS.md` §4）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 建：在用例内部建 schema 会与外层事务相冲
        #    （Django 的 schema DDL 不能在 `TestCase` 的事务里回滚）。手法同
        #    `tests/test_p4_isolation.py` / `tests/test_account_isolation.py`。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="禁止结算隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # 第一行把连接显式钉回租户 A（挡住上一条用例的 schema 残留）。
        connection.set_tenant(self.tenant)

    def test_a_frozen_person_does_not_freeze_the_same_id_in_another_tenant(self):
        a_user = User.objects.create_user(username=SAME_NAME)
        StaffProfile.objects.create(user=a_user, settle_forbidden=True)
        # B 侧的同名账号用一个**与 A 不同**的主键：这样下面「B 冻自己人」的正向对照
        # 不会同时把 A 的那个 id 也弄成「B 里存在且被冻」——那是两个 schema 主键各自
        # 从 1 开始带来的巧合，它会让负向断言失去判别力（`AGENTS.md` §5 的同族坑）。
        b_pk = a_user.pk + 1000
        try:
            with tenant_context(self.tenant_b):
                b_user = User.objects.create_user(pk=b_pk, username=SAME_NAME)
                StaffProfile.objects.create(user=b_user)      # settle_forbidden 默认 False
        finally:
            connection.set_tenant(self.tenant)

        # ── 夹具自证（承重点，不是用例）：两侧都有同名账号，且**取值相反** ──────
        self.assertIs(
            StaffProfile.objects.get(user__username=SAME_NAME).settle_forbidden, True
        )
        with tenant_context(self.tenant_b):
            self.assertIs(
                StaffProfile.objects.get(user__username=SAME_NAME).settle_forbidden, False
            )
            self.assertEqual(User.objects.get(username=SAME_NAME).pk, b_pk)
        # A 侧对照锚点：用 A 的 pk 读出来是「被冻」。
        self.assertEqual(settle_forbidden_user_ids([a_user.pk]), {a_user.pk})

        with tenant_context(self.tenant_b):
            # ── 正向对照：B 冻**自己**的人 ⇒ 同一个函数在 B 里真的回得出 id ─────
            StaffProfile.objects.filter(user__username=SAME_NAME).update(
                settle_forbidden=True
            )
            self.assertEqual(settle_forbidden_user_ids([b_pk]), {b_pk})
            # ── 负向断言：拿 **A 的 id** 在 B 里读 ⇒ 空集（串 schema 时会读到 A 的 True ⇒ 红）
            self.assertEqual(settle_forbidden_user_ids([a_user.pk]), set())
        self.assertEqual(settle_forbidden_user_ids([a_user.pk]), {a_user.pk})
