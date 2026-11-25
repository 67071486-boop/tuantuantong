"""A 族占链老单迁链命令 `migrate_member_link_to_survivor`（D-0370 / E-0315）的判据网。

钉住的契约（判据出处 = 命令模块 docstring「迁什么」一节）：

- **干跑零写**：默认只打印清单（迁移 / 人工核对 / 跳过三档），一个字节都不改；
- **恰好一张才迁**：同有效来源单号的存活候选恰好一张 ⇒ `--apply` 把链行迁指过去
  （同时重置 in_hall/listed_at/withdrawn_at，`binding` 与远端单号不动）；
- **歧义不迁不报错**：候选 0 张 / ≥2 张 / 候选在本绑定上已有链行 ⇒ 落「人工核对」
  档、零写库；
- **幂等**：迁完之后再跑，链行的现行单已是存活单 ⇒ 「跳过」档，零写库；
- 老单仍存活 ⇒ 「跳过」档（本命令只处置「链行现行单已死」的那一族）；
- 我方另一终态（已完成）与**软删**的单都不算存活候选。

⚠️ 夹具一律合成值（来源单号 `SYNTH-SRC-*`、远端单号 `JDP-MIG-*`、域名 `*.test.local`）。
⚠️ 夹具一律在 `tenant_context` 里建（租户 schema；照 `test_order_qr_fetch_queue` 先例）。
"""
import io
from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import (
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    Order,
    OrderStatus,
)
from apps.platform.provisioning import provision_tenant

SCHEMA_A = "mig_a"
DOMAIN_A = "mig-a.test.local"
BASE_URL = "https://jidanpu-mig.test.invalid"
#: 有效来源单号（老单与存活单共用这一把钥匙——认领预读查的就是它）。
SOURCE_KEY = "SYNTH-SRC-2026-1103-0001"
SOURCE_KEY_ALT = "SYNTH-SRC-2026-1103-0002"
REMOTE_ID = "JDP-MIG-0001"


def _order_no(seq: int) -> str:
    return f"2026110300000000{seq:04d}"


class MigrateMemberLinkToSurvivorTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 建租户必须在 setUpClass：schema DDL 进不了 TestCase 的事务（会原地报错）。
        cls.tenant, _ = provision_tenant(
            schema_name=SCHEMA_A, name="迁链租户A", domain=DOMAIN_A
        )

    # ── 夹具（一律 tenant_context）───────────────────────────────────────
    def _order(self, seq, *, status, source="", deleted_at=None):
        with tenant_context(self.tenant):
            game = Game.objects.create(name=f"迁链合成游戏-{seq}")
            return Order.objects.create(
                game=game,
                order_no=_order_no(seq),
                status=status,
                hall_price="12.34",
                total_price="50.00",
                source_order_no=source,
                deleted_at=deleted_at,
            )

    def _binding(self):
        from django.contrib.auth.models import User

        with tenant_context(self.tenant):
            user = User.objects.create_user(username="mig-user", password="pw-mig")
            return JidanpuMemberBinding.objects.create(
                user=user, jidanpu_base_url=BASE_URL,
                account="acc-mig", password="pw-mig", enabled=True,
            )

    def _old_link(self, old_order, *, in_hall=True, withdrawn_at=None):
        with tenant_context(self.tenant):
            return JidanpuMemberOrderLink.objects.create(
                binding=self.binding, order=old_order,
                jidanpu_order_id=REMOTE_ID, in_hall=in_hall,
                listed_at=timezone.now() if in_hall else None,
                withdrawn_at=withdrawn_at,
            )

    def _extra_link(self, order, remote_id):
        with tenant_context(self.tenant):
            return JidanpuMemberOrderLink.objects.create(
                binding=self.binding, order=order,
                jidanpu_order_id=remote_id, in_hall=False,
            )

    def _run(self, *args) -> str:
        buf = io.StringIO()
        call_command(
            "migrate_member_link_to_survivor", *args,
            f"--schema={self.tenant.schema_name}", stdout=buf,
        )
        return buf.getvalue()

    def _link_row(self):
        with tenant_context(self.tenant):
            return JidanpuMemberOrderLink.objects.get(
                binding=self.binding, jidanpu_order_id=REMOTE_ID
            )

    def _link_count(self):
        with tenant_context(self.tenant):
            return JidanpuMemberOrderLink.objects.filter(binding=self.binding).count()

    def setUp(self):
        super().setUp()
        self.binding = self._binding()


# ── 用例 ─────────────────────────────────────────────────────────────────────


class MigrateDryRunTests(MigrateMemberLinkToSurvivorTests):
    def test_dry_run_prints_plan_and_writes_nothing(self):
        """干跑零写：清单打印出迁移行（带老单/存活单/远端号/绑定 id），链行一字不动。"""
        old = self._order(1, status=OrderStatus.REFUNDED, source=SOURCE_KEY)
        self._old_link(old)
        survivor = self._order(2, status=OrderStatus.WAITING_GRAB, source=SOURCE_KEY)
        out = self._run()
        self.assertIn("[迁移]", out)
        self.assertIn(REMOTE_ID, out)
        self.assertIn(f"绑定 #{self.binding.pk}", out)
        self.assertIn(_order_no(1), out)
        self.assertIn(_order_no(2), out)
        self.assertIn("dry-run 未写库", out)
        self.assertEqual(self._link_row().order_id, old.pk, "干跑不许改链行")


class MigrateApplyTests(MigrateMemberLinkToSurvivorTests):
    def test_apply_migrates_when_exactly_one_survivor(self):
        """恰好一张存活候选 ⇒ 迁指存活单 + 生命周期三列重置；binding 与远端号不动。"""
        old = self._order(1, status=OrderStatus.REFUNDED, source=SOURCE_KEY)
        self._old_link(
            old, in_hall=True, withdrawn_at=timezone.now() - timedelta(hours=3)
        )
        survivor = self._order(2, status=OrderStatus.WAITING_GRAB, source=SOURCE_KEY)
        out = self._run("--apply")
        self.assertIn("已写库", out)
        row = self._link_row()
        self.assertEqual(row.order_id, survivor.pk)
        self.assertEqual(row.jidanpu_order_id, REMOTE_ID, "远端单号不动（认领的钥匙）")
        self.assertEqual(row.binding_id, self.binding.pk)
        self.assertFalse(row.in_hall)
        self.assertIsNone(row.listed_at)
        self.assertIsNone(row.withdrawn_at, "老单「已退成」的历史不属于新单")

    def test_rerun_after_apply_is_idempotent_skip(self):
        """幂等：迁完之后再跑，链行的现行单已是存活单 ⇒ 「跳过」档、零写库。"""
        old = self._order(1, status=OrderStatus.REFUNDED, source=SOURCE_KEY)
        self._old_link(old)
        survivor = self._order(2, status=OrderStatus.WAITING_GRAB, source=SOURCE_KEY)
        self._run("--apply")
        out = self._run("--apply")
        self.assertIn("[跳过]", out)
        self.assertNotIn("[迁移]", out)
        self.assertEqual(self._link_row().order_id, survivor.pk)

    def test_live_old_order_is_skipped(self):
        """老单还活着 ⇒ 跳过档（本命令只处置「链行现行单已死」的那一族）。"""
        old = self._order(1, status=OrderStatus.IN_PROGRESS, source=SOURCE_KEY)
        self._old_link(old)
        self._order(2, status=OrderStatus.WAITING_GRAB, source=SOURCE_KEY)
        out = self._run("--apply")
        self.assertIn("[跳过]", out)
        self.assertEqual(self._link_row().order_id, old.pk)


class MigrateManualListTests(MigrateMemberLinkToSurvivorTests):
    def test_ambiguous_candidates_are_not_migrated(self):
        """两张同来源存活候选（歧义）⇒ 人工核对档、--apply 也零写库、不报错。"""
        old = self._order(1, status=OrderStatus.REFUNDED, source=SOURCE_KEY)
        self._old_link(old)
        self._order(2, status=OrderStatus.WAITING_GRAB, source=SOURCE_KEY)
        self._order(3, status=OrderStatus.IN_PROGRESS, source=SOURCE_KEY)
        out = self._run("--apply")
        self.assertIn("[人工核对]", out)
        self.assertIn("歧义", out)
        self.assertEqual(self._link_row().order_id, old.pk)

    def test_zero_candidates_are_not_migrated(self):
        """同来源没有任何存活单 ⇒ 人工核对档、零写库。"""
        old = self._order(1, status=OrderStatus.REFUNDED, source=SOURCE_KEY_ALT)
        self._old_link(old)
        out = self._run("--apply")
        self.assertIn("同来源存活候选 0 张", out)
        self.assertEqual(self._link_row().order_id, old.pk)

    def test_survivor_already_linked_on_this_binding_is_manual(self):
        """候选在本绑定上已有链行（uniq_jdp_member_link_order 会拒）⇒ 人工核对、零写库。"""
        old = self._order(1, status=OrderStatus.REFUNDED, source=SOURCE_KEY)
        self._old_link(old)
        survivor = self._order(2, status=OrderStatus.WAITING_GRAB, source=SOURCE_KEY)
        self._extra_link(survivor, "JDP-MIG-OTHER")
        out = self._run("--apply")
        self.assertIn("已有链行", out)
        self.assertIn("[人工核对]", out)
        self.assertEqual(self._link_row().order_id, old.pk)
        self.assertEqual(self._link_count(), 2, "⛔ 不删任何既有行来腾位置")

    def test_completed_survivor_is_not_a_candidate(self):
        """「已完成」也是我方终态 ⇒ 不算存活候选（⛔ 不把活链行指到另一张死单）。"""
        old = self._order(1, status=OrderStatus.REFUNDED, source=SOURCE_KEY)
        self._old_link(old)
        self._order(2, status=OrderStatus.COMPLETED, source=SOURCE_KEY)
        out = self._run("--apply")
        self.assertIn("同来源存活候选 0 张", out)

    def test_soft_deleted_old_is_dead_and_soft_deleted_candidate_excluded(self):
        """软删老单也是「已死」那一档；软删的候选单不算存活（⛔ 不迁给一张删掉的单）。"""
        old = self._order(
            1, status=OrderStatus.WAITING_GRAB, source=SOURCE_KEY,
            deleted_at=timezone.now(),
        )
        self._old_link(old)
        self._order(
            2, status=OrderStatus.WAITING_GRAB, source=SOURCE_KEY,
            deleted_at=timezone.now(),
        )
        out = self._run("--apply")
        self.assertIn("同来源存活候选 0 张", out)
        self.assertIn("[人工核对]", out)
