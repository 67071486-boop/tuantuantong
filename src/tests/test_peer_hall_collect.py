"""peer 大厅「指派收单」：`hall.take` 带 `leave_reason` 分流 + 接收侧收回归池。

判定表（实施计划 `docs/plans/2026-11-07-peer-assign-collect.md` §2 是唯一口径）：

① 发送侧：指派族（`order_writes.assign_order` 的那次迁移）发**带因** take
   （wire 顶层键 `leave_reason="assigned"`）；抢单受理 / 撤回发**裸** take；
② 接收侧（`peer_inbound._apply_hall_event` 的 take 支）：镜像摘牌行为逐字不变；
   只认带因 take 才尝试收单（锚 = 镜像行 `promoted_order` 或上游链行本地单，
   条件 = `WAITING_GRAB` 且 MAIN 绩效行无接手人；预期失败 ⇒ 放弃该单、事件照常 applied）；
③ 多跳：转发链透传 reason（接收侧收单迁移再发带因 take ⇒ 链式收单）；
④ 兼容矩阵（计划 §5）逐行有测试；重投幂等、老对端无键、未知值、开关关全部零动作。

⚠️ 夹具值全部合成值（`syn-*` / `*.test.invalid` / `测试*`）；断言打数据库行。
⚠️ 必须用 `TenantTestCase`（`apps.business` 只在 `TENANT_APPS`，照 peer 线先例）。
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerHallMirror,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
    SettleStatus,
)
from apps.business.peer_dispatch import enqueue_hall_event
from apps.business.peer_inbound import apply_events
from apps.business.peer_sync import (
    HALL_TAKE_KIND,
    HALL_TAKE_LEAVE_ASSIGNED,
    hash_token,
    new_token,
)
from apps.business import peer_worker
from tests.p5_auth_helpers import (
    grant_games,
    grant_nodes,
    make_logged_in_client,
)

ORDERS = "/api/business/orders"
HALL_CLAIM = "/api/business/hall/{no}/claim/"

#: 与 `test_peer_hall_dispatch` 同一扇门（照抄，别凭记忆缩）。
NODES = (
    "order.change_status",
    "order.assign_taker",
    "order.send_to_grab_hall",
    "order.list",
    "grab_hall.take_order",
)

UP_BASE = "https://peer-hall-up-collect.test.invalid"
DOWN_BASE = "https://peer-hall-down-collect.test.invalid"


def _no(n):
    """20 位订单号（`OrderViewSet.lookup_value_regex` 要求 URL 里必须 20 位）。"""
    return f"2026110700000000{n:04d}"


def hall_payload(no, *, price="50.00"):
    """一条 `order_payload` 形状的合成载荷（白名单键 + 非默认值）。"""
    return {
        "order_no": no,
        "status": "WAITING_GRAB",
        "total_price": "100.00",
        "hall_price": price,
        "remark": "syn-备注",
        "game_name": "指派收单夹具游戏",
        "server_id": None,
        "project_id": None,
        "project_text": "syn-项目",
        "customer_phone": "",
        "customer_qq": "",
        "content": "syn-需求",
        "source_order_no": "",
    }


def take_event(no, *, leave_reason="UNSET"):
    """一条 `hall.take` 上行事件（worker 投出来的顶层形状）。

    `leave_reason="UNSET"`（哨兵）⇒ 不带这个键 = 老对端的裸 take；
    传 `None` 等真值则照写顶层键。
    """
    event = {
        "idempotency_key": f"syn-take-{no}",
        "kind": HALL_TAKE_KIND,
        "target_ref": no,
        "occurred_at": "2026-11-07T10:00:00+00:00",
        "payload": hall_payload(no),
    }
    if leave_reason != "UNSET":
        event["leave_reason"] = leave_reason
    return event


class ReceiverFixture(TenantTestCase):
    """我方是**下家**：一条生效上家绑定 + 一张已转正镜像卡 + 转正出的正式单。"""

    def setUp(self):
        self.game = Game.objects.create(name="指派收单夹具游戏")
        from django.contrib.auth import get_user_model as _get_user

        operator = _get_user().objects.create_user(
            username="syn-collect-up-op", password="pw-syn"
        )
        self.up_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家收单",
            peer_base_url=UP_BASE,
            peer_account="syn-up-collect",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-hall-local-collect.test.invalid",
            bound_by=operator,
            hall_sync_enabled=True,
        )
        # 我方的下家绑定（收单迁移会向它广播带因 take ⇒ 多跳链式收单的种子）。
        self.down_account = self._staff_account("syn-collect-down-acct")
        self.down_binding = PeerDownstreamBinding.objects.create(
            account=self.down_account,
            peer_base_url=DOWN_BASE,
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            hall_push_enabled=True,
        )
        self.ref = _no(11)
        self.formal = self._formal(self.ref, OrderStatus.WAITING_GRAB)
        self.mirror = PeerHallMirror.objects.create(
            peer_upstream_binding=self.up_binding,
            peer_base_url=UP_BASE,
            peer_order_no=self.ref,
            status=PeerHallMirror.Status.PROMOTED,
            promoted_order=self.formal,
            game=self.game,
            payload=hall_payload(self.ref),
            remote_hall_price=Decimal("50.00"),
            first_seen_at=self._now(),
            last_seen_at=self._now(),
        )

    # ── 夹具件 ────────────────────────────────────────────────────
    @staticmethod
    def _now():
        return timezone.now()

    def _staff_account(self, username):
        user = get_user_model().objects.create_user(username=username, password="pw-syn")
        StaffProfile.objects.create(
            user=user,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        grant_games(user, self.game)
        return user

    def _formal(self, no, status):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            total_price=Decimal("100.00"),
            hall_price=Decimal("50.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("10.00")
        )
        return order

    def _apply(self, *events):
        return apply_events(list(events), binding=self.up_binding)

    def _assigned(self):
        """对夹具那张卡发一条**带因** take。"""
        return self._apply(take_event(self.ref, leave_reason=HALL_TAKE_LEAVE_ASSIGNED))

    # ── 断言件 ────────────────────────────────────────────────────
    def _collect_logs(self):
        return OperationLog.objects.filter(action="peer.inbound_hall_collect")

    def _out_takes(self):
        return PeerSyncEvent.objects.filter(
            direction="out", binding_role="downstream", kind=HALL_TAKE_KIND
        )


class CollectOnAssignedTakeTests(ReceiverFixture):
    """判定表第 ① 行 + 多跳转发（⭐ 主链路）。"""

    def test_assigned_take_collects_the_promoted_formal_order(self):
        outcome = self._assigned()
        self.assertEqual(outcome.applied, 1)
        # 正式单：WAITING_GRAB → UNASSIGNED（收回归池，不退单不删单）。
        self.formal.refresh_from_db()
        self.assertEqual(self.formal.status, OrderStatus.UNASSIGNED)
        # 镜像行为逐字不变：promoted 是终态，take 不碰它（摘牌只发生在 listed 行上）。
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        # 台账：恰好一行 peer.inbound_hall_collect，挂在正式单上。
        self.assertEqual(self._collect_logs().count(), 1)
        log = self._collect_logs().get()
        self.assertEqual(log.order_no, self.formal.order_no)
        # 多跳：本租户向下家再发的 take 带同款 reason（链式收单的种子）。
        takes = list(self._out_takes())
        self.assertGreaterEqual(len(takes), 1)
        for row in takes:
            self.assertEqual((row.meta or {}).get("leave_reason"), "assigned")

    def test_bare_take_from_an_old_upstream_collects_nothing(self):
        """兼容矩阵「老上家 → 新下家」：take 不带键 ⇒ 只摘牌、零收单。"""
        outcome = self._apply(take_event(self.ref))
        self.assertEqual(outcome.applied, 1)
        self.formal.refresh_from_db()
        self.assertEqual(self.formal.status, OrderStatus.WAITING_GRAB)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        self.assertEqual(self._collect_logs().count(), 0)
        for row in self._out_takes():
            self.assertNotIn("leave_reason", row.meta or {})

    def test_in_progress_formal_is_not_collected(self):
        """兼容矩阵「代练中不动」：正式单已有人在做 ⇒ 零动作（但事件照常 applied）。"""
        taker = self._staff_account("syn-collect-in-progress-taker")
        Performance.objects.filter(order=self.formal).update(user=taker)
        Order.objects.filter(pk=self.formal.pk).update(status=OrderStatus.IN_PROGRESS)
        outcome = self._assigned()
        self.assertEqual(outcome.applied, 1)
        self.formal.refresh_from_db()
        self.assertEqual(self.formal.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._collect_logs().count(), 0)

    def test_waiting_grab_with_a_taker_is_not_collected_and_money_is_untouched(self):
        """WAITING_GRAB 但 MAIN 行有接手人 ⇒ 不收；顺带正向钉「不碰钱」：
        `temp_amount` / `bonus_amount` 一个字不动。"""
        taker = self._staff_account("syn-collect-waiting-taker")
        Performance.objects.filter(order=self.formal).update(
            user=taker, bonus_amount=Decimal("3.50")
        )
        outcome = self._assigned()
        self.assertEqual(outcome.applied, 1)
        self.formal.refresh_from_db()
        self.assertEqual(self.formal.status, OrderStatus.WAITING_GRAB)
        perf = Performance.objects.get(order=self.formal)
        self.assertEqual(perf.user_id, taker.pk)
        self.assertEqual(perf.temp_amount, Decimal("10.00"))
        self.assertEqual(perf.bonus_amount, Decimal("3.50"))
        self.assertEqual(self._collect_logs().count(), 0)

    def test_link_anchor_collects_when_the_mirror_row_is_gone(self):
        """第二锚：镜像行不在（gone / 无卡），链行指到的本地单照收。"""
        PeerHallMirror.objects.filter(pk=self.mirror.pk).update(
            status=PeerHallMirror.Status.GONE, gone_at=self._now()
        )
        PeerOrderLink.objects.create(
            order=self.formal,
            upstream_binding=self.up_binding,
            peer_order_no=self.ref,
        )
        outcome = self._assigned()
        self.assertEqual(outcome.applied, 1)
        self.formal.refresh_from_db()
        self.assertEqual(self.formal.status, OrderStatus.UNASSIGNED)
        self.assertEqual(self._collect_logs().count(), 1)

    def test_a_hidden_order_derived_from_own_card_is_not_a_target(self):
        """锚的隐藏单排除（照 `_local_order_for_peer_ref` 的两道 exclude）：第一锚不成立时
        （镜像行已 gone），链行指到的是本租户自己大厅卡派生的隐藏单（`hall_card` 非空）
        ⇒ 不是合法收单目标，不收。"""
        own_card = PeerHallMirror.objects.create(
            peer_upstream_binding=self.up_binding,
            peer_base_url=UP_BASE,
            peer_order_no=_no(99),
            status=PeerHallMirror.Status.LISTED,
            game=self.game,
            payload=hall_payload(_no(99)),
            remote_hall_price=Decimal("30.00"),
            first_seen_at=self._now(),
        )
        PeerHallMirror.objects.filter(pk=self.mirror.pk).update(
            status=PeerHallMirror.Status.GONE, gone_at=self._now()
        )
        PeerOrderLink.objects.create(
            order=self.formal,
            upstream_binding=self.up_binding,
            peer_order_no=self.ref,
        )
        Order.objects.filter(pk=self.formal.pk).update(hall_card=own_card)
        outcome = self._assigned()
        self.assertEqual(outcome.applied, 1)
        self.formal.refresh_from_db()
        self.assertEqual(self.formal.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(self._collect_logs().count(), 0)

    def test_a_soft_deleted_order_is_not_a_collect_target(self):
        """软删排除：链行指到的正式单已软删（默认 manager 取不到）⇒ 不是收单目标。"""
        PeerHallMirror.objects.filter(pk=self.mirror.pk).update(
            status=PeerHallMirror.Status.GONE, gone_at=self._now()
        )
        PeerOrderLink.objects.create(
            order=self.formal,
            upstream_binding=self.up_binding,
            peer_order_no=self.ref,
        )
        Order.objects.filter(pk=self.formal.pk).update(deleted_at=self._now())
        outcome = self._assigned()
        self.assertEqual(outcome.applied, 1)
        self.formal.refresh_from_db()
        self.assertEqual(self.formal.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(self._collect_logs().count(), 0)

    def test_a_settled_performance_abandons_the_collect_and_keeps_the_event_applied(self):
        """预期失败（已结账闸）⇒ savepoint 回滚、放弃该单；镜像摘牌与 applied 不动。"""
        Performance.objects.filter(order=self.formal).update(
            settle_status=SettleStatus.SETTLED,
            settled_by="财务甲",
            settled_amount=Decimal("8.00"),
        )
        outcome = self._assigned()
        self.assertEqual(outcome.applied, 1)
        self.formal.refresh_from_db()
        self.assertEqual(self.formal.status, OrderStatus.WAITING_GRAB)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        self.assertEqual(self._collect_logs().count(), 0)

    def test_redelivering_the_assigned_take_is_fully_idempotent(self):
        """重投幂等：第二次镜像条件更新落空、正式单已收过 ⇒ 全程 no-op、台账不加行。"""
        first = self._assigned()
        second = self._assigned()
        self.assertEqual(first.applied, 1)
        self.assertEqual(second.applied, 1)
        self.formal.refresh_from_db()
        self.assertEqual(self.formal.status, OrderStatus.UNASSIGNED)
        self.assertEqual(self._collect_logs().count(), 1)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)


class MirrorForwardLegTests(ReceiverFixture):
    """多跳转发腿：listed 卡被 take 摘牌时，向本租户下家的转发 take 随上游原因带因/裸。"""

    def _listed_mirror(self, no):
        return PeerHallMirror.objects.create(
            peer_upstream_binding=self.up_binding,
            peer_base_url=UP_BASE,
            peer_order_no=no,
            status=PeerHallMirror.Status.LISTED,
            game=self.game,
            payload=hall_payload(no),
            remote_hall_price=Decimal("50.00"),
            first_seen_at=self._now(),
            last_seen_at=self._now(),
        )

    def test_a_reasoned_take_forwards_the_reason_to_our_downstream(self):
        row = self._listed_mirror(_no(41))
        outcome = self._apply(take_event(_no(41), leave_reason=HALL_TAKE_LEAVE_ASSIGNED))
        self.assertEqual(outcome.applied, 1)
        row.refresh_from_db()
        self.assertEqual(row.status, PeerHallMirror.Status.GONE)
        forwarded = self._out_takes().get(target_ref=_no(41))
        self.assertEqual((forwarded.meta or {}).get("leave_reason"), "assigned")

    def test_a_bare_take_forwards_without_the_reason(self):
        row = self._listed_mirror(_no(42))
        outcome = self._apply(take_event(_no(42)))
        self.assertEqual(outcome.applied, 1)
        row.refresh_from_db()
        self.assertEqual(row.status, PeerHallMirror.Status.GONE)
        forwarded = self._out_takes().get(target_ref=_no(42))
        self.assertNotIn("leave_reason", forwarded.meta or {})


class CompatAndShapeTests(ReceiverFixture):
    """兼容矩阵剩余行：未知值、开关关、载荷形状。"""

    def test_a_garbage_leave_reason_value_is_ignored(self):
        """值不是约定字符串（如对象）⇒ 只当裸 take：摘牌、零收单、不炸。"""
        outcome = self._apply(take_event(self.ref, leave_reason={"why": "assigned"}))
        self.assertEqual(outcome.applied, 1)
        self.formal.refresh_from_db()
        self.assertEqual(self.formal.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(self._collect_logs().count(), 0)

    def test_a_take_without_a_time_still_lands_and_does_not_collect(self):
        """老对端连 occurred_at 都不带：take 落地（摘牌）不受影响；无键 ⇒ 不收单。"""
        event = take_event(self.ref)
        event.pop("occurred_at")
        outcome = self._apply(event)
        self.assertEqual(outcome.applied, 1)
        self.formal.refresh_from_db()
        self.assertEqual(self.formal.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(self._collect_logs().count(), 0)

    def test_receiver_switch_off_skips_the_take_branch_entirely(self):
        """接收开关关 ⇒ `hall.*` 整批跳过：不摘牌、不收单、不报错。"""
        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(
            hall_sync_enabled=False
        )
        self.up_binding.refresh_from_db()
        outcome = self._assigned()
        self.assertEqual((outcome.applied, outcome.skipped), (0, 0))
        self.formal.refresh_from_db()
        self.assertEqual(self.formal.status, OrderStatus.WAITING_GRAB)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        self.assertEqual(self._collect_logs().count(), 0)


class SenderSideTests(TenantTestCase):
    """发送侧分流钉子：指派带因、抢单受理与撤回裸发、pending 合并形状。"""

    def setUp(self):
        self.game = Game.objects.create(name="指派收单发送夹具游戏")
        self.binding_on = self._bind("syn-collect-send-on")
        self.api = make_logged_in_client(
            username="syn-collect-send-cs", groups=("客服",), nodes=NODES
        )

    def _staff_account(self, username):
        user = get_user_model().objects.create_user(username=username, password="pw-syn")
        StaffProfile.objects.create(
            user=user,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        grant_games(user, self.game)
        return user

    def _bind(self, username):
        return PeerDownstreamBinding.objects.create(
            account=self._staff_account(username),
            peer_base_url=DOWN_BASE,
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            hall_push_enabled=True,
        )

    def _order(self, no, *, status=OrderStatus.WAITING_GRAB, taker=None):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            total_price=Decimal("100.00"),
            hall_price=Decimal("50.00"),
        )
        Performance.objects.create(
            order=order, user=taker, emp_type=EmpType.MAIN,
            temp_amount=Decimal("10.00"),
        )
        return order

    def _post_ok(self, url, payload):
        response = self.api.post(url, payload, content_type="application/json")
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body

    def _takes(self):
        return PeerSyncEvent.objects.filter(kind=HALL_TAKE_KIND, direction="out")

    def test_assign_from_the_hall_carries_the_leave_reason(self):
        """判定表第 ① 行（发送侧）：/assign/ 发的 take 顶层带 leave_reason="assigned"。"""
        order = self._order(_no(21))
        self._post_ok(
            f"{ORDERS}/{order.order_no}/assign/",
            {"user_id": self.binding_on.account_id},
        )
        takes = list(self._takes())
        self.assertGreaterEqual(len(takes), 1)
        for row in takes:
            self.assertEqual((row.meta or {}).get("leave_reason"), "assigned")

    def test_claim_and_withdraw_takes_stay_bare(self):
        """判定表第 ②③ 行：抢单受理与撤回发的 take 不带 leave_reason。"""
        claimed = self._order(_no(22))
        withdraw = self._order(_no(23))
        # ⚠️ `make_logged_in_client` 自己建账号（同名二次 create 会撞唯一约束）——
        #    组与节点由 helper 一并挂上，⛔ 不再手工建同人。
        claim_api = make_logged_in_client(
            username="syn-collect-claimer", groups=("代练员",), nodes=NODES
        )
        self._post_ok_by(
            claim_api, HALL_CLAIM.format(no=claimed.order_no), {}
        )
        self._post_ok(
            f"{ORDERS}/{withdraw.order_no}/transition/", {"target": "UNASSIGNED"}
        )
        takes = list(self._takes())
        self.assertEqual(len(takes), 2)
        for row in takes:
            self.assertNotIn("leave_reason", row.meta or {})

    def _post_ok_by(self, client, url, payload):
        response = client.post(url, payload, content_type="application/json")
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body

    def test_a_pending_bare_take_upgrades_when_an_assigned_take_merges(self):
        """pending 合并（新形状）：先裸 take、后带因 take ⇒ 同一行刷新出 leave_reason。"""
        order = self._order(_no(24))
        enqueue_hall_event(order, kind=HALL_TAKE_KIND)
        enqueue_hall_event(
            order, kind=HALL_TAKE_KIND,
            meta_extra={"leave_reason": HALL_TAKE_LEAVE_ASSIGNED},
        )
        rows = list(self._takes())
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].meta or {}).get("leave_reason"), "assigned")

    def test_merge_keeps_old_keys_and_overwrites_new_ones(self):
        """合并语义另一半：旧键保留、新键覆盖——bare take 后到也不吞 meta 形状。"""
        order = self._order(_no(25))
        enqueue_hall_event(order, kind=HALL_TAKE_KIND)
        first = self._takes().get()
        enqueue_hall_event(
            order, kind=HALL_TAKE_KIND,
            meta_extra={"leave_reason": HALL_TAKE_LEAVE_ASSIGNED},
        )
        merged = self._takes().get()
        self.assertEqual(merged.pk, first.pk)
        self.assertEqual(
            set(merged.meta), {"occurred_at", "leave_reason"}
        )
        self.assertEqual(merged.meta.get("leave_reason"), "assigned")


class RescueCloneTests(TenantTestCase):
    """worker B1 救援克隆：meta 原样继承 ⇒ leave_reason 不丢（计划 §6 最后一行）。"""

    def test_the_rescue_clone_keeps_the_leave_reason(self):
        game = Game.objects.create(name="救援克隆夹具游戏")
        account = self._staff_account("syn-rescue-acct")
        binding = PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url=DOWN_BASE,
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            hall_push_enabled=True,
        )
        order = Order.objects.create(
            game=game, order_no=_no(31), status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("100.00"), hall_price=Decimal("50.00"),
        )
        Performance.objects.create(
            order=order, user=account, emp_type=EmpType.MAIN,
            temp_amount=Decimal("10.00"),
        )
        made = enqueue_hall_event(
            order, kind=HALL_TAKE_KIND,
            meta_extra={"leave_reason": HALL_TAKE_LEAVE_ASSIGNED},
        )
        self.assertEqual(len(made), 1)
        event = made[0]
        event.status = PeerSyncEvent.Status.FAILED
        event.attempts = 99
        event.save(update_fields=["status", "attempts"])
        self.assertTrue(peer_worker._rescue_hall_event(event, now=timezone.now()))
        clone = PeerSyncEvent.objects.filter(
            kind=HALL_TAKE_KIND, status=PeerSyncEvent.Status.PENDING
        ).latest("pk")
        self.assertEqual((clone.meta or {}).get("leave_reason"), "assigned")
        self.assertTrue((clone.meta or {}).get("rescued"))

    def _staff_account(self, username):
        user = get_user_model().objects.create_user(username=username, password="pw-syn")
        grant_games(user, Game.objects.get())
        return user
