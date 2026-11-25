"""`fix_missing_taker_performance` 的判据（标准档 / 动钱 / **默认 dry-run**）。

六条判据（每条都能红）：

1. 已完成 + MAIN 行 `user` 为空 + 有鸡蛋铺链路 ⇒ dry-run **零写入**；
   `--commit` 后 `user` = 鸡蛋铺账号、绩效金额 **> 0**（本例钉字面量 `100.00`）；
2. peer 链路的单 ⇒ 挂团团通账号；
3. 已结账的 MAIN 行 ⇒ **跳过且一行不改**；
4. 两条链路都没有 ⇒ 跳过并在输出里**打印订单号 + 原因**（⛔ 不猜）；
5. 重复执行 ⇒ 第二次 **零写入**（`--commit` 第二次命中 0 条）；
6. 找不到目标账号 ⇒ `CommandError`（**响亮失败**，不是静默跳过）。

另加两条：`--limit` 真的限批；鸡蛋铺与 peer 链路同时存在时**鸡蛋铺优先**。

⭐ 生产事故回归：重走状态会把 `Order.completed_at` 清空、最后一步再写成
`timezone.now()` ⇒ 46 张已完成单的「完单时间」被改成了修复时刻（原本是 10-11～10-25）。
两条判据：

7. 原完单时间**有值** ⇒ `--commit` 后仍是**原值**（且状态 / 接手人 / 金额都到位）；
8. 原完单时间**为空** ⇒ 仍按现状写成修复时刻（⛔ 别为了修 7 把这个分支改坏）。

两条同时钉住输出里那行「原完单时间=… ⇒ 保持原值 / 写为修复时刻」（以后核对用）。

⭐ 真值治理：源码不再内置任何默认接手人（改前写死过某租户的生产账号名）
⇒ 三条判据：

9. argparse 缺省 = `None`、模块**无** `DEFAULT_*` 常量（有人把名字写回默认值 ⇒ 红）；
10. 环境变量给了名字 ⇒ 不带 CLI 参数也能跑；
11. CLI 参数与环境变量**都没给**而该线有候选 ⇒ `CommandError` 响亮报错（⛔ 不打兜底名硬跑）。

⚠️ 断言一律打在**数据库行**上（E-0009），⛔ 不看 logger；夹具值一律合成值（E-0077）。
"""
import os
from argparse import ArgumentParser
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

import apps.business.management.commands.fix_missing_taker_performance as fix_cmd
from apps.audit.models import OperationLog
from apps.business.management.commands.fix_missing_taker_performance import (
    ENV_JIDANPU_USER,
    ENV_PEER_USER,
)
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    Order,
    OrderStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
    Performance,
    ReviewStatus,
    SettleStatus,
)
from apps.business.peer_sync import hash_token, new_token
from tests.p5_auth_helpers import grant_games

#: 两条线的目标账号名（**合成值**，E-0077）。真值治理起，源码不再内置任何
#: 默认接手人 ⇒ 测试与运维一样**显式给名字**（`_run` 自动带上这两个参数；
#: 「没给会怎样」的路径由本文件末尾的报错用例单独钉住）。
JIDANPU_NAME = "夹具鸡蛋铺系统"
PEER_NAME = "夹具团团通系统"

#: 20 位纯数字（`OrderViewSet.lookup_value_regex` 的形状）。
ORDER_NO_A = "20261027000000010001"
ORDER_NO_B = "20261027000000010002"
ORDER_NO_C = "20261027000000010003"


class FixMissingTakerPerformanceTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="修复夹具游戏")
        # 目标接手人：**按名字**能被命令找到（默认查询口径 = `username` 或 `first_name`）。
        self.jidanpu_account = User.objects.create_user(
            username="fix-jdp-account", password="pw-fix-jdp"
        )
        self.jidanpu_account.first_name = JIDANPU_NAME
        self.jidanpu_account.save(update_fields=["first_name"])
        self.peer_account = User.objects.create_user(
            username="fix-peer-account", password="pw-fix-peer"
        )
        self.peer_account.first_name = PEER_NAME
        self.peer_account.save(update_fields=["first_name"])
        # `assign_order` 的游戏范围闸（D-0252）：目标账号必须持有本夹具的游戏。
        grant_games(self.jidanpu_account, self.game)
        grant_games(self.peer_account, self.game)

        self.link_owner = User.objects.create_user(username="fix-link-owner")
        self.jidanpu_binding = JidanpuMemberBinding.objects.create(
            user=self.link_owner,
            jidanpu_base_url="https://jdp-fix.test.invalid",
            account="acc-fix-jdp",
            password="pw-fix-jdp-binding",
            enabled=True,
        )
        self.peer_binding = PeerUpstreamBinding.objects.create(
            peer_name="修复上家",
            peer_base_url="https://peer-fix.test.invalid",
            peer_account="peer-acc-fix",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://local-fix.test.invalid",
            bound_by=self.link_owner,
        )
        self._seq = 0

    # ── 夹具 ─────────────────────────────────────────────────────────────
    def _order(self, order_no=None, *, status=OrderStatus.COMPLETED, hall_price="100.00"):
        self._seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=order_no or f"2026102700000002{self._seq:04d}",
            status=status,
            hall_price=Decimal(hall_price),
            total_price=Decimal("100.00"),
        )

    def _blank_main(self, order, **overrides):
        """生产实测的那条脏行：MAIN 存在、`user` 为空、金额 0、未结账、`review_status=NULL`。"""
        fields = {
            "order": order,
            "emp_type": EmpType.MAIN,
            "user": None,
            "temp_amount": Decimal("0.00"),
            "bonus_amount": Decimal("0.00"),
            "settle_status": SettleStatus.UNSETTLED,
            "review_status": None,
        }
        fields.update(overrides)
        return Performance.objects.create(**fields)

    def _link_jidanpu(self, order):
        return JidanpuMemberOrderLink.objects.create(
            binding=self.jidanpu_binding,
            order=order,
            jidanpu_order_id=f"JDP-{order.order_no}",
        )

    def _link_peer(self, order):
        return PeerOrderLink.objects.create(
            order=order,
            upstream_binding=self.peer_binding,
            peer_order_no=f"PEER-{order.order_no}",
            origin_order_no=f"PEER-{order.order_no}",
        )

    def _run(self, *args):
        """运维常态形态：像环境变量给过名字一样，自动带上两个接手人参数。

        调用方已显式给过某个参数（如「找不到账号」用例）⇒ 不重复注入。
        """
        final = list(args)
        if "--jidanpu-user" not in final:
            final += ["--jidanpu-user", JIDANPU_NAME]
        if "--peer-user" not in final:
            final += ["--peer-user", PEER_NAME]
        return self._run_bare(*final)

    def _run_bare(self, *args):
        """原样调用（不补任何参数）——「没给名字」的路径用这个。"""
        out = StringIO()
        call_command(
            "fix_missing_taker_performance",
            "--schema", connection.schema_name,
            *args,
            stdout=out,
        )
        return out.getvalue()

    # ── ① 鸡蛋铺线：dry-run 零写入 + commit 后 user / 金额 ──────────────
    def test_jidanpu_line_dry_run_writes_nothing_then_commit_sets_taker_and_amount(self):
        order = self._order(ORDER_NO_A)
        perf = self._blank_main(order)
        self._link_jidanpu(order)
        before = (
            order.status,
            order.updated_at,
            perf.user_id,
            perf.temp_amount,
            perf.bonus_amount,
            perf.review_status,
            OperationLog.objects.count(),
        )

        out = self._run()  # 默认 dry-run

        self.assertIn("dry-run", out)
        self.assertIn("将修复=1", out)
        self.assertIn(order.order_no, out)
        self.assertIn("鸡蛋铺", out)
        self.assertIn(JIDANPU_NAME, out)
        # 完单时间那行在 dry-run 里必须是「将…」（⛔ 别把计划打印成已发生）。
        self.assertIn("原完单时间=空 ⇒ 将写为修复时刻", out)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(
            (
                order.status,
                order.updated_at,
                perf.user_id,
                perf.temp_amount,
                perf.bonus_amount,
                perf.review_status,
                OperationLog.objects.count(),
            ),
            before,
        )

        self._run("--commit")

        order.refresh_from_db()
        perf.refresh_from_db()
        # 终态：仍是已完成（重走只是把「待验收 → 已完成」真的再走一遍）。
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(perf.user_id, self.jidanpu_account.pk)
        # 金额：发单价 100.00 × 提成率 100%（无档案回落全额）⇒ 预计与生效都是 100.00。
        self.assertEqual(perf.temp_amount, Decimal("100.00"))
        self.assertEqual(perf.bonus_amount, Decimal("100.00"))
        self.assertEqual(perf.review_status, ReviewStatus.APPROVED)
        # 台账：每张成功单恰好一条，且用的是**既有**动作码 `performance.assign`。
        logs = OperationLog.objects.filter(order_no=order.order_no)
        self.assertEqual(logs.count(), 1)
        self.assertEqual(logs.get().action, "performance.assign")

    # ── ② peer 线：挂团团通账号 ────────────────────────────────────────
    def test_peer_line_uses_the_peer_system_account(self):
        order = self._order(ORDER_NO_A)
        perf = self._blank_main(order)
        self._link_peer(order)

        out = self._run("--commit")

        self.assertIn("团团通", out)
        perf.refresh_from_db()
        self.assertEqual(perf.user_id, self.peer_account.pk)
        self.assertEqual(perf.temp_amount, Decimal("100.00"))
        self.assertEqual(perf.bonus_amount, Decimal("100.00"))

    # ── ③ 已结账行：跳过且一行不改 ────────────────────────────────────
    def test_a_settled_main_row_is_skipped_and_untouched(self):
        order = self._order(ORDER_NO_A)
        perf = self._blank_main(order, settle_status=SettleStatus.SETTLED)
        self._link_jidanpu(order)
        before = (
            order.status,
            order.updated_at,
            perf.user_id,
            perf.temp_amount,
            perf.bonus_amount,
            perf.settle_status,
            perf.updated_at,
            OperationLog.objects.count(),
        )

        out = self._run("--commit")

        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(
            (
                order.status,
                order.updated_at,
                perf.user_id,
                perf.temp_amount,
                perf.bonus_amount,
                perf.settle_status,
                perf.updated_at,
                OperationLog.objects.count(),
            ),
            before,
        )
        self.assertIn("跳过=1", out)
        self.assertIn("已结账", out)

    # ── ④ 无链路：跳过并打印（⛔ 不猜）────────────────────────────────
    def test_an_order_without_any_link_is_skipped_and_printed(self):
        order = self._order(ORDER_NO_A)
        perf = self._blank_main(order)

        out = self._run("--commit")

        perf.refresh_from_db()
        self.assertIsNone(perf.user_id)
        self.assertEqual(OperationLog.objects.count(), 0)
        self.assertIn("跳过=1", out)
        self.assertIn(order.order_no, out)
        self.assertIn("无链路", out)

    # ── ⑤ 幂等：第二次零写入 ──────────────────────────────────────────
    def test_the_second_commit_writes_nothing(self):
        order = self._order(ORDER_NO_A)
        perf = self._blank_main(order)
        self._link_jidanpu(order)
        self._run("--commit")

        perf.refresh_from_db()
        order.refresh_from_db()
        # ⚠️ **阳性对照**：第一次必须**真的修好了**，否则「第二次零写入」是空转。
        # 变异实测：把候选收窄条件从 `user__isnull=True` 改坏时，
        # 缺了这两条断言的本用例会**静默绿**（第一次也没修，第二次自然也没写）。
        self.assertEqual(perf.user_id, self.jidanpu_account.pk)
        self.assertEqual(OperationLog.objects.filter(order_no=order.order_no).count(), 1)
        logs = OperationLog.objects.count()
        snapshot = (
            order.status,
            order.updated_at,
            perf.user_id,
            perf.temp_amount,
            perf.bonus_amount,
            perf.review_status,
            perf.updated_at,
        )

        out = self._run("--commit")

        perf.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(OperationLog.objects.count(), logs)
        self.assertEqual(
            (
                order.status,
                order.updated_at,
                perf.user_id,
                perf.temp_amount,
                perf.bonus_amount,
                perf.review_status,
                perf.updated_at,
            ),
            snapshot,
        )
        self.assertIn("成功=0", out)

    # ── ⑥ 找不到目标账号：响亮失败 ────────────────────────────────────
    def test_a_missing_target_account_is_a_loud_command_error(self):
        order = self._order(ORDER_NO_A)
        perf = self._blank_main(order)
        self._link_jidanpu(order)

        with self.assertRaises(CommandError) as caught:
            self._run("--jidanpu-user", "不存在的修复账号")

        self.assertIn("找不到鸡蛋铺系统账号", str(caught.exception))
        # 失败发生在**写任何东西之前**。
        perf.refresh_from_db()
        self.assertIsNone(perf.user_id)
        self.assertEqual(OperationLog.objects.count(), 0)

    # ── 补充：--limit 真的限批 ────────────────────────────────────────
    def test_limit_caps_how_many_orders_are_repaired(self):
        orders = [
            self._order(order_no) for order_no in (ORDER_NO_A, ORDER_NO_B, ORDER_NO_C)
        ]
        for order in orders:
            self._blank_main(order)
            self._link_jidanpu(order)

        out = self._run("--commit", "--limit", "2")

        self.assertIn("成功=2", out)
        repaired = Performance.objects.filter(
            emp_type=EmpType.MAIN, user=self.jidanpu_account
        ).count()
        self.assertEqual(repaired, 2)
        # 第 3 张仍是候选（留给下一轮，⛔ 不静默漏掉）。
        self.assertEqual(
            Performance.objects.filter(
                order=orders[-1], emp_type=EmpType.MAIN, user__isnull=True
            ).count(),
            1,
        )

    # ── 补充：两条链路都有 ⇒ 鸡蛋铺优先（判定只有一处，顺序写死）──────
    def test_a_jidanpu_link_wins_when_both_links_exist(self):
        order = self._order(ORDER_NO_A)
        perf = self._blank_main(order)
        self._link_peer(order)
        self._link_jidanpu(order)

        self._run("--commit")

        perf.refresh_from_db()
        self.assertEqual(perf.user_id, self.jidanpu_account.pk)

    # ── ⑦ 生产事故回归：原完单时间必须保住 ────────────────────────────
    def test_a_completed_order_keeps_its_original_completed_at(self):
        """重走状态会清 `completed_at`、最后一步又写 `now()` ⇒ 原值必须写回。

        生产实测：46 张已完成单的完单时间被改成了修复时刻。本用例把原来的
        时刻（5 天前）打进夹具 ⇒ 修复后库里必须**还是那一刻**；⛔ 不接受「不为空就算过」
        （那正是事故形态：不为空、但是修复时刻）。
        """
        order = self._order(ORDER_NO_A)
        original_completed_at = timezone.now() - timedelta(days=5)
        Order.objects.filter(pk=order.pk).update(
            completed_at=original_completed_at
        )
        perf = self._blank_main(order)
        self._link_jidanpu(order)

        out = self._run("--commit")

        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.completed_at, original_completed_at)
        # 阳性对照：其余终态都到位（否则「保住了时间」可能只是整单没修）。
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(perf.user_id, self.jidanpu_account.pk)
        self.assertEqual(perf.temp_amount, Decimal("100.00"))
        self.assertEqual(perf.bonus_amount, Decimal("100.00"))
        self.assertIn("成功=1", out)
        # 输出里能核对：原完单时间 → 保持原值（⛔ 不打印成「写为修复时刻」）。
        self.assertIn("原完单时间=", out)
        self.assertIn("保持原值", out)
        self.assertNotIn("写为修复时刻", out)

    # ── ⑧ 原完单时间为空：仍按现状写成修复时刻（⛔ 别改坏这一档）─────
    def test_an_order_without_a_completed_at_gets_the_repair_moment(self):
        order = self._order(ORDER_NO_A)
        self.assertIsNone(order.completed_at)
        perf = self._blank_main(order)
        self._link_jidanpu(order)

        started_at = timezone.now()
        out = self._run("--commit")

        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertIsNotNone(order.completed_at)
        # 「修复时刻」= 本次重走「待验收 → 已完成」的那一下（不早于用例开始）。⚠️ 判据用
        # 区间而不是「等于 started_at」：`apply_transition` 写的是它自己的 `now()`。
        self.assertGreaterEqual(order.completed_at, started_at)
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(perf.user_id, self.jidanpu_account.pk)
        # 输出口径同样要如实：原来为空 ⇒ 写为修复时刻。
        self.assertIn("原完单时间=空", out)
        self.assertIn("写为修复时刻", out)
        self.assertNotIn("保持原值", out)

    # ── 真值治理：默认接手人不再内置在源码里 ──────────────
    def test_no_builtin_default_taker_names(self):
        """源码不得再内置任何默认接手人。

        判据两条腿（否则「把名字写回默认值」不会红）：
        ① argparse 的两个缺省值都是 `None`（写死 `default=<名字>` ⇒ 红）；
        ② 模块里没有 `DEFAULT_JIDANPU_USER` / `DEFAULT_PEER_USER` 常量
           （新挂一个常量再接回默认值 ⇒ 红）。
        """
        self.assertFalse(hasattr(fix_cmd, "DEFAULT_JIDANPU_USER"))
        self.assertFalse(hasattr(fix_cmd, "DEFAULT_PEER_USER"))
        parser = ArgumentParser()
        fix_cmd.Command().add_arguments(parser)
        args = parser.parse_args([])
        self.assertIsNone(args.jidanpu_user)
        self.assertIsNone(args.peer_user)

    def test_env_var_supplies_the_default_taker_name(self):
        """环境变量给了名字 ⇒ 不带 CLI 参数也能跑（与兄弟命令同一种配置入口）。"""
        order = self._order(ORDER_NO_A)
        perf = self._blank_main(order)
        self._link_jidanpu(order)

        with patch.dict(
            os.environ, {ENV_JIDANPU_USER: JIDANPU_NAME, ENV_PEER_USER: PEER_NAME}
        ):
            out = self._run_bare()

        self.assertIn("将修复=1", out)
        self.assertIn(JIDANPU_NAME, out)
        # dry-run：一行没写。
        perf.refresh_from_db()
        self.assertIsNone(perf.user_id)

    def test_no_taker_name_anywhere_is_a_loud_command_error(self):
        """CLI 参数与环境变量**都没给**而该线有候选 ⇒ `CommandError`（⛔ 不打兜底名硬跑）。"""
        order = self._order(ORDER_NO_A)
        perf = self._blank_main(order)
        self._link_jidanpu(order)

        with patch.dict(os.environ, {ENV_JIDANPU_USER: "", ENV_PEER_USER: ""}):
            with self.assertRaises(CommandError) as caught:
                self._run_bare()

        self.assertIn("没有指定鸡蛋铺系统账号", str(caught.exception))
        self.assertIn("--jidanpu-user", str(caught.exception))
        self.assertIn(ENV_JIDANPU_USER, str(caught.exception))
        # 失败发生在**写任何东西之前**。
        perf.refresh_from_db()
        self.assertIsNone(perf.user_id)
        self.assertEqual(OperationLog.objects.count(), 0)
