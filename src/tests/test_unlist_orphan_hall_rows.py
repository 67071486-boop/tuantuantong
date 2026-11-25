"""停绑批量下架孤儿镜像卡（僵尸窗治理⑤；计划 `docs/plans/2026-11-05-binding-disable-batch-unlist.md`）。

被测对象 = 生效点辅助 `jidanpu_upstream.unlist_orphan_hall_rows`（经行级 PUT 停用 /
member PUT 兼容路径停用 / 换绑三个生效点触发，落点 `_persist_binding_with_audit`）
+ 一次性管理命令 `unlist_orphan_hall_rows`（默认 dry-run，`--apply` 才落写）。

⭐ **安全核心 = 同地址接管保护**（计划 §1）：行地址上还有任一「启用中」的绑定 ⇒ 下一轮
`sync_hall` 按 A2 地址级去重会把它重新归属（`dedupe_owner`）⇒ 拆了反而把活卡杀掉 ⇒ 不拆。

判据打**数据库行**（镜像终态 / 灰卡 / 台账 / 隐藏单），stdout 只当辅助（AGENTS §5-G）；
检测一律 mock（`jidanpu_upstream.detect_binding`，E-0029：打在被查找的那个模块上），零真实网络；
夹具一律合成值（地址 `…test.invalid`、账号 `kefu-up-*`、单号全零段）。
⚠️ 期望值写**字面值**（E-0024 防自指）：台账成因句不 import 被测常量、另存一份字面副本。
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_upstream
from apps.business.models import (
    Game,
    HallExitSnapshot,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    Order,
    OrderStatus,
)
from apps.business.order_no import generate_order_no
from tests.p5_auth_helpers import make_logged_in_client

#: 行级编辑端点（**逐字**，⛔ 不从视图模块反向拼）。
ROW_DETAIL_URL = "/api/business/upstream-bindings/jidanpu/{pk}/"
#: 既有按用户 member PUT（兼容保留）。
MEMBER_PUT_URL = "/api/business/member/upstream-jidanpu-binding/"

#: 「绑定上家系统设置」页的节点门（逐字写死，⛔ 不从视图 import——E-0024）。
BINDING_NODE = "system.bind_upstream_system"

BASE_URL_A = "https://unlist-a.test.invalid"
BASE_URL_B = "https://unlist-b.test.invalid"
BASE_URL_C = "https://unlist-c.test.invalid"
BASE_URL_E = "https://unlist-e.test.invalid"
ACCOUNT_X = "kefu-up-unlist-x"
ACCOUNT_Y = "kefu-up-unlist-y"
PASSWORD = "pw-canary-unlist"

#: 台账成因句的字面副本（⛔ 不从 `jidanpu_upstream` import——E-0024：被断言的东西
#: 与决定结果的东西是同一个符号时，断言恒真）。
EXPECTED_BATCH_REASON = "归属同步账号已停用（或换绑/删除），无人接管的孤儿镜像卡批量下架"

_LISTED = JidanpuUpstreamHallOrder.Status.LISTED
_CLAIMING = JidanpuUpstreamHallOrder.Status.CLAIMING
_GONE = JidanpuUpstreamHallOrder.Status.GONE


def _user(name: str) -> User:
    return User.objects.create_user(username=name, password=f"pw-{name}")


def _detect(ok=True):
    """把 `jidanpu_upstream.detect_binding` 换成固定结论的桩（零真实网络）。"""
    return mock.patch.object(
        jidanpu_upstream,
        "detect_binding",
        return_value=jidanpu_upstream.DetectionReport(ok=ok, problems=()),
    )


class UnlistFixture(TenantTestCase):
    """共用夹具：地址 A 上绑定 X（停用/换绑的主角）+ 候选镜像行工厂。"""

    def setUp(self):
        self.now = timezone.now()
        self.game = Game.objects.create(name="测试游戏甲")
        self.owner = self._make_owner()
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.owner, jidanpu_base_url=BASE_URL_A, account=ACCOUNT_X,
            password=PASSWORD, enabled=True,
        )

    def _make_owner(self) -> User:
        return _user("unlist-owner")

    # ── 工厂 ────────────────────────────────────────────────────────────────

    def _mirror(self, order_id, *, owner_binding, base_url=BASE_URL_A, status=_LISTED):
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=base_url,
            upstream_order_id=order_id,
            status=status,
            game=self.game,
            owner_binding=owner_binding,
            first_seen_at=self.now,
        )

    def _successor(self, *, base_url=BASE_URL_A, account=ACCOUNT_Y, enabled=True):
        """同地址的另一份绑定（接管语义的另一半）。"""
        return JidanpuUpstreamBinding.objects.create(
            user=_user(f"unlist-{account}"), jidanpu_base_url=base_url,
            account=account, password=PASSWORD, enabled=enabled,
        )

    # ── 断言工具 ────────────────────────────────────────────────────────────

    def _gone_events(self):
        return JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone")

    def _assert_unlisted(self, mirror, *, binding_pk):
        """「拆 = 条件更新 listed→gone + withdrawn 灰卡 + hall_gone 台账」的完整终态。"""
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, _GONE)
        self.assertIsNotNone(mirror.gone_at)
        snaps = HallExitSnapshot.objects.filter(mirror=mirror)
        self.assertEqual(snaps.count(), 1)
        self.assertEqual(snaps.first().exit_kind, HallExitSnapshot.ExitKind.WITHDRAWN)
        events = self._gone_events().filter(
            target_ref=f"{mirror.upstream_base_url}/{mirror.upstream_order_id}"
        )
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().reason, EXPECTED_BATCH_REASON)
        self.assertEqual(events.first().payload.get("upstream_binding_id"), binding_pk)

    def _assert_untouched(self, mirror):
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, _LISTED)
        self.assertIsNone(mirror.gone_at)
        self.assertEqual(HallExitSnapshot.objects.filter(mirror=mirror).count(), 0)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                kind="upstream.hall_gone",
                target_ref=f"{mirror.upstream_base_url}/{mirror.upstream_order_id}",
            ).count(),
            0,
        )


class RowLevelPutDisableTests(UnlistFixture):
    """生效点①：行级 PUT 把 `enabled` 翻假的那次保存（`upstream_binding_views.py`）。"""

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="unlist-row-op", groups=("编列甲",), nodes=(BINDING_NODE,),
        )

    def _put(self, pk, **overrides):
        body = {
            "jidanpu_base_url": BASE_URL_A,
            "account": ACCOUNT_X,
            "password": PASSWORD,
            "hall_price_percent": 100,
            "enabled": False,
        }
        body.update(overrides)
        return self.client.put(
            ROW_DETAIL_URL.format(pk=pk), json.dumps(body), content_type="application/json"
        )

    def test_disable_without_a_same_address_successor_unlists_orphans(self):
        r1 = self._mirror("20261105000000000011", owner_binding=self.binding)
        r2 = self._mirror("20261105000000000012", owner_binding=self.binding)
        res = self._put(self.binding.pk)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.binding.refresh_from_db()
        self.assertFalse(self.binding.enabled)
        self._assert_unlisted(r1, binding_pk=self.binding.pk)
        self._assert_unlisted(r2, binding_pk=self.binding.pk)
        self.assertEqual(self._gone_events().count(), 2)

    def test_disabling_a_full_hall_batches_cleanup_and_preserves_withdrawals(self):
        card_count = 611
        cards = JidanpuUpstreamHallOrder.objects.bulk_create([
            JidanpuUpstreamHallOrder(
                upstream_base_url=BASE_URL_A, upstream_order_id=f"up-{index}",
                status=_LISTED, game=self.game, owner_binding=self.binding, first_seen_at=self.now,
            )
            for index in range(card_count)
        ])
        shadows = Order.objects.bulk_create([
            Order(
                order_no=f"20261105{index:012d}", game=self.game, status=OrderStatus.WAITING_GRAB,
                hall_price=Decimal("66.00"), total_price=Decimal("88.00"),
                source_order_no=card.upstream_order_id, upstream_hall_card=card,
            )
            for index, card in enumerate(cards)
        ])
        downstream, second, queued = [
            JidanpuMemberBinding.objects.create(
                user=_user(f"full-hall-down-{index}"),
                jidanpu_base_url=f"https://full-hall-down-{index}.test.invalid",
                account=f"full-hall-down-{index}", password=PASSWORD, enabled=True,
            )
            for index in range(3)
        ]
        JidanpuMemberOrderLink.objects.bulk_create([
            JidanpuMemberOrderLink(
                binding=binding, order=shadow, jidanpu_order_id=f"down-{index}", in_hall=True,
            )
            for index, shadow in enumerate(shadows)
            for binding in (downstream, second)
            if index > 0 or binding == downstream
        ])
        JidanpuSyncEvent.objects.bulk_create([
            JidanpuSyncEvent(
                direction="out", kind="member.launch", status="pending",
                target_type="order", target_ref=shadow.order_no,
                payload={"member_binding_id": queued.pk},
            )
            for shadow in shadows[:-1]
        ])
        existing = JidanpuSyncEvent.objects.create(
            direction="out", kind="member.withdraw", status="failed", attempts=100,
            target_type="order", target_ref=shadows[0].order_no,
            reason="远端暂不可用", processed_at=self.now,
            payload={
                "member_binding_id": downstream.pk, "progress": "preserved",
                "rearmed_at": (self.now - timedelta(minutes=1)).isoformat(),
            },
            lease_owner="retained-lease", lease_expires_at=self.now + timedelta(minutes=1),
        )
        newer_row = JidanpuSyncEvent.objects.create(
            direction="out", kind="member.withdraw", status="done",
            target_type="order", target_ref=shadows[0].order_no,
            payload={"member_binding_id": downstream.pk},
        )
        JidanpuSyncEvent.objects.filter(pk=existing.pk).update(created_at=self.now - timedelta(minutes=3))
        JidanpuSyncEvent.objects.filter(pk=newer_row.pk).update(created_at=self.now - timedelta(minutes=2))
        narrow = JidanpuSyncEvent.objects.create(
            direction="out", kind="member.withdraw", status="pending",
            target_type="order", target_ref=shadows[1].order_no,
            payload={"member_binding_id": downstream.pk, "hall_only": True},
        )

        with CaptureQueriesContext(connection) as queries:
            response = self._put(self.binding.pk)

        self.assertEqual(response.json()["code"], 0, response.content)
        self.assertLessEqual(len(queries), 120, "保存大厅规模的绑定时，数据库往返不能按订单逐张增长")
        self.binding.refresh_from_db()
        self.assertFalse(self.binding.enabled)
        saved_cards = JidanpuUpstreamHallOrder.objects.filter(owner_binding=self.binding)
        self.assertEqual(saved_cards.filter(status=_GONE).count(), card_count)
        self.assertEqual(HallExitSnapshot.objects.filter(mirror__in=saved_cards).count(), card_count)
        self.assertEqual(self._gone_events().count(), card_count)
        self.assertEqual(Order.objects.filter(
            upstream_hall_card__in=saved_cards, status=OrderStatus.REFUNDED,
        ).count(), card_count)
        self.assertEqual(JidanpuSyncEvent.objects.filter(
            kind="upstream.hall_shadow_refunded",
        ).count(), card_count)
        withdrawals = JidanpuSyncEvent.objects.filter(
            direction="out", kind="member.withdraw", status="pending",
        ).exclude(pk=narrow.pk)
        destinations = [
            (shadow.order_no, binding.pk)
            for binding, eligible in (
                (downstream, shadows), (second, shadows[1:]), (queued, shadows[:-1]),
            )
            for shadow in eligible
        ]
        self.assertCountEqual(
            list(withdrawals.values_list("target_ref", "payload__member_binding_id")),
            destinations,
        )
        existing.refresh_from_db()
        self.assertEqual((existing.attempts, existing.reason, existing.processed_at), (0, "", None))
        self.assertEqual(existing.payload["progress"], "preserved")
        self.assertEqual(existing.lease_owner, "retained-lease")
        self.assertNotEqual(existing.payload["rearmed_at"], (self.now - timedelta(minutes=1)).isoformat())
        narrow.refresh_from_db()
        self.assertEqual(narrow.payload, {"member_binding_id": downstream.pk, "hall_only": True})

    def test_disable_keeps_cards_when_another_enabled_binding_covers_the_address(self):
        """⭐ 安全核心：同地址还有启用绑定 ⇒ 下一轮 sync_hall 会接管 ⇒ 不拆。"""
        successor = self._successor()
        r1 = self._mirror("20261105000000000021", owner_binding=self.binding)
        res = self._put(self.binding.pk)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.binding.refresh_from_db()
        self.assertFalse(self.binding.enabled, "绑定本身照常停用")
        self.assertTrue(
            JidanpuUpstreamBinding.objects.filter(pk=successor.pk).first().enabled
        )
        self._assert_untouched(r1)
        self.assertEqual(self._gone_events().count(), 0)

    def test_disable_only_touches_listed_rows(self):
        """并发条件更新竞争 ⇒ 不写第二笔：非 `listed` 的行一颗都不碰（照拆雷支）。"""
        taker = _user("unlist-taker")
        r_listed = self._mirror("20261105000000000031", owner_binding=self.binding)
        r_claiming = self._mirror(
            "20261105000000000032", owner_binding=self.binding, status=_CLAIMING
        )
        JidanpuUpstreamHallOrder.objects.filter(pk=r_claiming.pk).update(
            claiming_by=taker, claiming_at=self.now
        )
        gone_before = self.now - timedelta(hours=1)
        r_gone = self._mirror(
            "20261105000000000033", owner_binding=self.binding, status=_GONE
        )
        JidanpuUpstreamHallOrder.objects.filter(pk=r_gone.pk).update(gone_at=gone_before)
        res = self._put(self.binding.pk)
        self.assertEqual(res.json()["code"], 0, res.content)
        self._assert_unlisted(r_listed, binding_pk=self.binding.pk)
        r_claiming.refresh_from_db()
        self.assertEqual(r_claiming.status, _CLAIMING)
        self.assertEqual(r_claiming.claiming_by_id, taker.pk)
        self.assertIsNone(r_claiming.gone_at, "claiming 行没有 gone 时刻")
        r_gone.refresh_from_db()
        self.assertEqual(r_gone.status, _GONE)
        self.assertEqual(r_gone.gone_at, gone_before, "已 gone 的行不刷新 gone_at")
        self.assertEqual(HallExitSnapshot.objects.count(), 1, "claiming → gone 不写灰卡")
        self.assertEqual(self._gone_events().count(), 1, "一颗雷只写一笔台账")


class MemberPutDisableTests(UnlistFixture):
    """生效点②：按用户 member PUT 兼容路径停用（`jidanpu_upstream_views.py`，同判据）。"""

    def _make_owner(self) -> User:
        # member PUT 只作用「本人那一行」⇒ 登录用户必须就是绑定行的录入人：
        # 先建登录态，再把**同一个**用户拿去建绑定行（⛔ 不能各建一个同名用户）。
        self.client = make_logged_in_client(username="unlist-owner", groups=("编成甲",))
        return User.objects.get(username="unlist-owner")

    def _member_put(self, **overrides):
        body = {
            "jidanpu_base_url": BASE_URL_A,
            "account": ACCOUNT_X,
            "password": PASSWORD,
            "enabled": False,
        }
        body.update(overrides)
        return self.client.put(
            MEMBER_PUT_URL, json.dumps(body), content_type="application/json"
        )

    def test_member_put_disable_unlists_orphans_without_a_successor(self):
        r1 = self._mirror("20261105000000000041", owner_binding=self.binding)
        res = self._member_put()
        self.assertEqual(res.json()["code"], 0, res.content)
        self._assert_unlisted(r1, binding_pk=self.binding.pk)

    def test_member_put_disable_keeps_cards_under_a_same_address_successor(self):
        self._successor()
        r1 = self._mirror("20261105000000000042", owner_binding=self.binding)
        res = self._member_put()
        self.assertEqual(res.json()["code"], 0, res.content)
        self._assert_untouched(r1)
        self.assertEqual(self._gone_events().count(), 0)


class RebindTests(UnlistFixture):
    """生效点③：换绑（地址被真改写）⇒ 旧地址孤儿拆、同地址接管语义同样适用。"""

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="unlist-rebind-op", groups=("编换甲",), nodes=(BINDING_NODE,),
        )

    def _rebind(self, pk, new_url):
        body = {
            "jidanpu_base_url": new_url,
            "account": ACCOUNT_X,
            "password": PASSWORD,
            "hall_price_percent": 100,
            "enabled": True,
        }
        with _detect(ok=True):
            return self.client.put(
                ROW_DETAIL_URL.format(pk=pk), json.dumps(body), content_type="application/json"
            )

    def test_rebind_unlists_old_address_orphans_and_keeps_new_address_cards(self):
        r_old = self._mirror("20261105000000000051", owner_binding=self.binding)
        # 旧地址另一时期留下的行（X 曾在 B 拉过、又搬回 A，现在再搬回 B）。
        r_new = self._mirror(
            "20261105000000000052", owner_binding=self.binding, base_url=BASE_URL_B
        )
        res = self._rebind(self.binding.pk, BASE_URL_B)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.jidanpu_base_url, BASE_URL_B)
        self.assertTrue(self.binding.enabled, "换绑不等于停用")
        self._assert_unlisted(r_old, binding_pk=self.binding.pk)
        self._assert_untouched(r_new)
        self.assertEqual(self._gone_events().count(), 1, "新地址的活卡不拆")

    def test_rebind_keeps_old_address_cards_when_a_successor_covers_them(self):
        """⭐ 安全核心（换绑侧）：旧地址还有启用绑定 ⇒ 接管语义 ⇒ 不拆。"""
        self._successor()
        r_old = self._mirror("20261105000000000061", owner_binding=self.binding)
        res = self._rebind(self.binding.pk, BASE_URL_B)
        self.assertEqual(res.json()["code"], 0, res.content)
        self._assert_untouched(r_old)
        self.assertEqual(self._gone_events().count(), 0)


class DirectHelperTests(UnlistFixture):
    """辅助本体（不经端点）：条件更新 + 幂等（第二笔不写）。"""

    def test_a_second_call_writes_no_second_trace(self):
        r1 = self._mirror("20261105000000000071", owner_binding=self.binding)
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        self.assertEqual(jidanpu_upstream.unlist_orphan_hall_rows(self.binding), 1)
        self.assertEqual(jidanpu_upstream.unlist_orphan_hall_rows(self.binding), 0)
        self._assert_unlisted(r1, binding_pk=self.binding.pk)
        self.assertEqual(self._gone_events().count(), 1)
        self.assertEqual(HallExitSnapshot.objects.count(), 1)


class DeleteBindingTests(UnlistFixture):
    """⭐ 修复轮 M1：删号生效点——SET_NULL 留下的无主 `listed` 行批量下架（同一 atomic、删后跑）。"""

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="unlist-del-op", groups=("编删甲",), nodes=(BINDING_NODE,),
        )

    def _delete(self, pk):
        return self.client.delete(ROW_DETAIL_URL.format(pk=pk))

    def test_delete_unlists_ownerless_listed_rows_when_no_successor_covers_the_address(self):
        r1 = self._mirror("20261105000000000091", owner_binding=self.binding)
        # 另一地址的无主行不在删号生效点范围（候选按被删绑定的地址收窄；存量归命令管）。
        r_other = self._mirror(
            "20261105000000000092", owner_binding=None, base_url=BASE_URL_C
        )
        res = self._delete(self.binding.pk)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.assertFalse(
            JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).exists(), "绑定真删"
        )
        self._assert_unlisted(r1, binding_pk=None)
        self._assert_untouched(r_other)
        self.assertEqual(self._gone_events().count(), 1)

    def test_delete_keeps_rows_when_a_successor_covers_the_address(self):
        """⭐ 安全核心（删号侧）：同地址还有启用绑定 ⇒ 接管语义 ⇒ 只删绑定、不拆卡。"""
        self._successor()
        r1 = self._mirror("20261105000000000093", owner_binding=self.binding)
        res = self._delete(self.binding.pk)
        self.assertEqual(res.json()["code"], 0, res.content)
        r1.refresh_from_db()
        self.assertEqual(r1.status, _LISTED)
        self.assertIsNone(r1.owner_binding_id, "SET_NULL 照旧；重新归属在下一轮拉取")
        self.assertEqual(self._gone_events().count(), 0)


class BatchUnlistRefundWitnessTests(UnlistFixture):
    """⭐ 修复轮建议①：孤儿卡挂着 WAITING_GRAB 隐藏单被批量下架 ⇒ D-0325 全套跟随。"""

    def test_batch_unlist_refunds_a_waiting_shadow_and_queues_withdraw(self):
        r1 = self._mirror("20261105000000000094", owner_binding=self.binding)
        shadow = Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("66.00"),
            total_price=Decimal("88.00"), source_order_no=r1.upstream_order_id,
            upstream_hall_card=r1,
        )
        # 推送证据（`queue_withdraw` 的判据输入 = 链路行 ∪ launch）：链路行最省事，
        # 语义同「这张副本已经抛给下家」——没有它 withdraw 本来就该是 0（没推过没得撤）。
        downstream = JidanpuMemberBinding.objects.create(
            user=_user("unlist-down"), jidanpu_base_url="https://unlist-down.test.invalid",
            account="kefu-down-unlist", password=PASSWORD, enabled=True,
            throw_price_percent=90,
        )
        JidanpuMemberOrderLink.objects.create(
            binding=downstream, order=shadow,
            jidanpu_order_id="JDP-UNLIST-0000000000000001", in_hall=True,
        )
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        self.assertEqual(jidanpu_upstream.unlist_orphan_hall_rows(self.binding), 1)
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))
        # `upstream_hall_card` 不清（退掉的派生单仍属隐藏面；照 GoneRefundTests 同一判据）。
        self.assertIsNotNone(shadow.upstream_hall_card_id)
        # 引擎退款标记（S-03 / D-0331）：恰好一条，钉在这一次退款上（字面 kind，E-0024）。
        marks = JidanpuSyncEvent.objects.filter(kind="upstream.hall_shadow_refunded")
        self.assertEqual(marks.count(), 1)
        self.assertEqual(marks.first().payload.get("order_no"), shadow.order_no)
        # 下家副本撤单待办：恰好一条 `member.withdraw`（direction=out）。
        withdraws = JidanpuSyncEvent.objects.filter(
            kind="member.withdraw", target_ref=shadow.order_no, direction="out",
        )
        self.assertEqual(withdraws.count(), 1)


class UnlistOrphanHallRowsCommandTests(UnlistFixture):
    """一次性管理命令：默认 dry-run 零写入；--apply 拆真孤儿；重跑命中 0 行。"""

    def setUp(self):
        super().setUp()
        self.schema = connection.schema_name
        # 候选四类，各钉一行：
        #   r1/r2 = owner 已停用＋同地址无启用绑定 ⇒ 真孤儿；
        #   r3    = owner 已删（NULL）⇒ 「已删」档真孤儿；
        #   r4    = owner 还启用着 ⇒ 不是候选；
        #   r5    = owner 已停用、但地址 E 上另有启用绑定 ⇒ 接管保护，不拆。
        self.x_disabled = self.binding
        JidanpuUpstreamBinding.objects.filter(pk=self.x_disabled.pk).update(enabled=False)
        self.r1 = self._mirror("20261105000000000081", owner_binding=self.x_disabled)
        self.r2 = self._mirror("20261105000000000082", owner_binding=self.x_disabled)
        self.r3 = self._mirror(
            "20261105000000000083", owner_binding=None, base_url=BASE_URL_C
        )
        self.w = self._successor(base_url=BASE_URL_B, account="kefu-up-unlist-w")
        self.r4 = self._mirror(
            "20261105000000000084", owner_binding=self.w, base_url=BASE_URL_B
        )
        self.y_at_e = self._successor(base_url=BASE_URL_E, account="kefu-up-unlist-y")
        self.r5 = self._mirror(
            "20261105000000000085", owner_binding=self.x_disabled, base_url=BASE_URL_E
        )

    def _run(self, *args):
        out = StringIO()
        call_command("unlist_orphan_hall_rows", "--schema", self.schema, *args, stdout=out)
        return out.getvalue()

    def test_dry_run_prints_true_orphans_and_writes_nothing(self):
        out = self._run()
        for mirror in (self.r1, self.r2, self.r3):
            self.assertIn(mirror.upstream_order_id, out, "dry-run 要逐行打印清单")
        self.assertNotIn(self.r4.upstream_order_id, out, "owner 启用的行不是候选")
        self.assertNotIn(self.r5.upstream_order_id, out, "接管保护的行不是真孤儿")
        # 「合计」行的明确形状（⛔ 不 assertIn 裸数字——单号/主键字面里可能有它，恒真风险）。
        self.assertIn("合计 会下架（dry-run，未落写） 3 张", out)
        for mirror in (self.r1, self.r2, self.r3, self.r4, self.r5):
            mirror.refresh_from_db()
            self.assertEqual(mirror.status, _LISTED, "dry-run 一个字不落库")
        self.assertEqual(self._gone_events().count(), 0)
        self.assertEqual(HallExitSnapshot.objects.count(), 0)

    def test_apply_unlists_orphans_and_rerun_hits_zero(self):
        out = self._run("--apply")
        self.assertIn("合计 已下架 3 张", out)
        for mirror in (self.r1, self.r2, self.r3):
            self._assert_unlisted(mirror, binding_pk=mirror.owner_binding_id)
        self._assert_untouched(self.r4)
        self._assert_untouched(self.r5)
        self.assertEqual(self._gone_events().count(), 3)
        # 「已删」那一档的台账 payload 里没有绑定主键（诚实留痕）。
        orphan_event = self._gone_events().get(target_ref__contains=BASE_URL_C)
        self.assertIsNone(orphan_event.payload.get("upstream_binding_id"))
        # 幂等：重跑命中 0 行、不写第二笔（合计行明确形状）。
        out2 = self._run("--apply")
        self.assertIn("合计 已下架 0 张", out2)
        self.assertEqual(self._gone_events().count(), 3)
        self.assertEqual(HallExitSnapshot.objects.count(), 3)
