"""`rewrite_promoted_source_no`（D-0342 存量补写）的行为网：干跑 / 实跑 / 六道候选闸。

**为什么要有这条命令**：D-0325 隐藏单转正支在 D-0342）之前不改写
`source_order_no` ⇒ 存量转正单按 D-0272 全端口径搜不到（业务方报障现场：
涉事租户两张 IN_PROGRESS 真实订单按下游看到的展示号在本租户「搜不到」；租户名等真值
只在 docs 与 `.tools/recon/`——真值守卫只扫 `src/**`，E-0077 口径）。本命令一次性补写存量，
执行需业务方批准；本文件钉它的**判据本身**（dry-run 零写入、实跑幂等、六道闸各一档）。

判据一律打**数据库行**（订单列 / 台账行），夹具一律合成值（全零段单号、`…test.invalid`
域名）；`call_command` 走真入口（⛔ 不直接 import 私有函数当被测对象——`plan_rewrites`
另有一条直达用例兜「枚举层」的闸）。
"""
from __future__ import annotations

from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuUpstreamHallOrder,
    Order,
    OrderStatus,
    PeerOrderLink,
)
from apps.business.order_no import generate_order_no

BASE_TIME = datetime(2026, 10, 30, 8, 0, tzinfo=dt_timezone.utc)
BASE_URL = "https://rewrite-up.test.invalid"

INTERNAL = "33160000000000002001"
VIEW = "51270000000000002001"
VIEW_TAKEN = "51270000000000002002"
HAND_EDITED = "HAND-EDITED-0001"


def _run(*args) -> str:
    out = StringIO()
    call_command("rewrite_promoted_source_no", *args, stdout=out)
    return out.getvalue()


class RewritePromotedSourceNoTests(TenantTestCase):
    """命令主行为网：干跑零写入、实跑改值+台账、重跑幂等。"""

    def setUp(self):
        from django.contrib.auth import get_user_model

        from apps.business.models import Game

        self.user = get_user_model().objects.create_user(
            username="rw-cmd-owner", password="pw-canary-rw",
        )
        self.game = Game.objects.create(name="改写命令夹具游戏")
        self.mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL,
            upstream_order_id=INTERNAL,
            status=JidanpuUpstreamHallOrder.Status.PROMOTED,
            game=self.game,
            upstream_payload={"orderId": INTERNAL, "orderIdView": VIEW},
            first_seen_at=BASE_TIME,
        )
        self.order = Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("88.00"), source_order_no=INTERNAL,
        )
        self.mirror.promoted_order = self.order
        self.mirror.save(update_fields=["promoted_order"])

    def _ledgers(self):
        from apps.business.models import JidanpuSyncEvent

        return JidanpuSyncEvent.objects.filter(
            kind="upstream.promoted_source_rewrite"
        )

    def test_dry_run_prints_plan_and_writes_nothing(self):
        """`--dry-run`：逐行打印计划、订单一字不改、台账 0 行。"""
        output = _run("--dry-run")
        self.order.refresh_from_db()
        self.assertEqual(self.order.source_order_no, INTERNAL)
        self.assertEqual(self._ledgers().count(), 0)
        self.assertIn("dry-run", output)
        self.assertIn(VIEW, output)
        self.assertIn("合计 会改写（dry-run，未落库） 1 张", output)

    def test_execute_rewrites_and_notes_one_ledger(self):
        """实跑：改值 + 每单恰一条台账（payload 带新旧值与镜像 pk）。"""
        output = _run()
        self.order.refresh_from_db()
        self.assertEqual(self.order.source_order_no, VIEW)
        self.assertIn("已改写", output)
        ledger = self._ledgers().get()
        self.assertEqual(ledger.target_type, "order")
        self.assertEqual(ledger.target_ref, self.order.order_no)
        self.assertEqual(ledger.payload["internal"], INTERNAL)
        self.assertEqual(ledger.payload["view"], VIEW)
        self.assertEqual(ledger.payload["mirror_pk"], self.mirror.pk)
        self.assertIn("D-0342", ledger.reason)

    def test_rerun_is_a_noop(self):
        """幂等：第二次实跑零候选（判据 3 已筛掉）⇒ 零写入、台账仍 1 行。"""
        _run()
        output = _run()
        self.order.refresh_from_db()
        self.assertEqual(self.order.source_order_no, VIEW)
        self.assertEqual(self._ledgers().count(), 1)
        self.assertIn("合计 已改写 0 张", output)

    # ── 六道候选闸 ──────────────────────────────────────────────────────────

    def test_skips_when_payload_has_no_view(self):
        self.mirror.upstream_payload = {"orderId": INTERNAL}
        self.mirror.save(update_fields=["upstream_payload"])
        output = _run()
        self.order.refresh_from_db()
        self.assertEqual(self.order.source_order_no, INTERNAL)
        self.assertIn("没有展示号", output)

    def test_skips_already_compliant_orders(self):
        self.order.source_order_no = VIEW
        self.order.save(update_fields=["source_order_no"])
        output = _run()
        self.assertEqual(self._ledgers().count(), 0)
        self.assertIn("已是展示号", output)

    def test_skips_hand_edited_source(self):
        """指纹闸：人工改过的来源号（非内部号也非空）一律尊重。"""
        self.order.source_order_no = HAND_EDITED
        self.order.save(update_fields=["source_order_no"])
        output = _run()
        self.order.refresh_from_db()
        self.assertEqual(self.order.source_order_no, HAND_EDITED)
        self.assertIn("人工改", output)

    def test_skips_when_view_already_taken_by_another_order(self):
        """查重闸：目标展示号被另一张未删单占用 ⇒ 跳过（B4 重号档）。"""
        Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("10.00"), source_order_no=VIEW,
        )
        output = _run()
        self.order.refresh_from_db()
        self.assertEqual(self.order.source_order_no, INTERNAL)
        self.assertIn("占用", output)

    def test_skips_orders_with_a_peer_link(self):
        """peer 闸：有 `PeerOrderLink` 行 ⇒ 跳过（对端存旧值，改写会断回传解析）。"""
        from django.contrib.auth import get_user_model

        from apps.business.models import PeerDownstreamBinding

        account = get_user_model().objects.create_user(
            username="rw-cmd-peer-acct", password="pw-canary-rw",
        )
        downstream = PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url="https://peer-rw.test.invalid",
            token_in_hash="hash-rw-canary",
            token_out="out-rw-canary",
        )
        PeerOrderLink.objects.create(
            order=self.order, downstream_binding=downstream,
            peer_order_no="PEER-0001",
        )
        output = _run()
        self.order.refresh_from_db()
        self.assertEqual(self.order.source_order_no, INTERNAL)
        self.assertIn("peer 链路", output)

    def test_skips_soft_deleted_orders(self):
        self.order.deleted_at = BASE_TIME
        self.order.deleted_by = "rw-test"
        self.order.save(update_fields=["deleted_at", "deleted_by"])
        output = _run()
        self.assertIn("软删", output)
        self.assertEqual(self._ledgers().count(), 0)

    def test_member_links_do_not_block_and_survive_the_rewrite(self):
        """有**鸡蛋铺 member** 链路（D-0325 的常态）⇒ 照改；链路行原样保留
        （回传按链路远端单号寻址，不靠 source 列——方案 §3 的结论落一行判据）。"""
        from django.contrib.auth import get_user_model

        member_user = get_user_model().objects.create_user(
            username="rw-cmd-member", password="pw-canary-rw",
        )
        binding = JidanpuMemberBinding.objects.create(
            user=member_user, jidanpu_base_url="https://rewrite-member.test.invalid",
            account="acc-rw-member", password="pw-canary", enabled=True,
        )
        link = JidanpuMemberOrderLink.objects.create(
            binding=binding, order=self.order,
            jidanpu_order_id="JDP-RW-00000000000001", in_hall=False,
        )
        _run()
        self.order.refresh_from_db()
        self.assertEqual(self.order.source_order_no, VIEW)
        link.refresh_from_db()
        self.assertEqual(link.jidanpu_order_id, "JDP-RW-00000000000001")


class PlanRewritesGateTests(TenantTestCase):
    """枚举层直达用例：候选 / 跳过的返回形状（命令层六闸的行为判据在上面那类）。"""

    def setUp(self):
        from apps.business.management.commands.rewrite_promoted_source_no import (
            plan_rewrites,
        )

        from apps.business.models import Game

        self.plan_rewrites = plan_rewrites
        game = Game.objects.create(name="改写命令枚举夹具游戏")
        self.mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL,
            upstream_order_id=INTERNAL,
            status=JidanpuUpstreamHallOrder.Status.PROMOTED,
            game=game,
            upstream_payload={"orderIdView": VIEW_TAKEN},
            first_seen_at=BASE_TIME,
        )
        self.order = Order.objects.create(
            order_no=generate_order_no(), game=game,
            status=OrderStatus.PENDING_REVIEW,
            total_price=Decimal("1.00"), source_order_no=INTERNAL,
        )
        self.mirror.promoted_order = self.order
        self.mirror.save(update_fields=["promoted_order"])

    def test_blank_source_is_a_candidate(self):
        """空白来源号（人工清空过）也算候选：D-0272 口径下它比展示号更差。"""
        self.order.source_order_no = ""
        self.order.save(update_fields=["source_order_no"])
        candidates, _skipped = self.plan_rewrites()
        self.assertEqual([row[1].pk for row in candidates], [self.order.pk])

    def test_mirror_without_promoted_order_is_ignored_silently(self):
        """`promoted_order` 为空的 promoted 行（数据被手改的畸形）⇒ 不进候选也不报错。"""
        self.mirror.promoted_order = None
        self.mirror.save(update_fields=["promoted_order"])
        candidates, skipped = self.plan_rewrites()
        self.assertEqual(candidates, [])
        self.assertEqual(skipped, [])
