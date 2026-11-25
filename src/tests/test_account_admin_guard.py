"""§6.2：不能把租户管成没有管理员（三重保护 ① ② ③ + 反证 + 零管理员死角）。

**本文件的两类用例，边界不同，别混着读**：

- **端点层**（发真实请求）：钉的是「经过 API 时这条保护真的拦得住，而且该放行的真的放行」。
- **判据层**（直接调 `check_admin_invariant`）：钉的是分支算术本身。

准入是「管理员，或持任一账号管理节点」。人事不是管理员也能进守卫，
所以停用 / 降级最后一个管理员在端点层可达（文末人事用例，各带正向对照）。
自锁本来就可达。守卫不因准入放宽而放松。

⚠️ `TenantTestCase` 不跑 `seed_tenant`，而本文件要 `Group.objects.get(TENANT_ADMIN)` 与
`User.objects.get(username="admin")`，所以 `setUpClass` 必须显式灌一次。
"""
import hashlib

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.db.models import Q
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context

from apps.accounts.admin_mutations import _tenant_lock_key
from apps.accounts.guards import active_admin_count, check_admin_invariant
from apps.accounts.models import StaffProfile
from apps.accounts.permissions import is_account_admin
from apps.business.models import Game
from apps.business.roles import SERVICE, TENANT_ADMIN
from apps.platform.provisioning import seed_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: `TenantTestCase.get_test_tenant_domain` 的默认域名（与既有账号测试同口径）。
DOMAIN = "tenant.test.com"

#: `ErrorCode.LAST_ADMIN` / `SELF_LOCKOUT` 的字面值。断言写数字而不是枚举，是为了让
#: 「码值被改掉」这件事在测试里可见（枚举改名/改值不该静默通过）。
LAST_ADMIN = 1003
SELF_LOCKOUT = 1004

#: ⭐ F1：「非系统管理员不得授予『系统管理员』职位」的**业务码**。
#: 口径 = 复用 `ErrorCode.BUSINESS`(1002)（域层拒绝 + 可读 msg 的既有政策，
#: 见 `apps/business/jidanpu_common.py:229` 的「不新开码」），⛔ 不是 403（那是节点门/准入类
#: 的通道）、⛔ 不是 1000（那会读成「你参数写错了」）。断言写字面量（E-0024）。
ROLE_GRANT_DENIED = 1002

#: 同一档拒绝的**文案**字面量（值钉子：常量换成另一个非空串时，只断 `code` 的用例照样绿）。
ROLE_GRANT_DENIED_MSG = "只有系统管理员可以授予「系统管理员」职位"

#: 首页数据概览（判据 1 的**终态**：提权被拒之后它必须仍是 403）。
#: 闸门是 `is_account_admin`（`apps/business/api/console_views.py`），与账号端点无关。
HOME_OVERVIEW_URL = "/api/business/home/overview/"

#: 线 D 的「人事」形态：持账号管理节点、**不是**系统管理员（与
#: `test_account_admin_predicate.py` 的 HR_NODES 同口径）。
HR_NODES = ("system_user.list", "system_user.add_edit", "system_user.change_status")

#: 自建职位名（`Role.name` 是 `max_length=5` ⇒ 三个字可用）。
HR_ROLE = "人事岗"


class LastAdminGuardTests(TenantTestCase):
    """端点层的三重保护；判据层的算术在同一文件的下面一个类里。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # R5：`TenantTestCase` 只建 schema 跑迁移，不灌基线数据。
        seed_tenant(cls.tenant)

    def setUp(self):
        self.tenant_admin = Group.objects.get(name=TENANT_ADMIN)
        # `seed_tenant` 灌的超管管理员：pk 最小，且 `is_account_admin` 对它恒为真。
        self.seeded_admin = User.objects.get(username="admin")
        self.client = make_logged_in_client(username="guard-admin")
        self.actor = User.objects.get(username="guard-admin")
        self.actor.groups.add(self.tenant_admin)
        # 一个**不是**管理员的活跃账号：用于「零管理员死角」与判据层用例（不会触发 ③）。
        self.kefu_client = make_logged_in_client(username="guard-kefu")
        self.kefu = User.objects.get(username="guard-kefu")
        self.kefu.groups.add(Group.objects.get(name=SERVICE))
        self.game = Game.objects.create(name="王者荣耀")

    # ── 夹具 ───────────────────────────────────────────────────────
    def _make_admin(self, username):
        user = User.objects.create_user(username=username, password="pw-123456")
        user.groups.add(self.tenant_admin)
        return user

    def _status(self, user, is_active, client=None):
        return (client or self.client).post(
            f"/api/accounts/{user.pk}/status/",
            {"is_active": is_active},
            content_type="application/json",
        ).json()

    def _batch_status(self, users, is_active):
        return self.client.post(
            "/api/accounts/status/",
            {"ids": [u.pk for u in users], "is_active": is_active},
            content_type="application/json",
        ).json()

    def _patch_role(self, user, role, client=None):
        return (client or self.client).patch(
            f"/api/accounts/{user.pk}/",
            {"role": role},
            content_type="application/json",
        ).json()

    def _deactivate_all_admins(self):
        """把一个租户**直接改库**成零活跃管理员（§6.2.2 登记的绕过形态之一）。

        应用层做不到这件事（正是本任务要保证的），所以构造这个死角只能绕过本代码路径。
        只停用管理员，`guard-kefu` 保持活跃——死角要证的是「活跃的非管理员也进不去」。
        """
        admin_pks = list(
            User.objects.filter(Q(is_superuser=True) | Q(groups__name=TENANT_ADMIN))
            .values_list("pk", flat=True)
            .distinct()
        )
        User.objects.filter(pk__in=admin_pks).update(is_active=False)

    # ── 夹具自证（先跑，否则下面的负断言可能是「夹具不成立」凑出来的）────
    def test_the_fixture_starts_with_exactly_two_active_admins(self):
        """自证构造物：`admin`（超管）与 actor（持 Group）**都**算活跃管理员。"""
        self.assertTrue(is_account_admin(self.seeded_admin))
        self.assertTrue(is_account_admin(self.actor))
        self.assertFalse(is_account_admin(self.kefu))
        self.assertEqual(active_admin_count(), 2)

    def test_lock_key_is_derived_from_the_schema_name(self):
        """锁键来自 schema 名：同租户稳定、跨租户（这里是 public）不同。

        这是「租户之间不互相阻塞」的机制本身；键相同就等于全库共用一把锁。
        """
        tenant_key = _tenant_lock_key()
        self.assertIsInstance(tenant_key, int)
        self.assertLessEqual(-(2**63), tenant_key)
        self.assertLess(tenant_key, 2**63)          # pg 的 bigint 入参范围
        self.assertEqual(
            tenant_key,
            int.from_bytes(hashlib.sha256(b"test").digest()[:8], "big", signed=True),
        )
        with schema_context("public"):
            public_key = _tenant_lock_key()
        self.assertNotEqual(tenant_key, public_key)
        self.assertEqual(_tenant_lock_key(), tenant_key)   # 退出上下文后回到本租户

    # ── ③ 自锁 ─────────────────────────────────────────────────────
    def test_cannot_disable_self(self):
        """③：不允许自己停用自己（比不变式更严，spec §6.2-③ / §9-5 挂待确认）。"""
        body = self._status(self.actor, False)
        self.assertEqual(body["code"], SELF_LOCKOUT)
        self.actor.refresh_from_db()
        self.assertTrue(self.actor.is_active)          # 断言行未变
        # 正向对照：同一个请求换**别人**当目标就成功（`admin` 之外还有 actor 在，
        # 不变式也满足）——所以上面的 1004 不是「这个端点压根不能停用账号」。
        self.assertEqual(self._status(self.seeded_admin, False)["code"], 0)

    def test_cannot_demote_self(self):
        """③ 的另一半：不允许摘掉自己的「系统管理员」Group。"""
        body = self._patch_role(self.actor, SERVICE)
        self.assertEqual(body["code"], SELF_LOCKOUT)
        self.assertEqual(
            list(self.actor.groups.values_list("name", flat=True)), [TENANT_ADMIN]
        )
        # 正向对照：同一 verb 改自己的**显示名**必须放行（③ 只锁停用与降级）。
        self.assertEqual(
            self.client.patch(
                f"/api/accounts/{self.actor.pk}/",
                {"display_name": "我还是管理员"},
                content_type="application/json",
            ).json()["code"],
            0,
        )

    def test_self_rename_and_game_change_are_allowed(self):
        """③ 只锁「停用」与「降级」，**不锁**改自己的显示名与游戏权限。"""
        body = self.client.patch(
            f"/api/accounts/{self.actor.pk}/",
            {"display_name": "我是管理员", "games": [self.game.pk]},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        self.actor.refresh_from_db()
        self.assertEqual(self.actor.first_name, "我是管理员")
        # ⚠️ 断言**取值**而不是 `assertIsNotNone`：后者对 `False` 也通过
        #    （任何 bool 都不是 None），把「职位被摘掉了」写成绿色。
        self.assertIs(is_account_admin(self.actor), True)      # 职位没被动过
        self.assertTrue(self.actor.groups.filter(name=TENANT_ADMIN).exists())

    # ── ①/② 的正向对照（负断言没有它就等于「把所有停用都拒了」也能全绿）──
    def test_can_disable_another_admin_when_another_remains(self):
        """反证①：两个管理员时停用其中一个必须**放行**。"""
        body = self._status(self.seeded_admin, False)
        self.assertEqual(body["code"], 0)
        self.seeded_admin.refresh_from_db()
        self.assertFalse(self.seeded_admin.is_active)
        # 断言落在库上：剩下的活跃管理员恰好是 actor 一个。
        self.assertEqual(active_admin_count(), 1)
        self.assertTrue(is_account_admin(self.actor))

    def test_can_demote_another_admin_when_another_remains(self):
        """反证②：非超管管理员被别人降级时，只要还剩一个活跃管理员就必须放行。

        这是 ② 的**真实可达形态**（降级自己走 ③ 的 1004；降级最后一个不可达，见模块说明）。
        """
        other = self._make_admin("guard-demote-other")
        self.assertEqual(active_admin_count(), 3)
        body = self._patch_role(other, SERVICE)
        self.assertEqual(body["code"], 0)
        self.assertEqual(list(other.groups.values_list("name", flat=True)), [SERVICE])
        self.assertFalse(is_account_admin(other))
        self.assertEqual(active_admin_count(), 2)

    def test_superuser_without_the_group_is_no_longer_an_admin(self):
        """摘掉「系统管理员」Group 的超管**不再是**管理员（判据 = 「持该职位」≠
        `is_superuser`，这一摘就是**真降级**）。

        放行仍成立（actor `guard-admin` 自己还是管理员），但断言落在
        "降级之后他不再是管理员"。`is_superuser` 不可经 API 变更，
        所以「降级」只有「摘 Group」一种形态。
        """
        body = self._patch_role(self.seeded_admin, SERVICE)
        self.assertEqual(body["code"], 0)
        self.seeded_admin.refresh_from_db()
        self.assertTrue(self.seeded_admin.is_superuser)   # 标志位没被动过
        self.assertFalse(
            self.seeded_admin.groups.filter(name=TENANT_ADMIN).exists(),
            "构造失败：Group 没被摘掉，本用例证不了口径变更",
        )
        self.assertIs(is_account_admin(self.seeded_admin), False)

    def test_disabling_a_non_admin_is_unaffected(self):
        body = self._status(self.kefu, False)
        self.assertEqual(body["code"], 0)
        self.kefu.refresh_from_db()
        self.assertFalse(self.kefu.is_active)

    # ── ① 的端点层可达形态：批量把**所有**活跃管理员一起停用 ────────────
    def test_batch_disabling_every_active_admin_is_rejected_as_a_whole(self):
        """① + 批量正确性：整批被拒，且**一行都不许落库**（spec §9-10）。

        批量按 id 升序校验，`admin` 的 pk 比 actor 小 ⇒ 先被判定的是 `admin` 那一行，
        它命中 ①（1003）而不是 ③（1004）。

        ⚠️ 判别力（如实）：`also_losing` 漏掉时，`admin` 那一轮会因为「actor 还在」
        而放行，整批随后被 actor 那一轮的 ③ 拦成 **1004** —— 所以本用例钉的是
        **码值 1003**（`also_losing` 真在起作用），而「整批被拒」这一点在 ③ 存在时
        由 ③ 兜底。`also_losing` 改变「放行/拒绝」的算术由下面判据层的用例钉。
        """
        self.assertLess(self.seeded_admin.pk, self.actor.pk)   # 本用例的前提：判定顺序
        body = self._batch_status([self.seeded_admin, self.actor], False)
        self.assertEqual(body["code"], LAST_ADMIN)
        # 断言打在数据库行上（E-0009）：「先校验、后应用」意味着连**前一半**都没写。
        self.assertTrue(User.objects.get(pk=self.seeded_admin.pk).is_active)
        self.assertTrue(User.objects.get(pk=self.actor.pk).is_active)
        self.assertEqual(active_admin_count(), 2)

    def test_batch_disabling_all_but_one_admin_succeeds(self):
        """① 的正向对照：同一批量去掉一个（仍剩一个活跃管理员）就必须成功。

        没有它，上面那条「整批被拒」可能是「批量碰管理员就一律拒」凑出来的。
        """
        other = self._make_admin("guard-batch-other")
        self.assertEqual(active_admin_count(), 3)
        body = self._batch_status([self.seeded_admin, other], False)
        self.assertEqual(body["code"], 0)
        self.assertFalse(User.objects.get(pk=self.seeded_admin.pk).is_active)
        self.assertFalse(User.objects.get(pk=other.pk).is_active)
        self.assertTrue(User.objects.get(pk=self.actor.pk).is_active)
        self.assertEqual(active_admin_count(), 1)

    def test_batch_losing_set_counts_only_currently_active_admins(self):
        """`also_losing` 只收**当前活跃**的管理员：已经停用的管理员不能重复扣一次。

        形态（界面上真实可勾出来的）：批量停用「一个活跃管理员 + 两行**已停用**的管理员」，
        租户里还有两个活跃管理员（`admin` 与 actor）⇒ 变更后仍有 2 个 ⇒ **必须放行**。

        判别力：把已停用的那两行也扣一次，计数从 2 变成 0，整批被误拒成 1003
        （实测：去掉 `u.is_active` 这一条过滤时，本用例红、码值 1003）。
        """
        active = self._make_admin("guard-losing-active")
        already_off = [
            self._make_admin(f"guard-losing-off-{index}") for index in range(2)
        ]
        User.objects.filter(pk__in=[u.pk for u in already_off]).update(is_active=False)
        self.assertEqual(active_admin_count(), 3)      # admin + actor + active

        body = self._batch_status([active, *already_off], False)
        self.assertEqual(body["code"], 0)
        self.assertFalse(User.objects.get(pk=active.pk).is_active)
        for user in already_off:
            self.assertFalse(User.objects.get(pk=user.pk).is_active)
        # 变更后仍有管理员（`admin` 与 actor）——这才是「必须放行」的理由。
        self.assertEqual(active_admin_count(), 2)

    # ── 锁序：advisory 必须在行锁之前 ────────────────────────────────
    def test_batch_takes_the_advisory_lock_before_the_row_locks(self):
        """批量只取一次租户锁，而且必须排在账号行锁之前。"""
        with CaptureQueriesContext(connection) as captured:
            body = self._batch_status([self.kefu], False)      # 非管理员：批量本身必须成功
        self.assertEqual(body["code"], 0)

        sql = [entry["sql"] for entry in captured.captured_queries]
        advisory = [i for i, statement in enumerate(sql) if "pg_advisory_xact_lock" in statement]
        row_locks = [i for i, statement in enumerate(sql) if "FOR UPDATE" in statement]
        # 先自证两条语句都真的在这条请求里出现过——否则下面的 `assertLess` 可能由
        # 「某一边压根没发出」而 vacuous 通过（索引取不到会 IndexError，但先说清意图）。
        self.assertEqual(len(advisory), 1, f"这条请求应只取一次 advisory 锁：{sql}")
        self.assertTrue(row_locks, f"这条请求没有取行锁：{sql}")
        self.assertLess(
            advisory[0],
            row_locks[0],
            "advisory 锁发在行锁之后了——与 update 路径的锁序相反，会成环（HTML 500）",
        )

    # ── 零管理员死角（§6.2.3）────────────────────────
    def test_zero_active_admin_tenant_gets_a_distinguishable_code(self):
        """零活跃管理员的租户：活跃的**非管理员**也要拿到可区分的 1003，不是困惑的 403。

        ⚠️ 构造必须让请求者**保持活跃**（`guard-kefu` 不是管理员，不在被停用之列）：
        若把请求者自己也停用，请求会变成 401（会话解析不出活跃用户），
        测到的是「未认证」而不是这条分支（计划 A7 里的写法就是那样，会以 401 失败）。
        """
        # 正向对照先跑：这些管理员此刻确实能正常访问（夹具是健康的）。
        self.assertEqual(self.client.get("/api/accounts/").json()["code"], 0)

        self._deactivate_all_admins()
        self.kefu.refresh_from_db()
        self.assertTrue(self.kefu.is_active)          # 请求者仍活跃——本用例的前提
        self.assertEqual(active_admin_count(), 0)      # 自证构造物

        response = self.kefu_client.get("/api/accounts/")
        self.assertEqual(response.status_code, 200)     # 业务码，不是传输层的 403
        body = response.json()
        self.assertEqual(body["code"], LAST_ADMIN)
        self.assertIn("平台", body["msg"])              # 要知道去找谁修

        # 反向对照：租户恢复出一个活跃管理员后，同一条请求回到 403（而不是 1003）——
        # 所以上面的 1003 只由「零管理员」这一个条件触发。
        User.objects.filter(username="admin").update(is_active=True)
        self.assertEqual(active_admin_count(), 1)
        forbidden = self.kefu_client.get("/api/accounts/")
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.json()["code"], 403)

    def test_anonymous_request_on_a_zero_admin_tenant_is_still_401(self):
        """零管理员**不得**把未认证放宽成业务码：匿名仍是 401（fail-closed 未被削弱）。"""
        self._deactivate_all_admins()
        self.assertEqual(active_admin_count(), 0)
        anonymous = Client(SERVER_NAME=DOMAIN)
        response = anonymous.get("/api/accounts/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], 401)


class AdminInvariantArithmeticTests(TenantTestCase):
    """判据层：直接钉 `check_admin_invariant` 的分支算术。

    这里直接调守卫，和端点装配解开。人事持节点时 ①② 在端点层也走得到，
    那两条在文末；本类只钉分支算术。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)

    def setUp(self):
        self.tenant_admin = Group.objects.get(name=TENANT_ADMIN)
        self.seeded_admin = User.objects.get(username="admin")
        self.client = make_logged_in_client(username="guard-actor")
        self.actor = User.objects.get(username="guard-actor")
        self.actor.groups.add(self.tenant_admin)
        self.kefu = User.objects.create_user(username="guard-nonadmin", password="pw-123456")

    def _make_admin(self, username):
        user = User.objects.create_user(username=username, password="pw-123456")
        user.groups.add(self.tenant_admin)
        return user

    def test_guard_rejects_disabling_the_last_active_admin(self):
        """①：判定必须排掉**目标自己**（此刻它在库里仍是「管理员 + 活跃」）。"""
        self.actor.groups.clear()          # actor 退出管理员（这样 ③ 不会抢先）
        self.assertEqual(active_admin_count(), 1)      # 自证：只剩 seeded admin
        blocked = check_admin_invariant(
            self.kefu, self.seeded_admin, will_be_active=False, will_be_admin=True
        )
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.data["code"], LAST_ADMIN)

    def test_guard_rejects_demoting_the_last_active_admin(self):
        """②：用**变更后**的状态算（摘 Group ⇒ 该行不再是管理员）。"""
        self.actor.groups.clear()
        self.assertEqual(active_admin_count(), 1)
        blocked = check_admin_invariant(
            self.kefu, self.seeded_admin, will_be_active=True, will_be_admin=False
        )
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.data["code"], LAST_ADMIN)

    def test_guard_allows_when_another_active_admin_remains(self):
        """① ② 的正向对照：还剩一个别的活跃管理员 ⇒ 放行（`None`，不是某个信封）。"""
        self.actor.groups.clear()
        other = self._make_admin("guard-arithmetic-other")
        self.assertEqual(active_admin_count(), 2)
        self.assertIsNone(
            check_admin_invariant(
                self.kefu, self.seeded_admin, will_be_active=False, will_be_admin=True
            )
        )
        self.assertIsNone(
            check_admin_invariant(
                self.kefu, other, will_be_active=True, will_be_admin=False
            )
        )

    def test_guard_treats_other_batch_members_as_losing(self):
        """`also_losing` 是承重的：批量里**别的**成员不能算作「剩下的那一个」。

        本用例自带判别力——同一目标、同一状态，只在 `also_losing` 上分叉：
        不带它时「还剩 other」，带它时「other 也在被停用」⇒ 0 个 ⇒ 必须拒。
        """
        self.actor.groups.clear()
        other = self._make_admin("guard-arithmetic-batch")
        self.assertEqual(active_admin_count(), 2)      # seeded + other
        self.assertIsNone(
            check_admin_invariant(
                self.kefu, self.seeded_admin, will_be_active=False, will_be_admin=True
            )
        )
        blocked = check_admin_invariant(
            self.kefu,
            self.seeded_admin,
            will_be_active=False,
            will_be_admin=True,
            also_losing={other.pk},
        )
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.data["code"], LAST_ADMIN)

    def test_guard_does_not_block_a_self_rename(self):
        """③ 的边界：目标是自己、但**不动**活跃与管理员身份 ⇒ 放行。"""
        self.assertIsNone(
            check_admin_invariant(
                self.actor,
                self.actor,
                will_be_active=True,
                will_be_admin=is_account_admin(self.actor),
            )
        )


class HRActorOnTheAdminGuardTests(TenantTestCase):
    """⭐ **人事**（不是管理员、只持账号节点）当请求者时，① ② 照样拦得住。

    ⚠️ 准入换成 `IsAccountManager` 之后请求者不必是管理员 ⇒ ①② 在端点层可达，
    必须有端点层用例（否则一个把这两条判定删掉的实现照样全绿）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)

    def setUp(self):
        self.last_admin = User.objects.get(username="admin")
        self.hr_client = make_logged_in_client(
            username="hr-guard", groups=(HR_ROLE,), nodes=HR_NODES
        )
        self.hr = User.objects.get(username="hr-guard")

    def test_the_fixture_has_exactly_one_active_admin_and_the_hr_is_not_one(self):
        """夹具自证（否则「最后一个管理员被保护」可能是别的原因凑出来的）。"""
        self.assertEqual(active_admin_count(), 1)
        self.assertIs(self.last_admin.is_active, True)
        self.assertFalse(is_account_admin(self.hr))

    def test_an_hr_cannot_disable_the_last_active_admin(self):
        """① 的端点层可达形态：人事停用最后一个管理员 ⇒ **1003**，一行都不落库。"""
        response = self.hr_client.post(
            f"/api/accounts/{self.last_admin.pk}/status/",
            {"is_active": False},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], LAST_ADMIN, response.content[:300])
        self.last_admin.refresh_from_db()
        self.assertIs(self.last_admin.is_active, True)

        # 正向控制：停用一个**普通账号**（人事有 change_status 节点）⇒ 放行；
        # 否则上面的 1003 可能由「人事什么都不能停」凑出来。
        victim = User.objects.create_user(username="hr-disablee", password="pw-123456")
        allowed = self.hr_client.post(
            f"/api/accounts/{victim.pk}/status/",
            {"is_active": False},
            content_type="application/json",
        )
        self.assertEqual(allowed.json()["code"], 0, allowed.content[:300])
        victim.refresh_from_db()
        self.assertIs(victim.is_active, False)

    def test_an_hr_cannot_demote_the_last_active_admin(self):
        """② 的端点层可达形态：人事把最后一个管理员改成普通职位 ⇒ **1003**，职位不变。"""
        response = self.hr_client.patch(
            f"/api/accounts/{self.last_admin.pk}/",
            {"role": "客服"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], LAST_ADMIN, response.content[:300])
        self.last_admin.refresh_from_db()
        self.assertEqual(
            list(self.last_admin.groups.values_list("name", flat=True)), [TENANT_ADMIN]
        )


#: ⭐ F1 的「人事」形态：**只持 `system_user.add_edit`**（与本轮判据的措辞逐字一致）。
#: `create` 与 `partial_update` / `update` 的节点门都只要这一个 code
#: （`AccountViewSet.ACCOUNT_PERMISSIONS`），所以它就是「能改职位归属」的最小凭据。
GRANT_NODES = ("system_user.add_edit",)


class RoleGrantInvariantTests(TenantTestCase):
    """⭐ **非系统管理员不得授予「系统管理员」职位**。

    业务方口径：人事**子账号**能增 / 改 / 删子账户，⛔ **不是**「人事能把自己变成最高权限」
    （提权路径实测存在过，见 `apps/accounts/guards.py` 的边界登记），本类逐条钉住。

    ⚠️ 判据一律打在**真实出口 + 数据库行**上：⛔ 不只断状态码（本仓业务失败也是 HTTP 200），
    ⛔ 也不只断 `code`（`msg` 与终态各有自己的值钉子）。
    ⚠️ 这是**业务规则**、不是节点门：请求者的节点是够的、参数也合法，
    所以码是 `ErrorCode.BUSINESS`(1002) 而不是 403。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)

    def setUp(self):
        self.game = Game.objects.create(name="王者荣耀")
        # 请求者①：人事（非管理员，只持 add_edit）。
        self.hr_client = make_logged_in_client(
            username="grant-hr", groups=(HR_ROLE,), nodes=GRANT_NODES
        )
        self.hr = User.objects.get(username="grant-hr")
        # 请求者②：**另一个**系统管理员（正向对照；也让「管理员能授予」与不变式互不干扰）。
        self.admin_client = make_logged_in_client(username="grant-admin", games=None)
        self.admin = User.objects.get(username="grant-admin")
        self.admin.groups.add(Group.objects.get(name=TENANT_ADMIN))
        # 被编辑对象：一个普通「客服」账号（建档，便于断言职位行）。
        self.target = User.objects.create_user(username="grant-target", password="pw-123456")
        self.target.groups.add(Group.objects.get(name=SERVICE))

    # ── 夹具自证（先跑，否则下面的负数断言可能是夹具不成立凑出来的）────
    def test_the_fixture_is_an_hr_and_the_target_is_a_plain_account(self):
        self.assertFalse(is_account_admin(self.hr))
        self.assertFalse(is_account_admin(self.target))
        self.assertTrue(is_account_admin(self.admin))
        # 终态判据的前置：此刻首页概览对人事就是 403（提权被拒之后它必须**仍然**是 403）。
        before = self.hr_client.get(
            HOME_OVERVIEW_URL, {"start": "2026-09-10", "end": "2026-09-12"}
        )
        self.assertEqual(before.status_code, 403, before.content[:200])

    # ── 判据 1：人事**自我提权** ─────────────────────────────────────
    def test_an_hr_cannot_promote_itself_to_system_admin(self):
        """判据 1：人事 `PATCH` 自己成「系统管理员」⇒ 业务码 + 人话 msg，**终态**三项都不变。"""
        response = self.hr_client.patch(
            f"/api/accounts/{self.hr.pk}/",
            {"role": TENANT_ADMIN},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], ROLE_GRANT_DENIED, body)
        self.assertEqual(body["msg"], ROLE_GRANT_DENIED_MSG, body)
        self.assertIsNone(body["data"])

        # 终态①：**数据库行**里职位没变（不是「响应拒了、库里其实写了」）。
        self.assertEqual(
            list(
                User.objects.get(pk=self.hr.pk).groups.values_list("name", flat=True)
            ),
            [HR_ROLE],
        )
        # 终态②：管理员判据仍为假。
        self.assertFalse(is_account_admin(User.objects.get(pk=self.hr.pk)))
        # 终态③：首页数据概览**仍 403**（线 D 首版那条实测提权的反向钉子）。
        after = self.hr_client.get(
            HOME_OVERVIEW_URL, {"start": "2026-09-10", "end": "2026-09-12"}
        )
        self.assertEqual(after.status_code, 403, after.content[:200])
        self.assertEqual(after.json()["code"], 403, after.content[:200])

    # ── 判据 2：授予**别人**（`update` 与 `create` 两条路径）────────────
    def test_an_hr_cannot_grant_the_role_to_another_account(self):
        """判据 2：`PATCH` 别人与 `POST` 新建，两条路径都拒；终态都没有一个管理员被造出来。"""
        # ① `update` / `partial_update` 那条链
        patched = self.hr_client.patch(
            f"/api/accounts/{self.target.pk}/",
            {"role": TENANT_ADMIN},
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 200, patched.content[:200])
        self.assertEqual(patched.json()["code"], ROLE_GRANT_DENIED, patched.content[:300])
        self.assertEqual(patched.json()["msg"], ROLE_GRANT_DENIED_MSG)
        self.assertEqual(
            list(
                User.objects.get(pk=self.target.pk).groups.values_list("name", flat=True)
            ),
            [SERVICE],
        )
        self.assertFalse(is_account_admin(User.objects.get(pk=self.target.pk)))

        # ② `create` 那条链（⛔ 不许只改一处：两条路径各自要有判据）
        from tests.test_account_api import CREATE_REQUIRED_PROFILE_FIELDS

        created = self.hr_client.post(
            "/api/accounts/",
            {
                **CREATE_REQUIRED_PROFILE_FIELDS,
                "username": "grant-new-admin",
                "role": TENANT_ADMIN,
                "games": [self.game.pk],
            },
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 200, created.content[:200])
        self.assertEqual(created.json()["code"], ROLE_GRANT_DENIED, created.content[:300])
        self.assertEqual(created.json()["msg"], ROLE_GRANT_DENIED_MSG)
        # 终态：账号行**一行都没有**（判定排在 `atomic()` 之前 ⇒ 连口令都不生成）。
        self.assertFalse(User.objects.filter(username="grant-new-admin").exists())

    # ── 判据 3：**正向对照**——系统管理员照常能授予（含给自己）──────────
    def test_a_system_admin_can_grant_the_role_to_others_and_to_itself(self):
        """判据 3：⛔ 别把规则写成「谁都不能授予」。管理员给**别人**与**自己**都放行。"""
        granted = self.admin_client.patch(
            f"/api/accounts/{self.target.pk}/",
            {"role": TENANT_ADMIN},
            content_type="application/json",
        )
        self.assertEqual(granted.status_code, 200, granted.content[:200])
        self.assertEqual(granted.json()["code"], 0, granted.content[:300])
        self.assertTrue(is_account_admin(User.objects.get(pk=self.target.pk)))

        me = self.admin_client.patch(
            f"/api/accounts/{self.admin.pk}/",
            {"role": TENANT_ADMIN},
            content_type="application/json",
        )
        self.assertEqual(me.status_code, 200, me.content[:200])
        self.assertEqual(me.json()["code"], 0, me.content[:300])
        self.assertTrue(is_account_admin(User.objects.get(pk=self.admin.pk)))

    # ── 判据 4：规则**只**挡「系统管理员」这一个职位 ────────────────────
    def test_an_hr_can_still_assign_non_admin_roles_and_edit_other_fields(self):
        """判据 4：客服 / 代练员 / 人事照常能授予；不带 `role` 的编辑也不受影响。"""
        for role in ("客服", "代练员", HR_ROLE):
            with self.subTest(role=role):
                response = self.hr_client.patch(
                    f"/api/accounts/{self.target.pk}/",
                    {"role": role},
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertEqual(response.json()["code"], 0, response.content[:300])
                self.assertEqual(
                    list(
                        User.objects.get(pk=self.target.pk).groups.values_list(
                            "name", flat=True
                        )
                    ),
                    [role],
                )

        # 不带 `role` 的编辑：新规则不得顺手拦下（`role_name is None` ⇒ 放行）。
        from tests.test_account_api import CREATE_REQUIRED_PROFILE_FIELDS

        edited = self.hr_client.patch(
            f"/api/accounts/{self.target.pk}/",
            {"display_name": "人事改的"},
            content_type="application/json",
        )
        self.assertEqual(edited.status_code, 200, edited.content[:200])
        self.assertEqual(edited.json()["code"], 0, edited.content[:300])
        self.assertEqual(User.objects.get(pk=self.target.pk).first_name, "人事改的")

        # 新增一个**非管理员**账号：照常放行（终态打在数据库行上）。
        created = self.hr_client.post(
            "/api/accounts/",
            {
                **CREATE_REQUIRED_PROFILE_FIELDS,
                "username": "grant-new-kefu",
                "role": "客服",
                "games": [self.game.pk],
            },
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 200, created.content[:200])
        self.assertEqual(created.json()["code"], 0, created.content[:300])
        self.assertTrue(User.objects.filter(username="grant-new-kefu").exists())


#: ⭐ P0-1：「管理员账号只有管理员能动」的四句 `msg` **字面量**。
#: 码值与 F1 同族（复用 `ErrorCode.BUSINESS`(1002)），所以这里只钉文案 + 码值本身。
ADMIN_TARGET_MSG_RESET = "目标账号是系统管理员，只有系统管理员可以重置其登录密码"
ADMIN_TARGET_MSG_STATUS = "目标账号是系统管理员，只有系统管理员可以停用或启用其账号"
ADMIN_TARGET_MSG_DELETE = "目标账号是系统管理员，只有系统管理员可以删除其账号"
ADMIN_TARGET_MSG_ROLE = "目标账号是系统管理员，只有系统管理员可以修改其职位"

#: 固定重置口令（`apps/accounts/views.py::RESET_PASSWORD`）——**字面量**：它正是攻击者拿到的那个串。
RESET_PASSWORD_LITERAL = "123456"


class AdminTargetInvariantTests(TenantTestCase):
    """⭐ P0-1：非管理员（人事）**不得**让系统管理员失去身份或登录能力。

    ⚠️ 本类刻意用**内置「人事」职位**当请求者（`groups=("人事",)`）：评审实测的绕过路径
    正是它持 `system_user.reset_password` 把管理员口令改成固定 `123456` 再登录。
    夹具自证会断言它确实持有那个节点（`test_the_fixture_...`）。
    ⚠️ 判据一律打**真实出口 + 数据库行**：只断响应码会放过「回了拒信、库里其实改了」。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)

    def setUp(self):
        self.game = Game.objects.create(name="王者荣耀")
        # 请求者：**内置「人事」职位**（非管理员；持 system_user 栏目全部非 flag 点）。
        self.hr_client = make_logged_in_client(username="tgt-hr", groups=("人事",))
        self.hr = User.objects.get(username="tgt-hr")
        # 被动的**管理员**目标：自带**可用口令**（`seed_tenant` 的 `admin` 是
        # `set_unusable_password()`，`check_password` 对它恒为假 ⇒ 测不了「原口令仍有效」）。
        self.admin_target = User.objects.create_user(
            username="tgt-admin", password="old-pw-1234"
        )
        self.admin_target.groups.add(Group.objects.get(name=TENANT_ADMIN))
        # 管理员请求者（正向对照）。
        self.admin_client = make_logged_in_client(username="tgt-actor", games=None)
        self.admin_actor = User.objects.get(username="tgt-actor")
        self.admin_actor.groups.add(Group.objects.get(name=TENANT_ADMIN))
        # 普通子账户（正向对照：人事照常能管它）。
        self.plain = User.objects.create_user(username="tgt-kefu", password="pw-plain-1")
        self.plain.groups.add(Group.objects.get(name=SERVICE))

    def _pk(self, user):
        return user.pk

    def _groups_of(self, user):
        return list(
            User.objects.get(pk=user.pk).groups.values_list("name", flat=True)
        )

    def _profile_deleted_at(self, user):
        return StaffProfile.objects.filter(user_id=user.pk).values_list(
            "deleted_at", flat=True
        ).first()

    # ── 夹具自证 ───────────────────────────────────────────────────
    def test_the_fixture_hr_holds_the_reset_node_and_is_not_an_admin(self):
        from apps.rbac.permissions import nodes_of

        held = nodes_of(self.hr)
        self.assertIn("system_user.reset_password", held)   # 评审复现的那条路径
        self.assertIn("system_user.change_status", held)
        self.assertIn("system_user.add_edit", held)
        self.assertFalse(is_account_admin(self.hr))
        self.assertTrue(is_account_admin(self.admin_target))
        self.assertIs(self.admin_target.is_active, True)
        # 「原口令有效」这条终态判据的前置（否则下面的断言可能是口令本来就不可用凑出来的）。
        self.assertTrue(User.objects.get(pk=self.admin_target.pk).check_password("old-pw-1234"))

    # ── 判据 1：改口令（登录能力）────────────────────────────────────
    def test_an_hr_cannot_reset_an_admins_password(self):
        """判据 1：⛔ 只断响应码不够——**必须**断「原口令仍有效、123456 无效」。"""
        response = self.hr_client.post(
            f"/api/accounts/{self._pk(self.admin_target)}/reset-password/",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], ROLE_GRANT_DENIED, body)
        self.assertEqual(body["msg"], ADMIN_TARGET_MSG_RESET, body)
        self.assertIsNone(body["data"])

        row = User.objects.get(pk=self.admin_target.pk)
        self.assertTrue(row.check_password("old-pw-1234"), "原口令被改掉了")
        self.assertFalse(
            row.check_password(RESET_PASSWORD_LITERAL),
            "管理员的口令被重置成固定 123456 —— 这正是评审实测的提权路径",
        )

    # ── 判据 2：停用 / 软删（登录能力 + 身份）────────────────────────
    def test_an_hr_cannot_disable_or_delete_an_admin(self):
        """判据 2：单个启停、批量启停、软删三条路径都被拒；终态 `is_active=True`、
        `deleted_at is None`、职位仍是「系统管理员」。"""
        # ① 单个停用
        status = self.hr_client.post(
            f"/api/accounts/{self._pk(self.admin_target)}/status/",
            {"is_active": False},
            content_type="application/json",
        )
        self.assertEqual(status.status_code, 200, status.content[:200])
        self.assertEqual(status.json()["code"], ROLE_GRANT_DENIED, status.content[:300])
        self.assertEqual(status.json()["msg"], ADMIN_TARGET_MSG_STATUS)
        self.assertIs(User.objects.get(pk=self.admin_target.pk).is_active, True)
        # ② 反向（启用）也拦：统一口径 —— 见 `check_admin_target_invariant` 的注释
        enable = self.hr_client.post(
            f"/api/accounts/{self._pk(self.admin_target)}/status/",
            {"is_active": True},
            content_type="application/json",
        )
        self.assertEqual(enable.json()["code"], ROLE_GRANT_DENIED, enable.content[:300])
        # ③ 批量启停（`status_batch` 与 `status` 共用 `_apply_status` 里那一处判定）
        batch = self.hr_client.post(
            "/api/accounts/status/",
            {"ids": [self.admin_target.pk], "is_active": False},
            content_type="application/json",
        )
        self.assertEqual(batch.status_code, 200, batch.content[:200])
        self.assertEqual(batch.json()["code"], ROLE_GRANT_DENIED, batch.content[:300])
        self.assertEqual(batch.json()["msg"], ADMIN_TARGET_MSG_STATUS)
        self.assertIs(User.objects.get(pk=self.admin_target.pk).is_active, True)
        # ④ 软删
        deleted = self.hr_client.post(
            f"/api/accounts/{self._pk(self.admin_target)}/delete/"
        )
        self.assertEqual(deleted.status_code, 200, deleted.content[:200])
        self.assertEqual(deleted.json()["code"], ROLE_GRANT_DENIED, deleted.content[:300])
        self.assertEqual(deleted.json()["msg"], ADMIN_TARGET_MSG_DELETE)
        self.assertIs(User.objects.get(pk=self.admin_target.pk).is_active, True)
        self.assertIsNone(self._profile_deleted_at(self.admin_target), "被拒的删除留下了 deleted_at")

    # ── 判据 3：降级（身份）──────────────────────────────────────────
    def test_an_hr_cannot_demote_an_admin(self):
        """判据 3：人事 `PATCH` 管理员的 `role` ⇒ 被拒，职位行仍是「系统管理员」。"""
        response = self.hr_client.patch(
            f"/api/accounts/{self._pk(self.admin_target)}/",
            {"role": "客服"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], ROLE_GRANT_DENIED, body)
        self.assertEqual(body["msg"], ADMIN_TARGET_MSG_ROLE, body)
        self.assertEqual(self._groups_of(self.admin_target), [TENANT_ADMIN])

    # ── 判据 4：人事照常能管**普通**子账户 ───────────────────────────
    def test_an_hr_can_still_manage_a_plain_account(self):
        """判据 4：⛔ 别把规则写成「人事什么都不能做」——四个动作在普通账号上照旧 200。"""
        from tests.test_account_api import CREATE_REQUIRED_PROFILE_FIELDS

        # 改口令
        made = User.objects.create_user(username="tgt-plain-reset", password="pw-old-1")
        made.groups.add(Group.objects.get(name=SERVICE))
        reset = self.hr_client.post(f"/api/accounts/{made.pk}/reset-password/")
        self.assertEqual(reset.status_code, 200, reset.content[:200])
        self.assertEqual(reset.json()["code"], 0, reset.content[:300])
        self.assertTrue(User.objects.get(pk=made.pk).check_password(RESET_PASSWORD_LITERAL))

        # 停用（终态打 DB 行）
        disabled = User.objects.create_user(username="tgt-plain-status", password="pw-old-2")
        disabled.groups.add(Group.objects.get(name=SERVICE))
        status = self.hr_client.post(
            f"/api/accounts/{disabled.pk}/status/",
            {"is_active": False},
            content_type="application/json",
        )
        self.assertEqual(status.json()["code"], 0, status.content[:300])
        self.assertIs(User.objects.get(pk=disabled.pk).is_active, False)

        # 改职位
        edited = User.objects.create_user(username="tgt-plain-role", password="pw-old-3")
        edited.groups.add(Group.objects.get(name=SERVICE))
        role = self.hr_client.patch(
            f"/api/accounts/{edited.pk}/",
            {"role": "代练员"},
            content_type="application/json",
        )
        self.assertEqual(role.json()["code"], 0, role.content[:300])
        self.assertEqual(self._groups_of(edited), ["代练员"])

        # 软删（终态：`deleted_at` 非空 + 停用）
        killed = User.objects.create_user(username="tgt-plain-delete", password="pw-old-4")
        killed.groups.add(Group.objects.get(name=SERVICE))
        deleted = self.hr_client.post(f"/api/accounts/{killed.pk}/delete/")
        self.assertEqual(deleted.json()["code"], 0, deleted.content[:300])
        self.assertIsNotNone(self._profile_deleted_at(killed))
        self.assertIs(User.objects.get(pk=killed.pk).is_active, False)

        # 新建普通账号也照常（`create` 不经过本判据；它由 F1 的「授予管理员」规则管）
        created = self.hr_client.post(
            "/api/accounts/",
            {
                **CREATE_REQUIRED_PROFILE_FIELDS,
                "username": "tgt-plain-create",
                "role": "客服",
                "games": [self.game.pk],
            },
            content_type="application/json",
        )
        self.assertEqual(created.json()["code"], 0, created.content[:300])

    # ── 判据 5：管理员照旧全通（含既有 1003/1004 不退化）──────────────
    def test_a_system_admin_can_still_act_on_admins_and_plain_accounts(self):
        """判据 5：正向对照 —— 管理员对**管理员**与**普通账号**做同样四件事都 200。"""
        # ① 改另一个管理员的口令（终态：新口令生效）
        reset = self.admin_client.post(
            f"/api/accounts/{self.admin_target.pk}/reset-password/"
        )
        self.assertEqual(reset.status_code, 200, reset.content[:200])
        self.assertEqual(reset.json()["code"], 0, reset.content[:300])
        self.assertTrue(
            User.objects.get(pk=self.admin_target.pk).check_password(RESET_PASSWORD_LITERAL)
        )
        # ② 改另一个管理员的职位（还剩别的管理员 ⇒ 既有规则放行）
        role = self.admin_client.patch(
            f"/api/accounts/{self.admin_target.pk}/",
            {"role": "客服"},
            content_type="application/json",
        )
        self.assertEqual(role.json()["code"], 0, role.content[:300])
        self.assertEqual(self._groups_of(self.admin_target), [SERVICE])
        # ③ 停用另一个账号（普通账号）
        status = self.admin_client.post(
            f"/api/accounts/{self.plain.pk}/status/",
            {"is_active": False},
            content_type="application/json",
        )
        self.assertEqual(status.json()["code"], 0, status.content[:300])
        self.assertIs(User.objects.get(pk=self.plain.pk).is_active, False)
        # ④ 软删另一个账号
        deleted = self.admin_client.post(f"/api/accounts/{self.plain.pk}/delete/")
        self.assertEqual(deleted.json()["code"], 0, deleted.content[:300])
        self.assertIsNotNone(self._profile_deleted_at(self.plain))

    # ── 判据 6：只改**非职位**字段照旧放行 ────────────────────────────
    def test_an_hr_can_still_edit_a_non_position_field_of_an_admin(self):
        """判据 6：规则是「不能动管理员这个**身份**」，⛔ 不是「人事不能碰管理员这一行」。"""
        response = self.hr_client.patch(
            f"/api/accounts/{self.admin_target.pk}/",
            {"remark": "人事改的备注"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 0, response.content[:300])
        self.assertEqual(
            StaffProfile.objects.get(user_id=self.admin_target.pk).remark, "人事改的备注"
        )
        # 职位没被顺手改（判据打在库里的行上）。
        self.assertEqual(self._groups_of(self.admin_target), [TENANT_ADMIN])
