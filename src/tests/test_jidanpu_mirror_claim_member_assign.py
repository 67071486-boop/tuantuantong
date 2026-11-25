"""E-0283 / A 案（2026-10-24）：**上家镜像接单 ⇒ 立即给接手人的 member 绑定排 `member.assign`**。

计划：`docs/plans/2026-10-24-member-assign-on-mirror-claim-and-pic-repush.md` §2 / §4-A。
修复前：镜像接单路径（`JidanpuUpstreamHallClaimView` → `claim_upstream_mirror` →
`promote_upstream_mirror`）**零个** `queue_claimed` 调用 ⇒ 单要等 180 秒对账的
`to_record` 补录才进对方系统（现场：10:01:29 接单 → 10:03:46 才录到 peiyi）。

本文件钉计划 §4-A 的六条：

| # | 用例族 | 判据 |
|---|---|---|
| A1 | 请求路径 + worker 重试路径 | 转正成功 ⇒ **恰好一条** `member.assign`（绑定=接手人那份） |
| A2 | 条件更新输家 | promote 拿不到（行已被转正）⇒ **不**重复排队 |
| A3 | 接手人无启用绑定 | 不排队、转正照常 |
| A4 | **同地址**（M2） | 消费走**认领支**（按 `effective_source_order_no` 回读命中、不重录） |
| A5 | E-0236 抑制回归 | `test_jidanpu_member_openapi_consume.py` 既有钉子一行不改、照旧绿 |
| A6 | 在飞去重 | 180 秒对账 `to_record` 侧**不再补排第二条** assign |

⚠️ 判据一律打**数据库行**（`JidanpuSyncEvent` 的 kind / target_ref / payload）与桩的调用集合
（⛔ 不打 logger）；夹具一律合成值（E-0077）；零真实网络（`claim_session` 打桩，E-0029）。
"""
from __future__ import annotations

from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu, jidanpu_member, jidanpu_upstream
from apps.business.models import (
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    Order,
    OrderStatus,
)
from tests.p5_auth_helpers import make_logged_in_client

#: ⚠️ 替身形状只此一处：直接引传图/接单那几张网里现成的（⛔ 不另造一个假 client）。
from tests.test_jidanpu_member_consume import CLIENT, _stub, _Upstream
from tests.test_jidanpu_upstream_hall import (
    _ClientCtx,
    _StubClient,
    BASE_TIME,
    CLAIM_PATH,
    BASE_URL as UPSTREAM_BASE_URL,
    REMOTE_ID,
)

#: 下家 member 绑定的合成地址（默认与上家**不同**地址）。
MEMBER_BASE_URL = "https://jidanpu-member-claim.test.invalid"
#: 镜像报文里的上家展示号（转正后成为我方单的 `source_order_no`——同地址认领支的回读键）。
SOURCE_NO = "5127371679174182702"

KIND_ASSIGN = "member.assign"


class _MirrorClaimFixture(TenantTestCase):
    """共用夹具：一款游戏 + 一份上家绑定 + 一个能接镜像单的打手 + 一个镜像行工厂。"""

    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="镜像接单推下家夹具游戏")
        self.owner_user = User.objects.create_user(
            username="kefu-up-mirror-assign", password="pw-canary",
        )
        self.upstream_binding = JidanpuUpstreamBinding.objects.create(
            user=self.owner_user, jidanpu_base_url=UPSTREAM_BASE_URL,
            account="kefu-up-mirror-assign", password="pw-canary", enabled=True,
        )
        self.taker_client = make_logged_in_client(
            username="mirror-claim-taker-a", groups=("代练员",), games=[self.game],
            nodes=("grab_hall.take_order",),
        )
        self.taker = User.objects.get(username="mirror-claim-taker-a")
        self.mirror = self._mirror()

    def _mirror(self, *, remote_id=REMOTE_ID, status=None, claiming_by=None):
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=UPSTREAM_BASE_URL, upstream_order_id=remote_id,
            owner_binding=self.upstream_binding,
            status=status or JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game,
            project_text="合成项目", remote_remark="合成备注",
            remote_role_name="合成角色", remote_rec_fee=Decimal("88.00"),
            claiming_by=claiming_by, claiming_at=BASE_TIME if claiming_by else None,
            upstream_payload={
                "orderId": remote_id, "orderIdView": SOURCE_NO,
                "orderContent": "合成需求", "orderRemark": "合成备注",
                "roleName": "合成角色", "gameAcc": "acc-synth", "zgameAcc": "sub-synth",
                "recFee": "88.00",
            },
            first_seen_at=BASE_TIME,
        )

    def _member_binding(self, *, enabled=True, base_url=MEMBER_BASE_URL):
        return JidanpuMemberBinding.objects.create(
            user=self.taker,
            jidanpu_base_url=base_url,
            account="acc-member-claim-a",
            password="pw-canary",
            source_name="团团通系统",
            enabled=enabled,
        )

    def _claim_via_endpoint(self, client=None):
        """请求路径整条（真 HTTP 端点 + `claim_session` 桩）。回 `(stub, response)`。"""
        stub = _StubClient()
        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(stub)):
            response = (client or self.taker_client).post(
                CLAIM_PATH.format(pk=self.mirror.pk)
            )
        return stub, response

    def _assign_rows(self, order):
        return JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_ASSIGN, target_ref=order.order_no,
        )


class MirrorClaimQueuesMemberAssignTests(_MirrorClaimFixture):
    """A1 / A2 / A3：三条主路 + 两条不排队的边。"""

    def test_the_request_path_queues_exactly_one_member_assign(self):
        """⭐ A1·请求路径：转正成功 ⇒ **恰好一条** `member.assign`，绑定 = 接手人那份。

        判别力：把 `promote_upstream_mirror` 里那句 `queue_claimed` 删掉 ⇒ 行数断言红
        （退回 E-0283 现场：单要等 180 秒对账才进对方系统）。
        """
        self._member_binding(enabled=True)

        stub, response = self._claim_via_endpoint()

        self.assertEqual(response.json()["code"], 0, response.content[:200])
        order = Order.objects.get()
        rows = self._assign_rows(order)
        self.assertEqual(rows.count(), 1, "member.assign 必须恰好一条")
        row = rows.get()
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.target_type, "order")
        binding_id = row.payload["member_binding_id"]
        self.assertIs(type(binding_id), int)
        self.assertEqual(
            binding_id,
            JidanpuMemberBinding.objects.get(user=self.taker).pk,
        )
        # 新单没有任何已推痕迹 ⇒ withdraw / unlist 那两半都是 no-op（副作用清点）。
        self.assertEqual(
            {r.kind for r in JidanpuSyncEvent.objects.filter(target_ref=order.order_no)},
            {KIND_ASSIGN},
        )

    def test_the_worker_retry_path_queues_exactly_one_member_assign(self):
        """⭐ A1·worker 重试路径：后台轮消费 `upstream.hall_claim` 转正 ⇒ 同样恰好一条。"""
        self._member_binding(enabled=True)
        JidanpuUpstreamHallOrder.objects.filter(pk=self.mirror.pk).update(
            status=JidanpuUpstreamHallOrder.Status.CLAIMING,
            claiming_by=self.taker, claiming_at=BASE_TIME,
        )
        event = jidanpu_upstream.queue_hall_claim(self.mirror, taker_id=self.taker.pk)
        self.assertIsNotNone(event)

        stub = _StubClient()
        outcome = jidanpu_upstream.consume_event(event, client=stub)

        self.assertEqual(outcome, "done")
        order = Order.objects.get()
        self.assertEqual(self._assign_rows(order).count(), 1)

    def test_the_conditional_update_loser_rolls_back_and_does_not_queue(self):
        """⭐ A2：条件更新输家（行在 `claiming` 但占住人**不是**他）⇒ promote 回 `None`、
        **整笔回滚**（订单一行不留）、一条 `member.assign` 都不排。

        判别力：把那句 `queue_claimed` 挪到条件更新**之前**（或挪出事务）⇒ 输家也会排
        一条 ⇒ 本条的行数断言红（下游多出一张指向「根本没建成的那张单」的事件）。
        """
        self._member_binding(enabled=True)
        other = User.objects.create_user(
            username="mirror-claim-taker-b", password="pw-canary",
        )
        # 行在 claiming、但占住人是**别人** ⇒ promote 内部的条件更新（还要对上
        # `claiming_by_id=user_id`）拿不到 ⇒ `set_rollback` + 回 `None`。
        JidanpuUpstreamHallOrder.objects.filter(pk=self.mirror.pk).update(
            status=JidanpuUpstreamHallOrder.Status.CLAIMING,
            claiming_by=other, claiming_at=BASE_TIME,
        )

        loser = jidanpu_upstream.promote_upstream_mirror(
            self.mirror, binding=self.upstream_binding,
            detail=self.mirror.upstream_payload, user_id=self.taker.pk, now=BASE_TIME,
        )

        self.assertIsNone(loser)
        self.assertEqual(Order.objects.count(), 0, "输家的建单必须整笔回滚")
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind=KIND_ASSIGN).count(), 0,
            "条件更新输家不得排队",
        )

    def test_a_taker_without_a_member_binding_claims_without_queueing(self):
        """⭐ A3：接手人**没有** member 绑定 ⇒ 不排队、转正照常（单照建、状态照转）。"""
        _stub_client, response = self._claim_via_endpoint()

        self.assertEqual(response.json()["code"], 0, response.content[:200])
        order = Order.objects.get()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind=KIND_ASSIGN).count(), 0,
            "没有启用的 member 绑定 ⇒ 一条都不排",
        )

    def test_a_disabled_member_binding_also_claims_without_queueing(self):
        """⭐ A3·停用档：绑定在但 `enabled=False` ⇒ 不排队（启用那一刻的补推另有对账兜底）。"""
        self._member_binding(enabled=False)

        _stub_client, response = self._claim_via_endpoint()

        self.assertEqual(response.json()["code"], 0, response.content[:200])
        order = Order.objects.get()
        self.assertEqual(self._assign_rows(order).count(), 0)


class SameAddressClaimConsumptionTests(_MirrorClaimFixture):
    """⭐ A4（M2 修订）：接手人的 member 绑定与上游**同一套鸡蛋铺** ⇒ 消费走**认领支**。

    计划口径：`_existing_remote_order` 按 `effective_source_order_no`（上家 `orderIdView`）
    回读；同地址平台上家原单的 `orderIdView` 恰是该值 ⇒ **认领不重录**（verdict「other」
    不碰对方那张单），与现行 180 秒对账路行为完全相同、非新增风险。本类按**实际走的支**
    钉台账结果（涉事生产绑定现停用 ⇒ 夹具自建，不依赖生产行）。
    """

    def test_the_assign_consumes_via_adoption_without_re_recording(self):
        binding = self._member_binding(base_url=UPSTREAM_BASE_URL)  # ⭐ 与上家同一地址
        _stub_client, response = self._claim_via_endpoint()
        self.assertEqual(response.json()["code"], 0)
        order = Order.objects.get()
        # 夹具自证（承重前提）：转正单的来源单号 = 上家 `orderIdView`（认领支的回读键）。
        self.assertEqual(order.source_order_no, SOURCE_NO)
        event = self._assign_rows(order).get()
        self.assertEqual(event.status, "pending")

        # 同地址平台上「那张上家原单」按来源单号读得到（orderIdView 逐字等于回读键）。
        up = _Upstream(
            lookup={
                SOURCE_NO: {
                    "orderId": REMOTE_ID, "orderIdView": SOURCE_NO,
                    "orderSta": "代练中", "isThrowOrder": "N",
                },
            },
            remote_exists=True,
        )
        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "done")
        # ⭐ 认领支：一次 `add_order` 都不打（重录 = 往同一家店里灌第二张真单）。
        self.assertNotIn("add_order", up.names())
        # 回读键 = 有效来源单号（上家 `orderIdView`）。
        self.assertEqual(dict(up.calls)["fetch_order_by_key1"]["key"], SOURCE_NO)
        # 链路落在认领到的那个远端单号上（上家原单的 `orderId`）。
        link = JidanpuMemberOrderLink.objects.get(binding=binding, order=order)
        self.assertEqual(link.jidanpu_order_id, REMOTE_ID)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")


class SameAddressReuseBranchClaimTests(_MirrorClaimFixture):
    """⭐ D-0342（2026-10-30）：同地址认领支 × **复用支转正**（卡派生过隐藏单）。

    转正改写前，复用支的来源号留在上家**内部号**上 ⇒ 同地址认领支的回读键（
    `effective_source_order_no`＝内部号）认不得上家原单（它的 `orderIdView`＝展示号）
    ⇒ 会往同一家店**重录第二张**。改准后复用支与新建支同值（展示号）⇒ 认领不重录。
    本类把 §A4 的钉在**复用支**上重打一遍。
    """

    def test_reuse_branch_promotion_rewrites_source_and_adopts_without_re_recording(self):
        from apps.business.order_no import generate_order_no

        binding = self._member_binding(base_url=UPSTREAM_BASE_URL)  # ⭐ 与上家同一地址
        # 卡已派生隐藏单（创建期口径：来源号 = 上家**内部号**）。
        shadow = Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("74.80"), total_price=Decimal("88.00"),
            source_order_no=REMOTE_ID, upstream_hall_card=self.mirror,
        )
        _stub_client, response = self._claim_via_endpoint()
        self.assertEqual(response.json()["code"], 0, response.content[:200])
        order = Order.objects.get()
        # 复用支：同一张单接着用；⭐ 来源号已改写为上家展示号（= 认领支的回读键）。
        self.assertEqual(order.pk, shadow.pk)
        self.assertEqual(order.source_order_no, SOURCE_NO)
        event = self._assign_rows(order).get()

        up = _Upstream(
            lookup={
                SOURCE_NO: {
                    "orderId": REMOTE_ID, "orderIdView": SOURCE_NO,
                    "orderSta": "代练中", "isThrowOrder": "N",
                },
            },
            remote_exists=True,
        )
        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "done")
        # ⭐ 认领不重录：一次 `add_order` 都不打。
        self.assertNotIn("add_order", up.names())
        self.assertEqual(dict(up.calls)["fetch_order_by_key1"]["key"], SOURCE_NO)
        link = JidanpuMemberOrderLink.objects.get(binding=binding, order=order)
        self.assertEqual(link.jidanpu_order_id, REMOTE_ID)


class ReconcileDoesNotDoubleQueueTests(_MirrorClaimFixture):
    """⭐ A6：镜像接单已排 assign（在飞）⇒ 180 秒对账的 `to_record` **不补第二条**。

    对账那一侧 `queue_member_event` **不带 `rearm`** + 在飞去重 ⇒ 命中同一条 pending 行。
    """

    def test_reconcile_to_record_dedupes_onto_the_pending_assign(self):
        self._member_binding(enabled=True)
        _stub_client, response = self._claim_via_endpoint()
        self.assertEqual(response.json()["code"], 0)
        order = Order.objects.get()
        binding = JidanpuMemberBinding.objects.get(user=self.taker)
        self.assertEqual(self._assign_rows(order).count(), 1)
        before = self._assign_rows(order).get()

        stats = jidanpu_member.reconcile_and_queue(binding, now=BASE_TIME)

        # 夹具自证（承重前提）：对账确实把这张单算成了 `to_record`（已指派给他、没链路）
        # ——它排过了、只是被在飞去重并进同一条，而不是「对账没看见这张单」。
        self.assertEqual(stats.assign, 1, stats)
        rows = self._assign_rows(order)
        self.assertEqual(rows.count(), 1, "对账不得补排第二条 assign")
        self.assertEqual(rows.get().pk, before.pk)
        self.assertEqual(rows.get().status, "pending")
