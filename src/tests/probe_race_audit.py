"""代码审查代理 M（并发普查，2026-10-24）· 本地并发实证探针（只读普查的可写附件）。

⛔ 不进 git 的探针；只打本地测试库（manage.py test 的 test DB，标准测试基础设施），
线程 2 个/用例，不迁移本地开发库、不碰生产。

探针 1（锁序倒置）：/claim 路径先拿**钱包行锁**再在 apply_transition 拿 **Order 行锁**
（`claim_order` → `cancel_release_on_claim` → … → `apply_transition`），而 /assign 路径
先拿 **Order 行锁**再经 `set_taker` → `cancel_release_on_claim` 拿**钱包行锁**。
同一张「待抢单」上两路并发 ⇒ 经典 AB-BA。T1 用真 `claim_order`（enforce_deposit=False，
但 cancel_release 仍无条件拿钱包锁——前提是钱包行存在）；T2 复刻 `assign_order` 的锁序
（apply_transition → set_taker）。预期：PostgreSQL 死锁检测击杀一方（约 1s）。

探针 2（/adjust/ 的 is_settled TOCTOU）：`/adjust/`（api/performance_views.py:758 判据、
:915 写入）在**事务外**读 `perf.is_settled`、**无行锁**地 save 金额列；而结账侧
（settlement.py:444）持 `select_for_update(of=("self",))`。T1 复刻结账侧行级核心，
T2 复刻 /adjust/ 的「先判后写」。预期：T2 在 T1 提交后仍写进了 temp_amount
⇒ 「已结账不许动钱」闸被绕过 + settled_amount 快照与金额列脱钩。

运行：cd src && PYTHONPATH=../.tools/recon/code-audit/2026-10-24 \
  uv run python manage.py test probe_race_audit -v 2 --parallel 1
"""
import threading
import time
import traceback
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from django_tenants.utils import (
    get_tenant_domain_model,
    get_tenant_model,
    schema_context,
)

from apps.accounts.models import TakerWallet
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance, SettleStatus
from apps.business.claim import claim_order
from apps.business.order_status import apply_transition
from apps.business.performance import set_taker

TENANT_SCHEMA = "code_audit_probe"


class RaceAuditProbe(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("migrate_schemas", schema_name="public", interactive=False, verbosity=0)
        cls.tenant = get_tenant_model()(schema_name=TENANT_SCHEMA)
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(tenant=cls.tenant, domain="probe.test.local")
        cls.domain.save()
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def _run_thread(self, target) -> dict:
        box = {}
        def _wrap():
            connections.close_all()
            with schema_context(TENANT_SCHEMA):
                try:
                    box["result"] = target()
                except Exception as exc:  # noqa: BLE001 —— 结果要落到字典里
                    box["result"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                    box["tb"] = traceback.format_exc()[-400:]
                finally:
                    connections.close_all()
        t = threading.Thread(target=_wrap)
        t.start()
        return box, t

    # ── 探针 1：/claim（钱包锁→Order 锁）vs /assign（Order 锁→钱包锁）────────
    def test_probe1_claim_vs_assign_deadlock(self):
        game = Game.objects.create(name="原神")
        order = Order.objects.create(
            game=game, order_no="20261024000000000001",
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("88.00"),
        )
        Performance.objects.create(order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00"))
        taker = User.objects.create_user(username="probe_taker")
        TakerWallet.objects.get_or_create(user=taker)  # cancel_release 只有行存在才会拿锁
        order_id, taker_id = order.id, taker.id

        wallet_locked = threading.Event()
        results = {}

        def _claim():
            with transaction.atomic():
                # 预抓钱包行锁（claim_order 内 cancel_release_on_claim 拿的就是这一把）
                TakerWallet.objects.select_for_update().get(user_id=taker_id)
                wallet_locked.set()
                time.sleep(0.6)  # 让 T2 拿到 Order 行锁并睡进窗口
                claim_order(
                    user_id=taker_id, order_id=order_id,
                    nodes={"grab_hall.take_order"}, enforce_deposit=False,
                )
            return "ok"

        def _assign():
            with transaction.atomic():
                fresh = Order.objects.get(pk=order_id)
                apply_transition(fresh, OrderStatus.IN_PROGRESS, nodes={"order.assign_taker"})
                time.sleep(0.6)  # 让 T1 推进到它的 apply_transition（阻塞在 Order 锁上）
                perf = Performance.objects.get(order_id=order_id, emp_type=EmpType.MAIN)
                set_taker(perf, User.objects.get(pk=taker_id))
            return "ok"

        box1, t1 = self._run_thread(_claim)
        self.assertTrue(wallet_locked.wait(timeout=5), "T1 没拿到钱包锁")
        box2, t2 = self._run_thread(_assign)
        t1.join(timeout=30); t2.join(timeout=30)
        results["claim"] = box1.get("result")
        results["assign"] = box2.get("result")
        print(f"\n[probe1] claim={results['claim']!r} assign={results['assign']!r}")

        err = [r for r in results.values() if r and "OperationalError" in str(r) and "deadlock" in str(r).lower()]
        self.assertTrue(err, f"预期至少一方死锁被杀，实际: {results}")
        # 终态自洽：归属与状态要么是 claim 的、要么是 assign 的，不存在半截
        perf = Performance.objects.get(order_id=order_id, emp_type=EmpType.MAIN)
        order.refresh_from_db()
        print(f"[probe1] 终态 perf.user_id={perf.user_id} order.status={order.status}")

    # ── 探针 2：/adjust/ 的 is_settled 判据（事务外、无锁）vs 结账行锁 ────────
    def test_probe2_adjust_vs_settle_toctou(self):
        game = Game.objects.create(name="幻塔")
        order = Order.objects.create(
            game=game, order_no="20261024000000000002",
            status=OrderStatus.IN_PROGRESS, hall_price=Decimal("50.00"),
        )
        taker = User.objects.create_user(username="probe_taker2")
        perf = Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=taker,
            temp_amount=Decimal("20.00"),
        )
        perf_id = perf.id
        read_done = threading.Event()
        results = {}

        def _adjust():  # 复刻 /adjust/ 形状：事务外判 is_settled → 睡窗口 → 无锁 save 金额
            with schema_context(TENANT_SCHEMA):
                perf = Performance.objects.get(pk=perf_id)
                if perf.is_settled:
                    return "guard-rejected-at-check"
                read_done.set()
                time.sleep(1.0)  # 窗口：结账在这一睡里提交
                with transaction.atomic():
                    perf.temp_amount = Decimal("9.99")
                    perf.save(update_fields=["temp_amount", "updated_at"])
            return "adjust-written"

        def _settle():  # 复刻结账侧行级核心（settlement.py:444 的锁 + 快照写）
            read_done.wait(timeout=5)
            with transaction.atomic():
                locked = Performance.objects.select_for_update().get(pk=perf_id)
                time.sleep(0.5)
                locked.settle_status = SettleStatus.SETTLED
                locked.settled_at = timezone.now()
                locked.settled_by = "probe_cs"
                locked.settled_amount = locked.temp_amount  # 快照 = 当时读到 20.00
                locked.save(update_fields=["settle_status", "settled_at", "settled_by", "settled_amount"])
            return "settled"

        box1, t1 = self._run_thread(_adjust)
        box2, t2 = self._run_thread(_settle)
        t1.join(timeout=30); t2.join(timeout=30)
        results["adjust"] = box1.get("result")
        results["settle"] = box2.get("result")
        print(f"\n[probe2] adjust={results['adjust']!r} settle={results['settle']!r}")

        self.assertEqual(results["settle"], "settled")
        self.assertEqual(results["adjust"], "adjust-written", f"/adjust/ 的闸在并发下没被绕过: {results}")
        final = Performance.objects.get(pk=perf_id)
        print(f"[probe2] 终态 is_settled={final.is_settled} settled_amount={final.settled_amount} temp_amount={final.temp_amount}")
        self.assertTrue(final.is_settled)
        self.assertEqual(final.temp_amount, Decimal("9.99"))
        self.assertEqual(final.settled_amount, Decimal("20.00"))
