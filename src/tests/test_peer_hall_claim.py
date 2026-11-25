"""D-0303 团团通大厅镜像：**抢单确认制全链**（spec §4）。

覆盖（每条都用例钉住）：

① 下家 `claim_peer_mirror`：前置闸（NULL 价 / 停绑）、`listed → claiming` 条件更新、
   并发第二人「手慢了」、上行 `hall.claim` 事件的形状（`binding_role="upstream"`、
   `target_ref` = 上家单号）；
② 上家 inbound（`hall.claim` → 机器接手 → `order.assign` + `hall.claim_ok`；冲突回绝）；
③ 下家收 `order.assign` ⇒ 镜像行转正（`promoted_order` 回填）；收 `hall.claim_refused`
   ⇒ 回 `listed`；
④ 两处接单端点（内部 `/api/business/hall/peer/<pk>/claim/` 与打手 API
   `/api/taker/v1/hall/peer/<pk>/claim/`）：写侧游戏权限卡点 + 节点门 / Bearer 装配。

⚠️ 全链用例在**同一个租户 schema** 里摆出两侧夹具（两族绑定表并存；事件由测试手工
从一个侧递到另一个侧，⇔ 真实系统里由 worker 投递）。夹具值全部合成值。
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase

from apps.business.claim import ClaimError
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerHallMirror,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_hall import claim_peer_mirror
from apps.business.peer_inbound import apply_events, apply_upstream_events
from apps.business.peer_sync import (
    HALL_CLAIM_OK_KIND,
    HALL_CLAIM_REFUSED_KIND,
    HALL_THROW_KIND,
    hash_token,
    new_token,
)
from tests._peer_stream_fakes import fake_sync_claim_upstream
from tests.p5_auth_helpers import grant_games, grant_nodes, make_logged_in_client

UP_BASE = "https://peer-hall-up.test.invalid"


def hall_payload(no, price="50.00"):
    return {
        "order_no": no,
        "status": "WAITING_GRAB",
        "total_price": "100.00",
        "hall_price": price,
        "remark": "syn-备注",
        "game_name": "抢单链夹具游戏",
        "server_id": None,
        "project_id": None,
        "project_text": "syn-项目",
        "customer_phone": "",
        "customer_qq": "",
        "content": "syn-需求",
        "source_order_no": "",
    }


class ClaimChainFixture(TenantTestCase):
    """同 schema 双侧夹具：上家一张在厅单 + 下家一条镜像行。"""

    def setUp(self):
        self.game = Game.objects.create(name="抢单链夹具游戏")
        # ── 下家侧（我方为下家）──
        operator = get_user_model().objects.create_user(
            username="syn-claim-chain-op", password="pw-syn"
        )
        self.up_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家丁",
            peer_base_url=UP_BASE,
            peer_account="syn-up-chain",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-hall-local3.test.invalid",
            bound_by=operator,
            # ⭐ P1：接收侧开关默认关 ⇒ 正常链路夹具显式打开（关闸由专门用例钉）。
            hall_sync_enabled=True,
        )
        # ── 上家侧（我方为上家；下家在我这儿的账号要能被机器接手）──
        self.down_account = get_user_model().objects.create_user(
            username="syn-claim-chain-down-acct", password="pw-syn"
        )
        worker_group, _ = Group.objects.get_or_create(name="代练员")
        self.down_account.groups.add(worker_group)
        grant_games(self.down_account, self.game)
        grant_nodes(self.down_account, "grab_hall.take_order")
        self.down_binding = PeerDownstreamBinding.objects.create(
            account=self.down_account,
            peer_base_url="https://peer-hall-down4.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        # ── 上家的在厅单 ──
        self.upstream_order = Order.objects.create(
            game=self.game,
            order_no="20261025000000070001",
            status=OrderStatus.WAITING_GRAB,
            total_price=Decimal("100.00"),
            hall_price=Decimal("50.00"),
        )
        Performance.objects.create(
            order=self.upstream_order, emp_type=EmpType.MAIN,
            temp_amount=Decimal("10.00"),
        )
        # ── 下家的镜像行（上家 throw 过来的形状）──
        self.mirror = PeerHallMirror.objects.create(
            peer_upstream_binding=self.up_binding,
            peer_base_url=UP_BASE,
            peer_order_no=self.upstream_order.order_no,
            status=PeerHallMirror.Status.LISTED,
            game=self.game,
            payload=hall_payload(self.upstream_order.order_no),
            remote_hall_price=Decimal("50.00"),
            first_seen_at=self._now(),
        )
        # 下家的抢单人（有游戏权限即可；claim_peer_mirror 不需要登录态）。
        self.taker = get_user_model().objects.create_user(
            username="syn-claim-chain-taker", password="pw-syn"
        )
        from apps.accounts.models import StaffProfile

        StaffProfile.objects.create(user=self.taker).games.set([self.game])
        # ⭐ Z11：打手侧 Bearer 大厅 / 接单端点都判 `grab_hall.take_order`（与该账号
        #    在网页上能不能接单同一颗节点）⇒ 本夹具的「合法打手」必须持它，
        #    无节点那一档由 `TakerApiClaimViewTests.test_a_taker_without_the_node_...`
        #    自建一个**不持节点**的账号单独钉。
        #    ⚠️ 用「大厅节点」（⛔ 不是「代练员」）：进「代练员」会把这个夹具的人变成
        #    **个人打手**，优选闸（`> 200` 才拦）的语义会跟着变。
        taker_group, _ = Group.objects.get_or_create(name="大厅节点")
        self.taker.groups.add(taker_group)
        grant_nodes(self.taker, "grab_hall.take_order")

    @staticmethod
    def _now():
        from django.utils import timezone

        return timezone.now()

    def share_with_downstream(self):
        """上家**真的把这张单通过 `hall.throw` 投给**这个下家绑定过（Z20 的共享证据）。

        走真入队函数（`peer_dispatch.enqueue_hall_event`）再把那一行记 `done` ＋ 空 `reason`
        ——⇔ worker 真投递成功那一拍（`_mark_event_done` 的终态形状）。
        ⚠️ ⛔ 不手搓 `PeerSyncEvent.objects.create(...)`：手搓能把「事件形状」与「真投递」
        的差别遮掉，而 Z20 的判据就是**真投递成功**这个事实。
        """
        from apps.business import peer_dispatch
        from apps.business.peer_sync import HALL_THROW_KIND

        made = peer_dispatch.enqueue_hall_event(
            self.upstream_order, kind=HALL_THROW_KIND
        )
        self.assertTrue(made, "夹具自证：这条单至少要有一个下家绑定可投")
        PeerSyncEvent.objects.filter(pk__in=[event.pk for event in made]).update(
            status=PeerSyncEvent.Status.DONE,
            reason="",
            processed_at=self._now(),
            next_attempt_at=None,
        )

    # ── 两侧接力的手工「投递」──
    def deliver_claim_to_upstream(self):
        """把下家排出的 hall.claim 事件递到上家的接收内核（⇔ worker 那一拍）。"""
        event = PeerSyncEvent.objects.filter(
            binding_role="upstream", kind="hall.claim"
        ).latest("pk")
        return apply_upstream_events(
            [{
                "idempotency_key": event.idempotency_key,
                "kind": event.kind,
                "target_ref": event.target_ref,
                "occurred_at": (event.meta or {}).get("occurred_at"),
                "payload": event.payload,
            }],
            binding=self.down_binding,
        )

    def deliver_assign_to_downstream(self):
        """把上家排出的 order.assign 递到下家的接收内核（⇔ worker 那一拍）。"""
        event = PeerSyncEvent.objects.filter(
            binding_role="downstream", kind="order.assign"
        ).latest("pk")
        return apply_events(
            [{
                "idempotency_key": event.idempotency_key,
                "kind": event.kind,
                "target_ref": event.target_ref,
                "occurred_at": (event.meta or {}).get("occurred_at"),
                "payload": event.payload,
            }],
            binding=self.up_binding,
        )

    def deliver_status_update_to_downstream(self):
        """把上家排出的 order.update（状态漏斗对 IN_PROGRESS）递到下家（⇔ worker）。"""
        event = (
            PeerSyncEvent.objects.filter(
                binding_role="downstream", kind="order.update"
            ).latest("pk")
        )
        return apply_events(
            [{
                "idempotency_key": event.idempotency_key,
                "kind": event.kind,
                "target_ref": event.target_ref,
                "occurred_at": (event.meta or {}).get("occurred_at"),
                "payload": event.payload,
            }],
            binding=self.up_binding,
        )


class ClaimPeerMirrorTests(ClaimChainFixture):
    """第 ① 层：`claim_peer_mirror` 的闸与预占。"""

    def test_listed_row_moves_to_claiming_and_enqueues_upstream_claim(self):
        row = claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.assertEqual(row.status, PeerHallMirror.Status.CLAIMING)
        self.assertEqual(row.claiming_by_id, self.taker.pk)
        self.assertIsNotNone(row.claiming_at)
        event = PeerSyncEvent.objects.get()
        self.assertEqual(event.kind, "hall.claim")
        self.assertEqual(event.binding_role, "upstream")
        self.assertEqual(event.direction, "out")
        self.assertEqual(event.target_ref, self.upstream_order.order_no)
        self.assertIn("claim_by", event.payload)

    def test_null_price_row_is_refused_by_the_grab_gate(self):
        """无价行：`check_grab_limits` 的「NULL 接单价」那一支拒（与原生单同档）。"""
        PeerHallMirror.objects.filter(pk=self.mirror.pk).update(remote_hall_price=None)
        with self.assertRaises(ClaimError) as caught:
            claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.assertEqual(caught.exception.code, 1013)
        # ⛔ 被拒时一行都不写（闸在条件更新之前，E-0022 同族）。
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        self.assertEqual(PeerSyncEvent.objects.count(), 0)

    def test_second_claimer_loses(self):
        """并发第二人：条件更新抢不到 ⇒ 「已被接走」。"""
        first = get_user_model().objects.create_user(
            username="syn-claim-chain-first", password="pw-syn"
        )
        from apps.accounts.models import StaffProfile

        StaffProfile.objects.create(user=first).games.set([self.game])
        claim_peer_mirror(user_id=first.pk, mirror_id=self.mirror.pk)
        with self.assertRaises(ClaimError) as caught:
            claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        # ORDER_ALREADY_TAKEN = 1012（`apps/core/error_codes.py`；钉字面值）。
        self.assertEqual(caught.exception.code, 1012)

    def test_switched_off_binding_refuses_the_claim_with_a_human_message(self):
        """⭐ P1 第三处闸：开关关着 ⇒ 拒绝 + 人话「还没打开上家大厅同步」+ **零出站**。

        ⛔ 不是「已被接走」那种竞争失败：那张卡的归属没坏、也没人抢，是本租户还没打开接收。
        """
        from apps.business.peer_hall import HALL_SYNC_OFF_MESSAGE

        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(
            hall_sync_enabled=False
        )
        with self.assertRaises(ClaimError) as caught:
            claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        # `PLATFORM_NOT_CONFIGURED` = 1015（钉字面值，⛔ 不 import 枚举当期望）。
        self.assertEqual(caught.exception.code, 1015)
        self.assertEqual(str(caught.exception), HALL_SYNC_OFF_MESSAGE)
        self.assertEqual(str(caught.exception), "还没打开上家大厅同步")
        # 被拒时一行都不写 + 一条出站事件都不排（闸在条件更新 / 入队之前）。
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(self.mirror.claiming_by_id)
        self.assertEqual(PeerSyncEvent.objects.count(), 0)

    def test_the_grab_gate_sees_the_discounted_price(self):
        """⭐ P1：抢单闸判的是**展示价**（50 × 90% = 45），⛔ 不是上家价 50。

        判别力：把本人的接单价上限设成 **46** —— 按上家价 50 会被「接单价必须在…」拒，
        按展示价 45 才接得下来。⛔ 别把上限设成 50：那样两种价都放行，用例恒真。
        """
        from apps.accounts.models import StaffProfile

        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(
            hall_price_percent=90
        )
        StaffProfile.objects.filter(user=self.taker).update(
            grab_price_max=Decimal("46.00")
        )
        row = claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.assertEqual(row.status, PeerHallMirror.Status.CLAIMING)

    def test_a_disabled_binding_answers_already_taken_and_keeps_the_row(self):
        """⭐ 2026-10-29 业务方报障修法三：停用绑定 ⇒ `1012`「已被其他打手接取」+ **行不动**。

        修前：回 `1015`「还没配置好鸡蛋铺同步账号」（与鸡蛋铺线 `claim_upstream_mirror`
        同款报障文案，把人推给管理员）。修后与鸡蛋铺线同支同口径：对打手而言这张卡与
        「已下架」不可区分 ⇒ `1012`。⚠️ 与鸡蛋铺线的差异（刻意）：**不做**命中即下架
        ——peer 镜像行的 `gone` 由上家 `hall.claim_refused` 回包驱动（生命周期与鸡蛋铺
        拉取侧不同），本支生产零命中（见 `peer_hall` 该支注释）；行留着，绑定恢复启用后
        由 peer 同步自然收敛。
        """
        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(enabled=False)
        with self.assertRaises(ClaimError) as caught:
            claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        # ORDER_ALREADY_TAKEN = 1012（钉字面值，⛔ 不 import 枚举当期望）。
        self.assertEqual(caught.exception.code, 1012)
        self.assertEqual(str(caught.exception), "该笔订单已被其他打手接取")
        # 被拒时一行都不写、一条出站事件都不排（闸在条件更新 / 入队之前，E-0022 同族）。
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(self.mirror.claiming_by_id)
        self.assertEqual(PeerSyncEvent.objects.count(), 0)

    def test_a_soft_deleted_binding_answers_already_taken_too(self):
        """软删绑定（`deleted_at` 非空）与停用**同一支** ⇒ 同样 `1012` + 行不动。"""
        from django.utils import timezone as dj_timezone

        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(
            deleted_at=dj_timezone.now()
        )
        with self.assertRaises(ClaimError) as caught:
            claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.assertEqual(caught.exception.code, 1012)
        self.assertEqual(str(caught.exception), "该笔订单已被其他打手接取")
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        self.assertEqual(PeerSyncEvent.objects.count(), 0)


class FullChainTests(ClaimChainFixture):
    """第 ②③ 层：throw → claim → 上家指派 → assign 回推 → 转正；冲突 ⇒ refused 回 listed。"""

    def test_happy_path_claim_to_promoted(self):
        """全链正对照：预占 → 上家接手并派给绑定账号 → 正式订单回来 → 镜像行转正。"""
        # ⭐ Z20：这张单确实通过 hall.throw 投给过这个下家绑定（没有它 claim 会被回绝）。
        self.share_with_downstream()
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.CLAIMING)

        # 上家那一拍：claim 进来、机器接手、order.assign + claim_ok 出去。
        outcome = self.deliver_claim_to_upstream()
        self.assertEqual(outcome.applied, 1)
        self.upstream_order.refresh_from_db()
        self.assertEqual(self.upstream_order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(
                order=self.upstream_order, emp_type=EmpType.MAIN
            ).user_id,
            self.down_account.pk,
        )
        out_kinds = set(
            PeerSyncEvent.objects.filter(
                # ⚠️ 必须带 `binding_role`：两族绑定表 pk 各自从 1 起，只按 binding_id
                # 会把下家自己排出的上行 hall.claim 也数进来。
                binding_role="downstream", binding_id=str(self.down_binding.pk)
            ).values_list("kind", flat=True)
        )
        # 既有 order.assign 推送 + claim 应答都在（机器接手的同一事务里还会排
        # `order.update`（状态漏斗对 IN_PROGRESS）与 `hall.take`（出厅广播，
        # 下家那条 claiming 行不吃它）——那是既有漏斗的正确行为，这里钉的是
        # 「本线自己的两件必须都在」）。
        self.assertIn("order.assign", out_kinds)
        self.assertIn(HALL_CLAIM_OK_KIND, out_kinds)
        self.assertIn("hall.take", out_kinds)

        # 下家这一拍：order.assign 进来、正式订单落库、镜像行转正。
        outcome = self.deliver_assign_to_downstream()
        self.assertEqual(outcome.applied, 1)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        order = self.mirror.promoted_order
        self.assertIsNotNone(order)
        self.assertEqual(order.source_order_no, self.upstream_order.order_no)
        # ⭐ H1 放大器：转正单**挂抢单打手**（照鸡蛋铺 promote 的既有语义）——
        #    MAIN 绩效行归 claiming_by，打手在「我的单」里看得到。
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, self.taker.pk)
        self.assertIsNotNone(perf.claimed_at)
        # ⛔ 双订单竞态（spec 测试底线⑥）：正式订单只此一张。
        self.assertEqual(Order.objects.filter(source_order_no=order.source_order_no).count(), 1)

    def test_the_take_broadcast_by_claim_acceptance_has_no_leave_reason(self):
        """⭐ 分流钉子（判定表 ②）：抢单**受理**发出的 `hall.take` 是裸的（无
        `leave_reason`）——接收侧的收单支只认「指派」那一种原因，受理路不得误触发收单。"""
        self.share_with_downstream()
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        outcome = self.deliver_claim_to_upstream()
        self.assertEqual(outcome.applied, 1)
        take_rows = PeerSyncEvent.objects.filter(
            binding_role="downstream", binding_id=str(self.down_binding.pk),
            kind="hall.take",
        )
        self.assertEqual(take_rows.count(), 1)
        self.assertNotIn("leave_reason", take_rows.get().meta or {})

    def test_promoted_order_takes_the_discounted_price(self):
        """⭐ P1 / Z18：90% ⇒ 转正单的发单价 = 50 × 90% = **45.00**（与大厅里看到的价一致）。

        ⭐ Z18：这里钉的是「**落单价 = 展示价**」这条等式本身——
        右边用**同一个** `peer_hall.display_price` 现算（⛔ 不是再写一个 45.00：
        两份手写字面量会一起被改坏，而等式两边必须来自同一份算法）。
        """
        from apps.business import peer_hall

        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(
            hall_price_percent=90
        )
        self.share_with_downstream()
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.deliver_claim_to_upstream()
        self.deliver_assign_to_downstream()
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        # 字面值那一半（钉住 90% 的实际读数）。
        self.assertEqual(self.mirror.promoted_order.hall_price, Decimal("45.00"))
        # ⭐ 等式那一半：落单价 === 大厅展示价（同一个 `display_price`）。
        self.assertEqual(
            self.mirror.promoted_order.hall_price, peer_hall.display_price(self.mirror)
        )

    def test_promoted_taker_counts_as_active_work_for_the_wallet(self):
        """⭐ H1：转正（挂打手 + 落「代练中」）后，解冻闸看得到这个打手占着活。"""
        from apps.accounts.wallet import _has_active_work

        self.share_with_downstream()
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.deliver_claim_to_upstream()
        self.deliver_assign_to_downstream()
        # 转正那一刻就落「代练中」（P4 的抢单例外，见 apply_events 建单支/更新支）⇒ 占活成立。
        self.assertIn("进行中的订单", _has_active_work(self.taker))
        # 状态漏斗的 order.update（IN_PROGRESS）随后到，占活不丢。
        self.deliver_status_update_to_downstream()
        self.assertIn("进行中的订单", _has_active_work(self.taker))

    def test_upstream_refusal_takes_the_card_off_the_hall(self):
        """⭐ 业务方 2026-10-25：上家单已被别人接走（**订单已不存在于大厅**）⇒ 卡**彻底下架**。

        拒绝原因（`hall.claim_refused` 载荷里的 `reason`）是「不在大厅」这一类 ⇒
        `gone`（⛔ 不再回 `listed`：那张卡回到大厅也永远抢不到）。
        """
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        # 上家那张单先被本租户打手接走（claim 事件还没递到）。
        perf = Performance.objects.get(
            order=self.upstream_order, emp_type=EmpType.MAIN
        )
        perf.user = self.down_account  # 随便一个非空归属人 ⇒ 不在大厅
        perf.save(update_fields=["user", "updated_at"])
        self.upstream_order.status = OrderStatus.IN_PROGRESS
        self.upstream_order.save(update_fields=["status", "updated_at"])

        outcome = self.deliver_claim_to_upstream()
        self.assertEqual(outcome.applied, 1)
        # 上家只回绝、不动单。⚠️ 过滤带 `binding_role`（理由同上）。
        self.assertEqual(
            set(
                PeerSyncEvent.objects.filter(
                    binding_role="downstream",
                    binding_id=str(self.down_binding.pk),
                ).values_list("kind", flat=True)
            ),
            {HALL_CLAIM_REFUSED_KIND},
        )
        self.upstream_order.refresh_from_db()
        self.assertEqual(
            Performance.objects.get(
                order=self.upstream_order, emp_type=EmpType.MAIN
            ).user_id,
            self.down_account.pk,
        )

        # 下家这一拍：refused 进来、claiming 收口成 gone + 行上留原因。
        refused = PeerSyncEvent.objects.get(kind=HALL_CLAIM_REFUSED_KIND)
        self.assertIn("上家拒绝", (refused.payload or {}).get("reason", ""))
        outcome = apply_events(
            [{
                "idempotency_key": refused.idempotency_key,
                "kind": refused.kind,
                "target_ref": refused.target_ref,
                "occurred_at": (refused.meta or {}).get("occurred_at"),
                "payload": refused.payload,
            }],
            binding=self.up_binding,
        )
        self.assertEqual(outcome.applied, 1)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.GONE)
        self.assertIsNotNone(self.mirror.gone_at)
        self.assertEqual(self.mirror.claiming_by_id, None)
        self.assertIn("上家拒绝", self.mirror.last_error)
        # ⛔ 没有正式订单产生。
        self.assertFalse(Order.objects.exclude(pk=self.upstream_order.pk).exists())


class PromotedMemberAssignTests(ClaimChainFixture):
    """⭐ 2026-10-26 生产缺口：抢单转正单必须录到**打手自己**绑定的下家鸡蛋铺。

    真机现场（某租户）：打手在大厅接了上家推来的卡、单在本租户升格成正式单也挂上了打手，
    但 `JidanpuSyncEvent` 里**一条 `member.*` 都没有** ⇒ 打手自己的下家鸡蛋铺永远收不到
    这张单。根因：转正走 `_promote_claiming_mirror`，它只写本地单，不排 `member.assign`
    （后台指派走 `order_writes.assign_order`，那里才排）。

    本类钉两件事：
    ① 本租户打手那一档（`claiming_by` 非空）⇒ 排**一条** `member.assign`，绑定 id 正确；
    ② 同一张卡重复投递 ⇒ 仍然只有一条（转正条件更新命中 0 行，⛔ 不重复排）。
    """

    def setUp(self):
        super().setUp()
        self.member_binding = JidanpuMemberBinding.objects.create(
            user=self.taker,
            jidanpu_base_url="https://member-hall-claim.test.invalid",
            account="syn-member-hall-claim",
            password="pw-syn",
            enabled=True,
        )

    def _promote(self):
        """跑到「镜像行已转正」为止；回上家那条 `order.assign`（重复投递要用同一份载荷）。"""
        self.share_with_downstream()
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.deliver_claim_to_upstream()
        self.deliver_assign_to_downstream()
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        self.order = self.mirror.promoted_order
        return PeerSyncEvent.objects.filter(
            binding_role="downstream",
            binding_id=str(self.down_binding.pk),
            kind="order.assign",
        ).latest("pk")

    def _deliver(self, event, *, occurred_at):
        return apply_events(
            [{
                "idempotency_key": event.idempotency_key,
                "kind": event.kind,
                "target_ref": event.target_ref,
                "occurred_at": occurred_at,
                "payload": event.payload,
            }],
            binding=self.up_binding,
        )

    def _member_assigns(self):
        return JidanpuSyncEvent.objects.filter(direction="out", kind="member.assign")

    def test_local_taker_promotion_queues_member_assign_for_his_own_binding(self):
        self._promote()
        events = self._member_assigns()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.target_ref, self.order.order_no)
        # ⚠️ 载荷里的绑定 id 必须是**打手自己**那一份（⛔ 不是别人的、也⛔ 不是字符串）。
        self.assertEqual(event.payload["member_binding_id"], self.member_binding.pk)

    def test_repeated_delivery_of_the_same_card_stays_a_single_member_assign(self):
        event = self._promote()
        # ⚠️ 先把那一条记成 `done`（⇔ worker 已经真的录到打手鸡蛋铺了）：`queue_member_event`
        #    的去重只挡**在飞**（`pending` / `failed`）的行 ⇒ 留下 `pending` 的话，
        #    「重复排了一条」会被它顺手吃掉、这条用例就抓不住错实现。记成 `done` 之后，
        #    唯一能挡住第二条的就是「转正条件更新命中 0 行」那道闸 —— 才是本用例要钉的东西。
        self.assertEqual(len(self._member_assigns()), 1)
        self._member_assigns().update(status=JidanpuSyncEvent.Status.DONE)
        # 同一张卡**再来一次**、且时刻更晚（否则先被接收侧 `stale` 闸挡住、走不到转正那一步）
        # ⇒ 行早已 `promoted` ⇒ ⛔ 不许排第二条。
        outcome = self._deliver(
            event, occurred_at=(self._now() + timedelta(seconds=5)).isoformat()
        )
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(len(self._member_assigns()), 1)


class PromotedTakerWithoutMemberBindingTests(ClaimChainFixture):
    """⭐ 打手**没有** member 绑定 ⇒ 不排、也不报错（照 `queue_assign` 既有口径：回 0）。"""

    def test_promotion_without_member_binding_queues_nothing(self):
        self.share_with_downstream()
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.deliver_claim_to_upstream()
        outcome = self.deliver_assign_to_downstream()
        self.assertEqual(outcome.applied, 1)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        # 一条本线事件都不排（也没有抛异常 —— 上面那行 applied 就是「转正正常完成」）。
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind__startswith="member."
            ).exists()
        )


class UpstreamClaimSourceTests(ClaimChainFixture):
    """⭐ Z20：`hall.claim` 的**来源校验**。

    判据：这张单必须**曾经真的通过 `hall.throw` 投递到**来要的这条下家绑定
    （`direction="out"` ∧ `kind="hall.throw"` ∧ `binding_role="downstream"` ∧
    `binding_id` ∧ `target_ref` ∧ **`status="done"`** ∧ **`reason=""`**）。
    没有 ⇒ 逐条回 `hall.claim_refused`（⛔ 不整批跳过：那会让对端以为投递成功）。
    ⭐ **两条 claim 支同一个判据**：原生支（`_apply_upstream_hall_claim`）与
    多跳镜像支（`_forward_mirror_claim`）——后者没共享时⛔ 不占卡、⛔ 不向上游转发。

    ⚠️ **「有这一行」不算共享**（本类第二条钉的就是这个改准）：投递失败的 `failed` 行、
    以及「done 但被作废」的非空 `reason` 行都留着行，但都不是「对端在大厅里看见过」。
    """

    def _claim(self):
        """下家先点接单（排出上行 `hall.claim`），再把它递到上家的接收内核。"""
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        return self.deliver_claim_to_upstream()

    def _refused_rows(self):
        return PeerSyncEvent.objects.filter(
            binding_role="downstream",
            binding_id=str(self.down_binding.pk),
            kind=HALL_CLAIM_REFUSED_KIND,
            target_ref=self.upstream_order.order_no,
        )

    def _assert_order_untouched(self):
        self.upstream_order.refresh_from_db()
        self.assertEqual(self.upstream_order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(
            Performance.objects.get(
                order=self.upstream_order, emp_type=EmpType.MAIN
            ).user_id
        )

    def test_a_claim_without_a_delivered_throw_is_refused(self):
        """⭐ 从没 throw 过 ⇒ 回绝 + 人话，⚠️ 逐条拒绝（applied=1、skipped=0）。"""
        outcome = self._claim()
        # ⛔ 不是整批跳过（那会让发送侧把这次请求记成「投递成功但被忽略」）。
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped, 0, outcome)
        refused = self._refused_rows()
        self.assertEqual(refused.count(), 1)
        # 本地留痕的人话（协议载荷仍为空对象，人话只落在 `reason` 列上）。
        self.assertIn("从未通过大厅投递", refused.get().reason)
        self._assert_order_untouched()

    def test_a_failed_or_voided_throw_does_not_count_as_shared(self):
        """⭐ 改准点：`failed` 行与「done 但被作废（非空 reason）」行都**不算**共享。"""
        base = {
            "binding_role": "downstream",
            "binding_id": str(self.down_binding.pk),
            "direction": "out",
            "kind": "hall.throw",
            "target_type": "order",
            "target_ref": self.upstream_order.order_no,
            "payload": {},
            "meta": {"occurred_at": self._now().isoformat()},
        }
        # ① 投递失败（对端超时）——行留着，reason 非空。
        PeerSyncEvent.objects.create(
            status=PeerSyncEvent.Status.FAILED,
            reason="connection refused",
            idempotency_key="syn-throw-failed",
            **base,
        )
        # ② `hall.take` 把未投的 throw 作废：`done` ＋ 人话 reason（M1 的形状）。
        PeerSyncEvent.objects.create(
            status=PeerSyncEvent.Status.DONE,
            reason="该单已离开大厅，这条未投递的上厅事件作废（以 hall.take 为准）",
            processed_at=self._now(),
            idempotency_key="syn-throw-voided",
            **base,
        )
        outcome = self._claim()
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped, 0, outcome)
        self.assertEqual(self._refused_rows().count(), 1)
        self._assert_order_untouched()

    def test_a_done_clean_throw_allows_the_claim(self):
        """⭐ 正对照：真的投递成功（done ＋ 空 reason）⇒ 放行、机器接手。"""
        self.share_with_downstream()
        outcome = self._claim()
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(self._refused_rows().count(), 0)
        self.upstream_order.refresh_from_db()
        self.assertEqual(self.upstream_order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(
                order=self.upstream_order, emp_type=EmpType.MAIN
            ).user_id,
            self.down_account.pk,
        )

    # ── ⭐ 多跳（镜像）那一支的同一道来源校验 ──────────────────────────
    def _unshared_mirror_card(self, card_no):
        """一张 `listed` 的镜像卡——但从未通过 `hall.throw` 推给这条下家绑定。

        ⚠️ 卡的号**必须与夹具那张不同**：同号时原生单优先（`_apply_upstream_hall_claim`
        的判据顺序），根本走不到镜像支，用例会假绿。
        """
        return PeerHallMirror.objects.create(
            peer_upstream_binding=self.up_binding,
            peer_base_url=UP_BASE,
            peer_order_no=card_no,
            status=PeerHallMirror.Status.LISTED,
            game=self.game,
            payload=hall_payload(card_no),
            remote_hall_price=Decimal("50.00"),
            first_seen_at=self._now(),
        )

    def _apply_mirror_claim(self, card_no):
        return apply_upstream_events(
            [{
                "idempotency_key": f"syn-mirror-claim-{card_no}",
                "kind": "hall.claim",
                "target_ref": card_no,
                "payload": {"claim_by": "syn-下家打手"},
            }],
            binding=self.down_binding,
        )

    def _share_mirror_card(self, card):
        """真的把这张镜像卡通过 `hall.throw` 投给这个下家绑定（多跳的正对照底座）。"""
        from apps.business import peer_dispatch

        made = peer_dispatch.enqueue_hall_mirror_event(card, kind=HALL_THROW_KIND)
        self.assertTrue(
            any(event.binding_id == str(self.down_binding.pk) for event in made),
            "夹具自证：这张卡没排给要测的那个下家绑定",
        )
        PeerSyncEvent.objects.filter(pk__in=[event.pk for event in made]).update(
            status=PeerSyncEvent.Status.DONE,
            reason="",
            processed_at=self._now(),
            next_attempt_at=None,
        )

    def test_a_mirror_card_claim_without_a_delivered_throw_is_refused(self):
        """⭐ 多跳中间层：`listed` 但没推给它 ⇒ refused、**零转发**、行零改动。

        判别力：把 `_forward_mirror_claim` 的那道判据删掉 ⇒ 行变 `claiming`、上游多一条
        `hall.claim`（下游凭一个猜到的卡号就能把卡要走）⇒ 本用例三处都会红。
        """
        card = self._unshared_mirror_card("SYN-CHAIN-UNSHARED-CARD-1")
        outcome = self._apply_mirror_claim(card.peer_order_no)
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped, 0, outcome)
        refused = PeerSyncEvent.objects.get(
            binding_role="downstream",
            binding_id=str(self.down_binding.pk),
            kind=HALL_CLAIM_REFUSED_KIND,
            target_ref=card.peer_order_no,
        )
        self.assertIn("从未通过大厅投递", refused.reason)
        card.refresh_from_db()
        self.assertEqual(card.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(card.claim_downstream_id)
        self.assertEqual(card.claim_id, "")
        self.assertIsNone(card.claiming_at)
        # ⛔ 零转发：这一家从没拿到过卡，不许替它往上游要。
        self.assertFalse(
            PeerSyncEvent.objects.filter(
                binding_role="upstream", kind="hall.claim"
            ).exists()
        )

    def test_a_shared_mirror_card_claim_is_forwarded(self):
        """⭐ 正对照：真的推过这张卡 ⇒ 占住中间层 + 向上游转发（证明判据不是恒拒）。"""
        card = self._unshared_mirror_card("SYN-CHAIN-SHARED-CARD-1")
        self._share_mirror_card(card)
        outcome = self._apply_mirror_claim(card.peer_order_no)
        self.assertEqual(outcome.applied, 1, outcome)
        card.refresh_from_db()
        self.assertEqual(card.status, PeerHallMirror.Status.CLAIMING)
        self.assertEqual(card.claim_downstream_id, self.down_binding.pk)
        self.assertNotEqual(card.claim_id, "")
        self.assertIsNotNone(card.claiming_at)
        forwarded = PeerSyncEvent.objects.filter(
            binding_role="upstream",
            kind="hall.claim",
            target_ref=card.peer_order_no,
        )
        self.assertEqual(forwarded.count(), 1)


class UpstreamClaimIntegrityTests(ClaimChainFixture):
    """⭐ Z8：机器接手撞 `IntegrityError` ⇒ 回绝，⛔ 不 500。"""

    def test_an_integrity_error_is_refused_with_a_human_note(self):
        import django.db

        self.share_with_downstream()
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        with mock.patch(
            # ⚠️ 打在「被查找的那个模块」：`_apply_upstream_hall_claim` 在函数体里
            #    `from apps.business.claim import claim_order` ⇒ 运行期在 claim 模块里查。
            "apps.business.claim.claim_order",
            side_effect=django.db.IntegrityError("uniq_performance_order_user"),
        ):
            outcome = self.deliver_claim_to_upstream()
        self.assertEqual(outcome.applied, 1, outcome)
        refused = PeerSyncEvent.objects.get(
            binding_role="downstream",
            binding_id=str(self.down_binding.pk),
            kind=HALL_CLAIM_REFUSED_KIND,
            target_ref=self.upstream_order.order_no,
        )
        self.assertIn("数据冲突", refused.reason)
        # 单没被接手（半截绩效行也不许留）。
        self.upstream_order.refresh_from_db()
        self.assertEqual(self.upstream_order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(
            Performance.objects.get(
                order=self.upstream_order, emp_type=EmpType.MAIN
            ).user_id
        )


class DeadClaimWritebackTests(ClaimChainFixture):
    """⭐ M3（评审修复轮）：`hall.claim` 走到终态 ⇒ 镜像行 `last_error` 留人话线索。"""

    def _claim_event(self, *, attempts=0):
        return PeerSyncEvent.objects.create(
            binding_role="upstream",
            binding_id=str(self.up_binding.pk),
            direction="out",
            kind="hall.claim",
            target_type="order",
            target_ref=self.mirror.peer_order_no,
            payload={"claim_by": "syn-打手"},
            meta={"occurred_at": self._now().isoformat()},
            idempotency_key="syn-dead-claim-1",
            attempts=attempts,
        )

    def _claim_the_mirror(self):
        from apps.business.models import PeerHallMirror

        PeerHallMirror.objects.filter(pk=self.mirror.pk).update(
            status=PeerHallMirror.Status.CLAIMING,
            claiming_by_id=self.taker.pk,
            claiming_at=self._now(),
        )

    def test_exhausted_retries_write_the_clue(self):
        """烧完退避梯子（第 6 次尝试仍失败）⇒ 终态 failed + 镜像行上留线索。"""
        from unittest import mock

        from apps.business.models import PeerHallMirror
        from apps.business.peer_client import PeerUnreachable
        from apps.business.peer_worker import DEAD_CLAIM_NOTE, deliver_one

        self._claim_the_mirror()
        event = self._claim_event(attempts=5)
        with mock.patch(
            "apps.business.peer_worker.push_events",
            side_effect=PeerUnreachable("syn-对端失联"),
        ):
            outcome = deliver_one(event.pk, now=self._now())
        self.assertEqual(outcome, "failed")
        self.assertEqual(
            PeerSyncEvent.objects.get(pk=event.pk).status,
            PeerSyncEvent.Status.FAILED,
        )
        self.mirror.refresh_from_db()
        # ⛔ 状态不动（claimed 的事实还在，收口仍归 assign / refused / take）。
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.CLAIMING)
        self.assertEqual(self.mirror.last_error, DEAD_CLAIM_NOTE)

    def test_dropped_claim_writes_the_clue_too(self):
        """绑定失效被丢弃（`dropped`）⇒ 同样留线索（不会自愈，行上无声没人知道）。"""
        from apps.business.models import PeerHallMirror, PeerUpstreamBinding
        from apps.business.peer_worker import DROPPED_CLAIM_NOTE, deliver_one

        self._claim_the_mirror()
        event = self._claim_event()
        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(enabled=False)
        outcome = deliver_one(event.pk, now=self._now())
        self.assertEqual(outcome, "dropped")
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.CLAIMING)
        self.assertEqual(self.mirror.last_error, DROPPED_CLAIM_NOTE)

    def test_settled_rows_are_not_touched(self):
        """已被收口的行（非 claiming）⇒ 一个字不覆盖（条件更新只认 claiming）。"""
        from unittest import mock

        from apps.business.models import PeerHallMirror
        from apps.business.peer_client import PeerUnreachable
        from apps.business.peer_worker import deliver_one

        PeerHallMirror.objects.filter(pk=self.mirror.pk).update(
            status=PeerHallMirror.Status.PROMOTED,
            promoted_order=self.upstream_order,
            last_error="",
        )
        event = self._claim_event(attempts=5)
        with mock.patch(
            "apps.business.peer_worker.push_events",
            side_effect=PeerUnreachable("syn-对端失联"),
        ):
            deliver_one(event.pk, now=self._now())
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.last_error, "")


class ReleaseGateTests(ClaimChainFixture):
    """⭐ H1：解冻闸把 peer 镜像行的 claiming 算「占着活」（`wallet._has_active_work` 第三支）。"""

    def test_release_request_is_rejected_while_claiming(self):
        """claiming 期间申请解冻被拒（与鸡蛋铺 claiming 那一支逐字同形的判据）。"""
        from apps.accounts.models import TakerWallet
        from apps.accounts.wallet import WalletError, get_wallet, request_release

        get_wallet(self.taker)
        TakerWallet.objects.filter(user=self.taker).update(frozen=Decimal("50.00"))
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        with self.assertRaises(WalletError) as caught:
            request_release(self.taker)
        self.assertIn("团团通上家单", str(caught.exception))

    def test_release_allowed_once_the_claim_settles_to_gone(self):
        """claiming 收口（refused 回绝）之后可以申请——闸不误伤已落定的人。"""
        from apps.accounts.models import TakerWallet
        from apps.accounts.wallet import get_wallet, request_release

        get_wallet(self.taker)
        TakerWallet.objects.filter(user=self.taker).update(frozen=Decimal("50.00"))
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        # 上家拒绝（单已被别人接走）⇒ 行回 listed、claiming 消失。
        perf = Performance.objects.get(
            order=self.upstream_order, emp_type=EmpType.MAIN
        )
        perf.user = self.down_account
        perf.save(update_fields=["user", "updated_at"])
        self.upstream_order.status = OrderStatus.IN_PROGRESS
        self.upstream_order.save(update_fields=["status", "updated_at"])
        self.deliver_claim_to_upstream()
        refused = PeerSyncEvent.objects.get(kind=HALL_CLAIM_REFUSED_KIND)
        apply_events(
            [{
                "idempotency_key": refused.idempotency_key,
                "kind": refused.kind,
                "target_ref": refused.target_ref,
                "occurred_at": (refused.meta or {}).get("occurred_at"),
                "payload": refused.payload,
            }],
            binding=self.up_binding,
        )
        wallet, created = request_release(self.taker)
        self.assertTrue(created)
        self.assertIsNotNone(wallet.release_requested_at)


class InternalClaimViewTests(ClaimChainFixture):
    """内部接单端点 `POST /api/business/hall/peer/<pk>/claim/`。"""

    def setUp(self):
        super().setUp()
        # ⚠️ 组名用「打手」（⛔ 不是夹具 setUp 给 down_account 授过节点的「代练员」）：
        #    `grant_nodes` 挂的是 Group 对应的**共享 Role**，同组即共享节点 ⇒
        #    用同组会让下面的「无节点 403」恒真失效。
        self.api = make_logged_in_client(
            username="syn-claim-chain-web",
            groups=("打手",),
            nodes=("grab_hall.take_order",),
            games=[self.game],
        )

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_total_deposit_switch_blocks_a_web_claim_without_deposit(self):
        """总闸打开 ⇒ 网页接镜像单同样要过押金（2026-10-25 热修：这条路径此前不传判据）。

        判据：② 业务码非 0（押金那句人话）；② 行仍 `listed`；③ **零** `hall.claim` 事件。
        """
        response = self.api.post(
            f"/api/business/hall/peer/{self.mirror.pk}/claim/",
            {}, content_type="application/json",
        )
        body = response.json()
        self.assertNotEqual(body["code"], 0, body)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        self.assertEqual(PeerSyncEvent.objects.count(), 0)

    def test_app_only_switch_does_not_block_a_web_claim(self):
        """⭐ 反向判据：`DEPOSIT_ENFORCED_APP` **只拦 App**，网页会话照旧能接。

        ⭐ D-0347 起接单是同步直连（假身拦运输层、受理机是真的）⇒ 成功 = 当场转正。
        ⚠️ 受理机是真的 ⇒ Z20 的「共享过」证据必须先摆出来（旧异步路径到不了上家，
        旧用例不需要它；现在没有它会被按「没共享」回绝）。
        """
        self.share_with_downstream()
        with override_settings(DEPOSIT_ENFORCED=False, DEPOSIT_ENFORCED_APP=True):
            with fake_sync_claim_upstream(self, {}):
                response = self.api.post(
                    f"/api/business/hall/peer/{self.mirror.pk}/claim/",
                    {}, content_type="application/json",
                )
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)

    def test_claim_via_http_deals_in_request(self):
        """⭐ D-0347 同步直连：请求内拿到对端判决并当场转正（⛔ 不再有「接单中」响应）。"""
        self.share_with_downstream()
        with fake_sync_claim_upstream(self, {}):
            response = self.api.post(
                f"/api/business/hall/peer/{self.mirror.pk}/claim/",
                {}, content_type="application/json",
            )
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["status"], "promoted")
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        # 对端（假身里的真受理机）排的应答在案；⛔ 我方不再排 outbox hall.claim。
        self.assertFalse(
            PeerSyncEvent.objects.filter(kind="hall.claim").exists()
        )
        self.assertTrue(
            PeerSyncEvent.objects.filter(kind="order.assign").exists()
        )

    def test_out_of_game_scope_is_fail_closed(self):
        """写侧游戏权限卡点：看不到的行接不了，且与「不存在」同形（不泄露存在性）。"""
        from apps.accounts.models import StaffProfile

        other_game = Game.objects.create(name="抢单链无权游戏")
        api = make_logged_in_client(
            username="syn-claim-chain-nogame",
            groups=("打手",),
            nodes=("grab_hall.take_order",),
            games=[other_game],  # ⛔ 不持镜像行那款游戏
        )
        response = api.post(
            f"/api/business/hall/peer/{self.mirror.pk}/claim/",
            {}, content_type="application/json",
        )
        body = response.json()
        # 与「这一行不存在」同形（fail-closed、不泄露存在性）：1012。
        self.assertEqual(body["code"], 1012)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        self.assertEqual(PeerSyncEvent.objects.count(), 0)

    def test_switched_off_binding_is_a_business_error_not_a_500(self):
        """端点层把领域结论原样交给调用方：`1015` + 那句人话（⛔ 不是 1012「已被接走」）。"""
        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(
            hall_sync_enabled=False
        )
        response = self.api.post(
            f"/api/business/hall/peer/{self.mirror.pk}/claim/",
            {}, content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 1015, body)
        self.assertEqual(body["msg"], "还没打开上家大厅同步")
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        self.assertEqual(PeerSyncEvent.objects.count(), 0)

    def test_node_gate_returns_403_without_the_node(self):
        # ⚠️ 组名必须与上面带节点的那个**不同**：`grant_nodes` 挂在 Group 对应的
        #    **共享 Role** 上，同组即共享节点（同组会让这道 403 恒真失效）。
        api = make_logged_in_client(
            username="syn-claim-chain-nonode",
            groups=("运营",),
            nodes=(),
            games=[self.game],
        )
        response = api.post(
            f"/api/business/hall/peer/{self.mirror.pk}/claim/",
            {}, content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)


class TakerApiClaimViewTests(ClaimChainFixture):
    """打手 API 接单端点 `POST /api/taker/v1/hall/peer/<pk>/claim/`。"""

    def setUp(self):
        super().setUp()
        from apps.takerapi.models import TakerApp

        self.anonymous = Client(SERVER_NAME=self.domain.domain)
        self.taker_app = TakerApp.objects.create(
            name="抢单链打手应用",
            app_key="peerhallkey0000001",
            app_secret="peerhallsecret000001",
            taker=self.taker,
            created_by=self.taker,
        )

    def _token(self, app=None):
        from tests.test_takerapi_hall_api import SOURCE_IP, TOKEN_PATH, GRANT_TYPE

        app = self.taker_app if app is None else app
        response = self.anonymous.post(
            TOKEN_PATH,
            {
                "grant_type": GRANT_TYPE,
                "client_id": app.app_key,
                "client_secret": app.app_secret,
            },
            REMOTE_ADDR=SOURCE_IP,
        )
        body = response.json()
        # ⚠️ 成功体是**裸三字段**（⛔ 没有本仓信封），照既有用例同款取法。
        return body["access_token"]

    @override_settings(DEPOSIT_ENFORCED=False, DEPOSIT_ENFORCED_APP=True)
    def test_app_only_switch_blocks_an_app_claim_without_deposit(self):
        """⭐ 2026-10-25 线上热修（某租户真机：无押金打手在 App 上接走了上家卡）。

        判据：App 令牌 + `DEPOSIT_ENFORCED_APP=True` + 打手没押金 ⇒ 接单被拒、
        镜像行仍 `listed`、**零** `hall.claim` 事件（没有越权预占）。
        """
        from tests.test_takerapi_hall_api import SOURCE_IP

        token = self._token()
        response = self.anonymous.post(
            f"/api/taker/v1/hall/peer/{self.mirror.pk}/claim/",
            {}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR=SOURCE_IP,
            HTTP_IDEMPOTENCY_KEY="peer-claim-deposit-app-0001",
        )
        body = response.json()
        self.assertNotEqual(body["code"], 0, body)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        self.assertEqual(PeerSyncEvent.objects.count(), 0)

    @override_settings(DEPOSIT_ENFORCED=False, DEPOSIT_ENFORCED_APP=True)
    def test_app_claim_passes_when_the_taker_quota_is_zero(self):
        """⭐ 免押口径（业务方 2026-10-25）：个人押金额度 = 0 ⇒ 闸开着也允许接、且分文不冻。

        「额度 = 0」的形态 = 打手档案开了个人额度开关、但**没有**该游戏的额度行
        （`deposit_amount_for_user` 的缺行回落就是 0）。反面对照是上面那条「没押金 ⇒ 被拒」，
        两条合起来才钉住「闸开时能不能免押由个人额度决定」。
        """
        from apps.accounts.models import StaffProfile, TakerWallet
        from apps.accounts.wallet import get_wallet

        from tests.test_takerapi_hall_api import SOURCE_IP

        profile = StaffProfile.objects.get(user=self.taker)
        profile.deposit_quota_enabled = True
        profile.save(update_fields=["deposit_quota_enabled", "updated_at"])
        get_wallet(self.taker)
        TakerWallet.objects.filter(user=self.taker).update(frozen=Decimal("0.00"))

        self.share_with_downstream()
        token = self._token()
        # ⭐ D-0347：接单走同步直连（假身拦运输层、受理机是真的）⇒ 成功 = 当场转正。
        with fake_sync_claim_upstream(self, {}):
            response = self.anonymous.post(
                f"/api/taker/v1/hall/peer/{self.mirror.pk}/claim/",
                {}, content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR=SOURCE_IP,
                HTTP_IDEMPOTENCY_KEY="peer-claim-quota-zero-0001",
            )
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["status"], "promoted", body)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.frozen, Decimal("0.00"))

    def test_a_taker_without_the_node_gets_1002_and_the_row_stays_listed(self):
        """⭐ Z11：只持 App 凭据、账号没勾 `grab_hall.take_order` ⇒ 1002。

        ⚠️ 判据三点：① 业务码 `1002` ＋ 与「这张单不存在 / 不在厅」**逐字同形**的文案
        （⛔ 不是 403、⛔ 不是 1012——后两者都会告诉调用方这张单**存在**）；
        ② 镜像行一个字不动；③ **零** `hall.claim` 事件（没有越权预占）。
        """
        from apps.accounts.models import StaffProfile
        from apps.takerapi.models import TakerApp

        from tests.test_takerapi_hall_api import SOURCE_IP

        nobody = get_user_model().objects.create_user(
            username="syn-claim-chain-nonode-bearer", password="pw-syn"
        )
        StaffProfile.objects.create(user=nobody).games.set([self.game])
        app = TakerApp.objects.create(
            name="抢单链无节点应用",
            app_key="peerhallkey0000002",
            app_secret="peerhallsecret000002",
            taker=nobody,
            created_by=nobody,
        )
        token = self._token(app)
        response = self.anonymous.post(
            f"/api/taker/v1/hall/peer/{self.mirror.pk}/claim/",
            {}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR=SOURCE_IP,
            HTTP_IDEMPOTENCY_KEY="peer-claim-nonode-0001",
        )
        body = response.json()
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(body["msg"], "该订单不存在或不在抢单大厅", body)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(self.mirror.claiming_by_id)
        self.assertFalse(
            PeerSyncEvent.objects.filter(kind="hall.claim").exists()
        )

    def test_claim_via_bearer_deals_in_request(self):
        """⭐ D-0347 同步直连：Bearer 接单在请求内拿到判决、当场转正。"""
        from tests.test_takerapi_hall_api import SOURCE_IP

        self.share_with_downstream()
        token = self._token()
        with fake_sync_claim_upstream(self, {}):
            response = self.anonymous.post(
                f"/api/taker/v1/hall/peer/{self.mirror.pk}/claim/",
                {}, content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR=SOURCE_IP,
                # ⚠️ 幂等装饰器：有真实副作用的 POST 必带幂等键（与镜像接单同款，缺它 = 1000）。
                HTTP_IDEMPOTENCY_KEY="peer-claim-0001",
            )
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            set(body["data"]), {"mirror_pk", "peer_order_no", "status", "status_text"}
        )
        self.assertEqual(body["data"]["status"], "promoted")
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        main = Performance.objects.get(
            order=self.mirror.promoted_order, emp_type=EmpType.MAIN
        )
        self.assertEqual(main.user_id, self.taker.pk)

    def test_old_order_no_route_claims_peer_mirror_and_promotes_it(self):
        """大厅返回的 peer 上家单号可直接拼旧单号路径，并走真实同步接手主体。"""
        from apps.business import hall_shadow
        from apps.business.models import EmpType
        from tests.test_takerapi_hall_api import SOURCE_IP

        shadow = hall_shadow.ensure_for_card(self.mirror)
        self.assertIsNotNone(shadow)
        self.assertEqual(shadow.source_order_no, self.mirror.peer_order_no)
        self.assertFalse(
            Performance.objects.filter(order=shadow, emp_type=EmpType.MAIN).exists()
        )
        upstream_only_game = Game.objects.create(name="旧路径用例专用上家游戏")
        Order.objects.filter(pk=self.upstream_order.pk).update(game=upstream_only_game)
        grant_games(self.down_account, upstream_only_game)
        self.share_with_downstream()
        token = self._token()
        captured: dict = {}
        # 两侧夹具共用 schema：上家同号单移出打手游戏范围，本地正式单查询落空；隐藏影子仍由后续镜像回退识别。
        with fake_sync_claim_upstream(self, captured):
            response = self.anonymous.post(
                f"/api/taker/v1/hall/{self.mirror.peer_order_no}/claim/",
                {}, content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR=SOURCE_IP,
                HTTP_IDEMPOTENCY_KEY="peer-old-order-no-claim-0001",
            )
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(set(body["data"]), {"order_no", "status", "status_text"})
        self.assertEqual(captured["base_url"], UP_BASE)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        self.assertIsNotNone(self.mirror.promoted_order_id)
        self.assertEqual(self.mirror.promoted_order_id, shadow.pk)
        shadow.refresh_from_db()
        self.assertIsNone(shadow.hall_card_id)
        self.assertEqual(body["data"]["order_no"], self.mirror.promoted_order.order_no)
        self.assertEqual(body["data"]["status"], "IN_PROGRESS")
        self.assertEqual(body["data"]["status"], self.mirror.promoted_order.status)
        main = Performance.objects.get(
            order=self.mirror.promoted_order, emp_type=EmpType.MAIN
        )
        self.assertEqual(main.user_id, self.taker.pk)

    def test_old_order_no_route_keeps_nonlisted_peer_error_semantics(self):
        """仍可见绑定下的非在挂卡要到达 peer 主体，返回现有 1012 而非 1002。"""
        from apps.business import hall_shadow
        from tests.test_takerapi_hall_api import SOURCE_IP

        shadow = hall_shadow.ensure_for_card(self.mirror)
        self.assertIsNotNone(shadow)
        PeerHallMirror.objects.filter(pk=self.mirror.pk).update(
            status=PeerHallMirror.Status.GONE
        )
        token = self._token()
        with mock.patch(
            "apps.takerapi.api.order_write_views._live_order_in_scope", return_value=None
        ), fake_sync_claim_upstream(self, {}) as push:
            response = self.anonymous.post(
                f"/api/taker/v1/hall/{self.mirror.peer_order_no}/claim/",
                {}, content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR=SOURCE_IP,
                HTTP_IDEMPOTENCY_KEY="peer-old-order-no-gone-0001",
            )
        body = response.json()
        self.assertEqual(body["code"], 1012, body)
        self.assertEqual(push.call_count, 0)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.GONE)

    def test_double_claim_returns_already_taken(self):
        """第二人当场 1012；⛔ 输家不发对端 HTTP（预占输在本地，推都不推）。

        ⭐ D-0347：第一枪走同步直连当场成交；第二枪在行状态闸就输（`promoted` 非
        `listed`）⇒ 1012 且 `push_events` 恰好被调 **1** 次（假身自己的调用计数）。
        """
        from tests.test_takerapi_hall_api import SOURCE_IP

        self.share_with_downstream()
        token = self._token()
        captured: dict = {}
        with fake_sync_claim_upstream(self, captured) as push:
            first = self.anonymous.post(
                f"/api/taker/v1/hall/peer/{self.mirror.pk}/claim/",
                {}, content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR=SOURCE_IP,
                HTTP_IDEMPOTENCY_KEY="peer-claim-0002",
            )
            self.assertEqual(first.json()["code"], 0)
            second = self.anonymous.post(
                f"/api/taker/v1/hall/peer/{self.mirror.pk}/claim/",
                {}, content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR=SOURCE_IP,
                # ⛔ 幂等键不同才是「第二次真请求」；同键重放会拿到第一次的缓存结果。
                HTTP_IDEMPOTENCY_KEY="peer-claim-0003",
            )
        # 第二次 = 并发输家 ⇒「已被接取」一族（1012）。
        self.assertEqual(second.json()["code"], 1012)
        self.assertEqual(push.call_count, 1)

    def test_sync_off_answers_1015_without_leaking_the_platform_name(self):
        """⭐ 2026-10-29（业务方指令）：1015 在 takerapi 出口**换装**——订单来源一个字不出。

        领域层那句常量是「还没打开上家大厅同步」（`HALL_SYNC_OFF_MESSAGE`，含「上家」）
        ⇒ 对外口必须换成**带本租户工作室名**的句子；code 仍是 1015（⛔ 不动码值）。
        判据打在码上（`hall_views` 的换装闸判 `PLATFORM_NOT_CONFIGURED`），这里用
        `assertNotIn` 钉死「鸡蛋铺 / 上家」两个词——将来那句常量再改 wording 也不会漏网。
        ⚠️ 内部网页版端点（`InternalClaimViewTests`）**不换装**，仍走常量原句——那条用例
        钉着常量本体没被改。
        """
        from apps.platform.models import Tenant

        from tests.test_takerapi_hall_api import SOURCE_IP

        # 工作室名写进行（中间件每个请求都从库里现取租户 ⇒ 只改内存对象不够）。
        Tenant.objects.filter(pk=self.tenant.pk).update(name="抢单链测试工作室")
        self.tenant.name = "抢单链测试工作室"
        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(
            hall_sync_enabled=False
        )
        token = self._token()
        response = self.anonymous.post(
            f"/api/taker/v1/hall/peer/{self.mirror.pk}/claim/",
            {}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR=SOURCE_IP,
            HTTP_IDEMPOTENCY_KEY="peer-claim-sync-off-0001",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 1015, body)
        self.assertEqual(
            body["msg"], "还没配置好抢单链测试工作室的同步账号，请联系管理员", body
        )
        self.assertNotIn("鸡蛋铺", body["msg"])
        self.assertNotIn("上家", body["msg"])
        # 领域侧终态与既有那条内部端点用例同款：行没动、零事件。
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        self.assertEqual(PeerSyncEvent.objects.count(), 0)


# ══ 大厅共享的身份键 = **来源单号**（计划 §2 第 3 条 / §5-4）══════════
#: 上家那张单的**来源单号**（与内置单号取值不同 ⇒ 取错列能分辨，E-0024）。
CLAIM_SOURCE_NO = "SYN-CLAIM-SRC-1"


class SourceIdentityHallChainTests(ClaimChainFixture):
    """`hall.throw` / `hall.claim` / `order.assign` 三跳都用来源单号，整链仍能走通。

    ⚠️ 新口径下下家的镜像行 `peer_order_no` 记的是**上家的来源单号**（本夹具显式造出这个
    形状：上家单有来源号 + 镜像行按来源号建）——这正是升级后新事件到达时落成的样子。
    """

    def setUp(self):
        super().setUp()
        self.upstream_order.source_order_no = CLAIM_SOURCE_NO
        self.upstream_order.save(update_fields=["source_order_no"])
        # 上家 `hall.throw` 现在带的 `target_ref` 就是来源号 ⇒ 下家落出来的镜像行同值。
        PeerHallMirror.objects.filter(pk=self.mirror.pk).update(
            peer_order_no=CLAIM_SOURCE_NO
        )

    def test_the_whole_chain_carries_the_source_order_no(self):
        # ① 上家广播：hall.throw 的 target_ref = 来源号（⛔ 不是内置号）。
        self.share_with_downstream()
        throw = PeerSyncEvent.objects.filter(kind=HALL_THROW_KIND).latest("pk")
        self.assertEqual(throw.target_ref, CLAIM_SOURCE_NO)
        self.assertNotEqual(throw.target_ref, self.upstream_order.order_no)

        # ② 下家抢单：hall.claim 的 target_ref = 镜像行上那个号 = 来源号。
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        claim = PeerSyncEvent.objects.filter(kind="hall.claim").latest("pk")
        self.assertEqual(claim.target_ref, CLAIM_SOURCE_NO)

        # ③ 上家收 claim：按**来源单号**命中自己那张单 ⇒ 机器接手（判别力：
        #    只按 `Order.order_no` 查的实现会回 `unknown_order` ⇒ 拒绝 / 不指派）。
        outcome = self.deliver_claim_to_upstream()
        self.assertEqual(outcome.applied, 1, outcome)
        self.upstream_order.refresh_from_db()
        self.assertEqual(self.upstream_order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(
                order=self.upstream_order, emp_type=EmpType.MAIN
            ).user_id,
            self.down_account.pk,
        )

        # ④ 上家回推的 order.assign 也用来源号；下家据此建单 / 转正，两列都是来源号。
        assign = PeerSyncEvent.objects.filter(
            kind="order.assign", binding_role="downstream"
        ).latest("pk")
        self.assertEqual(assign.target_ref, CLAIM_SOURCE_NO)
        outcome = self.deliver_assign_to_downstream()
        self.assertEqual(outcome.applied, 1, outcome)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        order = self.mirror.promoted_order
        self.assertIsNotNone(order)
        self.assertEqual(order.source_order_no, CLAIM_SOURCE_NO)
        self.assertEqual(order.peer_links.get().peer_order_no, CLAIM_SOURCE_NO)


class ShadowOrderPromotionTests(ClaimChainFixture):
    """⭐ 2026-10-26 业务方拍板：卡被本租户打手抢走时，**直接用那张隐藏单**（不再新建单）。

    判据：
    ① 转正单 **就是**隐藏单（主键相同、`Order` 只多这一张）；
    ② 隐藏单挂打手（MAIN 绩效行）+ 状态 `IN_PROGRESS` + 展示价；`hall_card` 清空（回到列表）；
    ③ 打手自己的鸡蛋铺收到 `member.assign`（既有钩子）；其余收到过 `member.launch` 的店收到
       `member.withdraw`（撤其他家大厅）。
    """

    def _jidanpu_binding(self, username, url, *, user=None):
        from apps.business.models import JidanpuMemberBinding

        if user is None:
            user = get_user_model().objects.create_user(
                username=username, password="pw-syn"
            )
            grant_games(user, self.game)
        return JidanpuMemberBinding.objects.create(
            user=user,
            jidanpu_base_url=url,
            account=username,
            password="pw-syn",
            enabled=True,
        )

    def test_the_claim_promotes_the_hidden_order_in_place(self):
        from apps.business.jidanpu_member import (
            KIND_ASSIGN,
            KIND_LAUNCH,
            KIND_WITHDRAW,
        )
        from apps.business.models import JidanpuSyncEvent

        own_shop = self._jidanpu_binding(
            "syn-shadow-own-shop",
            "https://syn-shadow-own-shop.test.invalid",
            user=self.taker,
        )
        other_shop = self._jidanpu_binding(
            "syn-shadow-other-shop", "https://syn-shadow-other-shop.test.invalid"
        )
        # 上家把这张单真的推下来（建 / 刷新镜像卡 ⇒ 建隐藏单 + 两家都排 launch）。
        outcome = apply_events(
            [{
                "idempotency_key": "syn-shadow-throw",
                "kind": HALL_THROW_KIND,
                "target_ref": self.upstream_order.order_no,
                "occurred_at": "2026-10-25T10:00:00+00:00",
                "payload": hall_payload(self.upstream_order.order_no),
            }],
            binding=self.up_binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)
        mirror = PeerHallMirror.objects.get(
            peer_order_no=self.upstream_order.order_no
        )
        shadow = mirror.shadow_order
        self.assertIsNotNone(shadow, "卡落成 listed 之后没有隐藏单")
        shadow_pk = shadow.pk
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind=KIND_LAUNCH, target_ref=shadow.order_no,
            ).count(),
            2,
        )

        self.share_with_downstream()                       # Z20 的共享证据
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=mirror.pk)
        self.deliver_claim_to_upstream()                   # 上家受理 → order.assign
        promoted = self.deliver_assign_to_downstream()     # 下家收 assign ⇒ 转正
        self.assertEqual(promoted.applied, 1, promoted)

        mirror.refresh_from_db()
        shadow.refresh_from_db()
        self.assertEqual(mirror.status, PeerHallMirror.Status.PROMOTED)
        self.assertEqual(mirror.promoted_order_id, shadow_pk)
        self.assertEqual(shadow.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(shadow.hall_price, Decimal("50.00"))
        self.assertIsNone(shadow.hall_card_id, "转正后仍是隐藏单 ⇒ 列表里看不到它")
        # ⛔ 双订单：同一个来源单号只有**一张**（复用了隐藏单，不再新建）。
        self.assertEqual(
            Order.objects.filter(
                source_order_no=self.upstream_order.order_no
            ).count(),
            1,
        )
        perf = Performance.objects.get(order=shadow, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, self.taker.pk)
        self.assertIsNotNone(perf.claimed_at)
        # ③ 自己的店收 assign（录单）、别家收 withdraw（撤下）。
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind=KIND_ASSIGN, target_ref=shadow.order_no,
                payload__member_binding_id=own_shop.pk,
            ).count(),
            1,
        )
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind=KIND_WITHDRAW, target_ref=shadow.order_no,
                payload__member_binding_id=other_shop.pk,
            ).count(),
            1,
        )
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind=KIND_WITHDRAW, target_ref=shadow.order_no,
                payload__member_binding_id=own_shop.pk,
            ).count(),
            0,
            "接手人自己的店不该被退单",
        )


class MachineClaimShadowOrderTests(ClaimChainFixture):
    """⭐ 独立评审阻断 1：**下家鸡蛋铺打手接了隐藏单** ⇒ 我方必须能把那张卡接回来。

    ## 选型（②镜像 claim 路径，理由见 `jidanpu_member.machine_claim_order`）

    隐藏单**没有 MAIN 绩效行**（它只是把卡同步给下家大厅的载体）⇒ 原生 `claim_order` 的
    条件更新必然 0 行（`jidanpu_member.py` 的 `AUTO_CLAIM_NO_TAKER_ROW_REASON` 那一档），
    下家一接就失败、卡永远回不来。下家在**对方店里**接走的是**那张卡** ⇒ 正确动作是复用
    既有 `peer_hall.claim_peer_mirror` 预占同一张卡、向上家排 `hall.claim`；上家受理后
    `_promote_claiming_mirror` 会**原地**把隐藏单转正（挂打手 / `IN_PROGRESS` / 清 `hall_card`）。

    ⛔ 不选「建隐藏单时补一行无主 MAIN 行」：那会让「隐藏单没有绩效行」这条不变量失效，
    大厅 / 我的单 / 绩效 / takerapi 四处读面都得靠显式排除兜底；而且卡自身仍要在上家
    被接走 —— 补 MAIN 行并不回答「卡为什么回不来」。
    """

    def _shop(self):
        """本租户的一条下游鸡蛋铺绑定（`binding.user` = 接了这张单的打手）。"""
        from apps.business.models import JidanpuMemberBinding

        return JidanpuMemberBinding.objects.create(
            user=self.taker,
            jidanpu_base_url="https://syn-shadow-shop.test.invalid",
            account="syn-shadow-shop",
            password="pw-syn",
            enabled=True,
        )

    def test_a_downstream_taker_claiming_the_hidden_order_claims_the_card(self):
        from apps.business import hall_shadow, jidanpu_member

        shop = self._shop()
        shadow = hall_shadow.ensure_for_card(self.mirror)
        self.assertIsNotNone(shadow)
        self.share_with_downstream()                       # Z20 的共享证据

        outcome = jidanpu_member.machine_claim_order(
            binding=shop, order=shadow,
            remote_uid=5001, remote_name="syn-shadow-taker",
            remote_order_id="JDP-SHADOW-1",
            source_note="uid=5001 显示名=syn-shadow-taker",
        )
        self.assertTrue(outcome.claimed, outcome)
        self.assertIsNone(outcome.claim_error)

        # ① 卡被**本租户**预占（claiming_by = 那个下家打手），并向上家排了 hall.claim。
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.CLAIMING)
        self.assertEqual(self.mirror.claiming_by_id, self.taker.pk)
        self.assertEqual(
            PeerSyncEvent.objects.filter(
                binding_role="upstream", direction="out",
                kind="hall.claim", target_ref=self.upstream_order.order_no,
            ).count(),
            1,
        )

        # ② 上家受理 → order.assign 回来 ⇒ 隐藏单**原地转正**（卡真的回来了）。
        self.assertEqual(self.deliver_claim_to_upstream().applied, 1)
        self.assertEqual(self.deliver_assign_to_downstream().applied, 1)
        shadow.refresh_from_db()
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        self.assertEqual(self.mirror.promoted_order_id, shadow.pk)
        self.assertEqual(shadow.status, OrderStatus.IN_PROGRESS)
        self.assertIsNone(shadow.hall_card_id)
        main = Performance.objects.get(order=shadow, emp_type=EmpType.MAIN)
        self.assertEqual(main.user_id, self.taker.pk)

    def test_a_taker_without_the_take_node_cannot_claim_the_hidden_order(self):
        """⭐ 二轮评审 Important 4：隐藏单分支也要过**节点门**（与普通机器接手同一判据）。

        普通机器接手走 `claim_order(nodes=nodes_of(binding.user))` ⇒ 状态机
        `apply_transition` 判 `WAITING_GRAB → IN_PROGRESS` 那条边；隐藏单分支绕过了
        `claim_order`（它没有 MAIN 行），过去**一道节点都不判** ⇒ 一个没有接单节点的账号
        也能把大厅卡预占走（卡被抢走，而那个人连接单权限都没有）。

        判据（三条，缺一条都可能是「因为别的理由失败」）：
        ① 结局是 `denied`（不是 `claimed`）；
        ② 卡**还在厅里**（`listed`）、**没有**排 `hall.claim` —— 一次写都没发生；
        ③ 拒绝原因指认的是**节点门**（含「缺少所需权限节点」），⛔ 不是「已被接走」。
        """
        from django.contrib.auth import get_user_model

        from apps.business import hall_shadow, jidanpu_member
        from apps.business.models import JidanpuMemberBinding

        #: 一个**什么节点都没有**的下家打手（`nodes_of` 回空集 ⇒ 状态机 fail-closed）。
        no_node_user = get_user_model().objects.create_user(
            username="syn-shadow-no-node", password="pw-syn"
        )
        shop = JidanpuMemberBinding.objects.create(
            user=no_node_user,
            jidanpu_base_url="https://syn-shadow-shop-nonode.test.invalid",
            account="syn-shadow-shop-nonode",
            password="pw-syn",
            enabled=True,
        )
        shadow = hall_shadow.ensure_for_card(self.mirror)
        self.assertIsNotNone(shadow)
        self.share_with_downstream()

        outcome = jidanpu_member.machine_claim_order(
            binding=shop, order=shadow,
            remote_uid=5002, remote_name="syn-no-node",
            remote_order_id="JDP-SHADOW-NONODE",
            source_note="uid=5002 显示名=syn-no-node",
        )

        self.assertFalse(outcome.claimed, outcome)
        self.assertIsNotNone(outcome.denial, "节点门没有拒绝：隐藏单被无节点账号接走了")
        self.assertIn("缺少所需权限节点", str(outcome.denial))
        self.mirror.refresh_from_db()
        self.assertEqual(
            self.mirror.status, PeerHallMirror.Status.LISTED,
            "卡被没有接单节点的账号预占了",
        )
        self.assertIsNone(self.mirror.claiming_by_id)
        self.assertFalse(
            PeerSyncEvent.objects.filter(
                binding_role="upstream", direction="out", kind="hall.claim",
            ).exists(),
            "被节点门挡下时⛔ 不该向上家排 hall.claim",
        )


class PeerPreferredSwitchOffTests(ClaimChainFixture):
    """⭐ 优选租户开关（D-0328，2026-10-29）——peer 镜像链的「优选强制押金」覆盖点。

    关（N）⇒ 展示价 250 的镜像也**不再**强制押金：总闸关 + 0 余额 ⇒ 照样预占
    （`listed → claiming`）、一分不冻。开（无覆盖行 = 默认 Y）⇒ 覆盖照旧
    （对照：同夹具不关开关时 0 余额被 `1013`「押金不足」拒——那一条由
    `test_peer_hall_claim` 的押金用例族与 `test_hall_preferred_gate` 钉着）。
    变异方向：把 `claim_peer_mirror` 覆盖点上的 `preferred_enabled()` 摘掉 ⇒ 本条红
    （覆盖复活 ⇒ `1013`，预占不成）。
    """

    def test_switch_off_high_price_mirror_claims_without_freezing(self):
        from django.test import override_settings as _override_settings

        from apps.accounts.models import TakerWallet
        from apps.business.models import GameDepositRule, SystemSwitch

        GameDepositRule.objects.create(game=self.game, amount=Decimal("300.00"))
        wallet = TakerWallet.objects.create(user=self.taker, balance=Decimal("0.00"))
        PeerHallMirror.objects.filter(pk=self.mirror.pk).update(remote_hall_price=Decimal("250.00"))
        SystemSwitch.objects.create(key="hall_preferred_orders", value="N")
        with _override_settings(DEPOSIT_ENFORCED=False):
            row = claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.assertEqual(row.status, PeerHallMirror.Status.CLAIMING)
        self.assertEqual(row.claiming_by_id, self.taker.pk)
        wallet.refresh_from_db()
        self.assertEqual(
            (wallet.frozen, wallet.balance), (Decimal("0.00"), Decimal("0.00")),
            "开关关着还冻了押金 = peer 链的优选强制押金没被跳过",
        )
