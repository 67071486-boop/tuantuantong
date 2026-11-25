"""D-0303 团团通大厅镜像：**下家侧**接收内核（spec §3.2 / §4.3）。

判据分两块（照 `tests/test_peer_order_push.py::ReceiveEventTests` 的形状，直接调
`apply_events` / `apply_upstream_events`——批事务边界在视图，这两个内核是判据的落点）：

① **hall.* 五类的落地**：`hall.throw` upsert（新建 listed / gone 复活【D-0300 口径】 /
   promoted 永不复活 / claiming 不动）、`hall.take`（listed → gone 条件更新）、
   `hall.reprice`（只刷在挂行）、两个应答（refused 收回 claiming、ok 不改状态）；
   形状闸（`game_name` 缺失 = bad_shape 硬跳过 + 零写入，同 A14 口径）。
② **转正回填**（`claiming → promoted` + `promoted_order`）：随正式订单的落库支走；
   ⛔ 只认 claiming（普通 peer 单 0 行副作用）；`order.update` 先到（建单支）也能转正。

⚠️ 夹具值全部合成值；断言打数据库行（E-0024）。
⚠️ 必须用 `TenantTestCase`。
"""
from decimal import Decimal

from django_tenants.test.cases import TenantTestCase

from apps.business.api.peer_views import receive_events
from apps.business.models import (
    Game,
    Order,
    OrderStatus,
    PeerHallMirror,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
)
from apps.business.peer_inbound import (
    CLAIM_REFUSED_CONFLICT_NOTE,
    CLAIM_REFUSED_NOTE,
    CLAIM_REFUSED_NOT_SHARED_NOTE,
    apply_events,
    apply_upstream_events,
)
from apps.business.peer_sync import (
    HALL_CLAIM_OK_KIND,
    HALL_CLAIM_REFUSED_KIND,
    HALL_REPRICE_KIND,
    HALL_TAKE_KIND,
    HALL_THROW_KIND,
    hash_token,
    new_token,
)
from apps.core.error_codes import ErrorCode

UP_BASE = "https://peer-hall-up.test.invalid"


def hall_payload(no, *, game_name="镜像接收夹具游戏", price="50.00", status="WAITING_GRAB"):
    """一条 `order_payload` 形状的合成载荷（白名单键 + 非默认值，取错列时断言分辨得出）。"""
    return {
        "order_no": no,
        "status": status,
        "total_price": "100.00",
        "hall_price": price,
        "remark": "syn-备注",
        "game_name": game_name,
        "server_id": None,
        "project_id": None,
        "project_text": "syn-项目",
        "customer_phone": "",
        "customer_qq": "",
        "content": "syn-需求",
        "source_order_no": "",
    }


def hall_event(no, kind=HALL_THROW_KIND, **kw):
    """一条大厅事件（worker 投出来的形状：kind / target_ref / payload / 事件级时刻）。

    ⚠️ `hall.take` 在真实投递里也带**订单白名单载荷**（出站广播统一带），接收侧不消费
    但「payload 必须是对象」的形状闸照过 ⇒ 夹具与真实形状一致，⛔ 不传 `None`。
    ⚠️ 显式给 `payload=` 时原样用它（`hall.claim_refused` 的**拒绝原因**就装在里面）；
    缺省才按 `hall_payload(no, **kw)` 拼一条订单载荷。
    """
    missing = object()
    payload = kw.pop("payload", missing)
    if payload is missing:
        payload = hall_payload(no, **kw)
    return {
        "idempotency_key": f"syn-{no}-{kind}",
        "kind": kind,
        "target_ref": no,
        "occurred_at": "2026-10-25T10:00:00+00:00",
        "payload": payload,
    }


class InboundFixture(TenantTestCase):
    """下家侧夹具：一条**我方为下家**的生效上家绑定。"""

    def setUp(self):
        self.game = Game.objects.create(name="镜像接收夹具游戏")
        from django.contrib.auth import get_user_model

        operator = get_user_model().objects.create_user(
            username="syn-peer-hall-inbound-op", password="pw-syn"
        )
        self.operator = operator
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家丙",
            peer_base_url=UP_BASE,
            peer_account="syn-up-inbound",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-hall-local2.test.invalid",
            bound_by=operator,
            # ⭐ P1：接收侧开关默认关 ⇒ 落地夹具显式打开（关闸由专门用例钉）。
            hall_sync_enabled=True,
        )

    def _apply(self, *events):
        return apply_events(list(events), binding=self.binding)

    def _mirror(self, no, *, status=PeerHallMirror.Status.LISTED, price="50.00"):
        return PeerHallMirror.objects.create(
            peer_upstream_binding=self.binding,
            peer_base_url=UP_BASE,
            peer_order_no=no,
            status=status,
            game=self.game,
            payload=hall_payload(no, price=price),
            remote_hall_price=Decimal(price),
            first_seen_at=parse_now(),
            last_seen_at=parse_now(),
        )


def parse_now():
    from django.utils.dateparse import parse_datetime

    return parse_datetime("2026-10-25T09:00:00+00:00")


class NegativePriceFallbackTests(InboundFixture):
    """⭐ D-0306 入向应用处兜底闸（防版本偏斜）：负价按「没带价 / 跳过」处理。

    ⚠️ 正常版本走不到这里（`apply_events` 的大厅事件分派处有整条硬跳过）；这两条**直接调
    应用内核**，钉的正是「版本偏斜 / 手工重放把负价送到落地层」那一档。
    """

    def test_a_negative_throw_payload_lands_with_no_price(self):
        from apps.business.peer_inbound import _land_hall_throw

        ok, outcome, row = _land_hall_throw(
            binding=self.binding,
            payload=hall_payload("UP-NEG-THROW", price="-9.00"),
            target_ref="UP-NEG-THROW",
            note_skip=lambda *_args: None,
            now=parse_now(),
        )
        self.assertTrue(ok)
        self.assertEqual(outcome, "created")
        self.assertIsNone(row.remote_hall_price, "负价不许落进镜像行的上家价")
        self.assertEqual(row.status, PeerHallMirror.Status.LISTED)

    def test_a_negative_reprice_is_neither_landed_nor_forwarded(self):
        from apps.business.models import PeerDownstreamBinding, PeerSyncEvent
        from apps.business.peer_inbound import _apply_hall_event

        PeerDownstreamBinding.objects.create(
            account=self.operator,
            peer_base_url="https://peer-hall-down-neg.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            hall_push_enabled=True,
        )
        mirror = self._mirror("UP-NEG-REPRICE", price="50.00")

        applied = _apply_hall_event(
            kind=HALL_REPRICE_KIND,
            payload=hall_payload("UP-NEG-REPRICE", price="-9.00"),
            binding=self.binding,
            target_ref="UP-NEG-REPRICE",
            note_skip=lambda *_args: None,
            now=parse_now(),
        )
        self.assertTrue(applied)
        mirror.refresh_from_db()
        self.assertEqual(mirror.remote_hall_price, Decimal("50.00"), "负价把上家价改坏了")
        self.assertFalse(
            PeerSyncEvent.objects.filter(kind=HALL_REPRICE_KIND).exists(),
            "负价改价被转发给下游了",
        )

        # 阳性对照：同一行收到**正价**改价 ⇒ 落价 + 转发一条（否则上面两条可能只是"从来没转发过"）。
        applied = _apply_hall_event(
            kind=HALL_REPRICE_KIND,
            payload=hall_payload("UP-NEG-REPRICE", price="60.00"),
            binding=self.binding,
            target_ref="UP-NEG-REPRICE",
            note_skip=lambda *_args: None,
            now=parse_now(),
        )
        self.assertTrue(applied)
        mirror.refresh_from_db()
        self.assertEqual(mirror.remote_hall_price, Decimal("60.00"))
        self.assertEqual(
            PeerSyncEvent.objects.filter(kind=HALL_REPRICE_KIND).count(), 1
        )


class HallThrowTests(InboundFixture):
    """`hall.throw` 的 upsert 与终态口径（D-0300 从第一天）。"""

    def test_first_throw_creates_a_listed_row(self):
        outcome = self._apply(hall_event("UP-1"))
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(outcome.skipped, 0)
        row = PeerHallMirror.objects.get()
        self.assertEqual(row.status, PeerHallMirror.Status.LISTED)
        self.assertEqual(row.peer_base_url, UP_BASE)
        self.assertEqual(row.peer_upstream_binding_id, self.binding.pk)
        # 游戏按名字精确对齐（本租户已有同名 ⇒ 不新建）。
        self.assertEqual(row.game_id, self.game.pk)
        self.assertEqual(row.remote_hall_price, Decimal("50.00"))
        self.assertEqual(row.payload["order_no"], "UP-1")
        self.assertIsNotNone(row.first_seen_at)
        self.assertIsNone(row.gone_at)

    def test_unknown_game_name_is_autocreated(self):
        """对不上的名字 ⇒ 自动建同名游戏（A14），单不漏。"""
        outcome = self._apply(hall_event("UP-2", game_name="syn-全新游戏甲"))
        self.assertEqual(outcome.applied, 1)
        row = PeerHallMirror.objects.get(peer_order_no="UP-2")
        self.assertIsNotNone(row.game_id)
        self.assertTrue(Game.objects.filter(name="syn-全新游戏甲").exists())

    def test_duplicate_throw_is_benign_and_refreshes(self):
        """重复投递 = 良性：仍一行，价与投影刷成最新。"""
        self._apply(hall_event("UP-3"))
        outcome = self._apply(hall_event("UP-3", price="80.00"))
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(PeerHallMirror.objects.count(), 1)
        row = PeerHallMirror.objects.get()
        self.assertEqual(row.remote_hall_price, Decimal("80.00"))
        self.assertEqual(row.payload["hall_price"], "80.00")

    def test_throw_on_gone_row_revives_it(self):
        """⭐ D-0300：`gone` 重新见到即复活（条件更新 + 清 gone_at / last_error）。"""
        row = self._mirror("UP-4")
        PeerHallMirror.objects.filter(pk=row.pk).update(
            status=PeerHallMirror.Status.GONE,
            gone_at=parse_now(),
            last_error="曾经被误判过",
        )
        outcome = self._apply(hall_event("UP-4"))
        self.assertEqual(outcome.applied, 1)
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(fresh.gone_at)
        self.assertEqual(fresh.last_error, "")

    def test_throw_on_promoted_row_never_revives(self):
        """⭐ D-0300 的另一半：`promoted` 永不复活（重复接单的口子不开）。"""
        row = self._mirror("UP-5")
        order = Order.objects.create(
            game=self.game, order_no="20261025000000090001",
            status=OrderStatus.UNASSIGNED,
        )
        PeerHallMirror.objects.filter(pk=row.pk).update(
            status=PeerHallMirror.Status.PROMOTED, promoted_order=order,
        )
        outcome = self._apply(hall_event("UP-5"))
        self.assertEqual(outcome.applied, 1)
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.PROMOTED)
        self.assertEqual(fresh.promoted_order_id, order.pk)

    def test_throw_on_claiming_row_leaves_it_alone(self):
        """`claiming` 不动（它的收口在 claim 应答链上）。"""
        row = self._mirror("UP-6")
        PeerHallMirror.objects.filter(pk=row.pk).update(
            status=PeerHallMirror.Status.CLAIMING,
        )
        self._apply(hall_event("UP-6"))
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.CLAIMING)

    def test_missing_game_name_is_bad_shape_and_writes_nothing(self):
        """`game_name` 缺失 = bad_shape 硬跳过 + **零写入**（连游戏行都不建，同 A14 口径）。"""
        event = hall_event("UP-7")
        event["payload"] = {**event["payload"], "game_name": None}
        outcome = self._apply(event)
        self.assertEqual(outcome.applied, 0)
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(outcome.skipped_details[0]["code"], "bad_shape")
        self.assertEqual(PeerHallMirror.objects.count(), 0)
        self.assertEqual(Game.objects.count(), 1)  # 只有夹具那款


    def test_rebind_same_url_repoints_the_row(self):
        """⭐ M5（评审修复轮）：解绑重绑同一域名 ⇒ 新绑定 throw 后，旧行重挂到新绑定
        （可见 + 可接；不重挂会被「绑定启用」判据静默藏掉）。"""
        from django.contrib.auth import get_user_model

        from apps.business import peer_hall
        from apps.business.models import PeerUpstreamBinding

        # 行先挂在 A（夹具那条绑定）名下，A 随后解绑（enabled=False）。
        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        # 同域名的**新**绑定 B（重绑）。
        operator_b = get_user_model().objects.create_user(
            username="syn-peer-hall-rebind-op", password="pw-syn"
        )
        binding_b = PeerUpstreamBinding.objects.create(
            peer_name="测试上家丙",
            peer_base_url=UP_BASE,
            peer_account="syn-up-inbound-b",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-hall-local2.test.invalid",
            bound_by=operator_b,
            enabled=True,
            hall_sync_enabled=True,
        )
        # ⚠️ 重绑后上家带的是 **B 的令牌** ⇒ 事件以 B 的身份进来（不是夹具的 A）。
        outcome = apply_events([hall_event("UP-1")], binding=binding_b)
        self.assertEqual(outcome.applied, 1)
        row = PeerHallMirror.objects.get(peer_order_no="UP-1")
        self.assertEqual(row.peer_upstream_binding_id, binding_b.pk)
        # 读口与抢单前置都认 B（A 已停用，重挂失败的话这里就空了/拒了）。
        self.assertTrue(
            peer_hall.peer_hall_queryset().filter(pk=row.pk).exists()
        )


    def test_a_late_push_throw_does_not_revive_a_gone_row(self):
        """⭐ A3 / M-3：事件时刻**不晚于** `gone_at` ⇒ 迟到的 throw 只刷 `last_seen_at`，
        ⛔ 不复活（与拉取支的轮次闸对齐；幽灵卡没有第二次入口）。"""
        row = self._mirror("UP-8")
        gone_at = parse_now()
        PeerHallMirror.objects.filter(pk=row.pk).update(
            status=PeerHallMirror.Status.GONE,
            gone_at=gone_at,
            last_error="曾经被误判过",
        )
        # `hall_event` 缺省带 `occurred_at = 2026-10-25T10:00:00+00:00`（晚于 gone_at）
        # ⇒ 这里把事件时刻改到 gone_at **之前**（相等也判旧，同 `<=` 口径）。
        event = hall_event("UP-8")
        event["occurred_at"] = "2026-10-25T08:30:00+00:00"
        outcome = self._apply(event)
        self.assertEqual(outcome.applied, 1)
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.GONE)
        self.assertEqual(fresh.gone_at, gone_at)
        self.assertEqual(fresh.last_error, "曾经被误判过")
        # 「只刷 last_seen_at」那一半：迟到事件仍是「见过」的证据。
        self.assertGreater(fresh.last_seen_at, gone_at)

    def test_a_fresh_push_throw_still_revives_a_gone_row(self):
        """对照：事件时刻**晚于** `gone_at` ⇒ 照旧复活（闸不误伤真重推）。"""
        row = self._mirror("UP-9")
        PeerHallMirror.objects.filter(pk=row.pk).update(
            status=PeerHallMirror.Status.GONE,
            gone_at=parse_now(),
        )
        event = hall_event("UP-9")
        event["occurred_at"] = "2026-10-25T11:00:00+00:00"
        outcome = self._apply(event)
        self.assertEqual(outcome.applied, 1)
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(fresh.gone_at)

    def test_a_push_throw_without_a_time_fail_open_and_revives(self):
        """旧对端不带 `occurred_at` ⇒ fail-open 照旧复活（兼容不破）。"""
        row = self._mirror("UP-10")
        PeerHallMirror.objects.filter(pk=row.pk).update(
            status=PeerHallMirror.Status.GONE,
            gone_at=parse_now(),
        )
        event = hall_event("UP-10")
        event.pop("occurred_at")
        outcome = self._apply(event)
        self.assertEqual(outcome.applied, 1)
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(fresh.gone_at)


class GameRuleSyncTests(InboundFixture):
    def test_hall_push_and_pull_match_scope_keep_existing_card_and_snapshot(self):
        from apps.business.peer_hall import display_price
        from apps.business.peer_inbound import apply_hall_pull_rows

        old = self._mirror("SYN-OLD")
        self.binding.game_rules = [{"game_id": self.game.pk, "hall_price_percent": 33}]
        self.binding.save(update_fields=["game_rules"])
        self._apply(hall_event("SYN-PUSH", price="111.00"))
        result = apply_hall_pull_rows(
            self.binding,
            [{"target_ref": "SYN-PULL", "payload": hall_payload("SYN-PULL", price="111.00")}],
            now=parse_now(), round_started_at=parse_now(),
        )
        self.assertEqual(result["failures"], 0)
        for no in ("SYN-PUSH", "SYN-PULL"):
            row = PeerHallMirror.objects.get(peer_order_no=no)
            self.assertEqual(row.hall_price_percent_at_sync, 33)
            self.assertEqual(display_price(row), Decimal("36.63"))

        other_game = Game.objects.create(name="测试另选游戏")
        self.binding.game_rules = [{"game_id": other_game.pk, "hall_price_percent": 76}]
        self.binding.save(update_fields=["game_rules"])
        self._apply(hall_event("SYN-PUSH", price="120.00"))
        self._apply(hall_event("SYN-OUTSIDE", game_name="测试未登记游戏"))
        self._apply(hall_event(old.peer_order_no, kind=HALL_TAKE_KIND))
        self.assertEqual(
            display_price(PeerHallMirror.objects.get(peer_order_no="SYN-PUSH")),
            Decimal("39.60"),
        )
        self.assertFalse(PeerHallMirror.objects.filter(peer_order_no="SYN-OUTSIDE").exists())
        self.assertFalse(Game.objects.filter(name="测试未登记游戏").exists())
        old.refresh_from_db()
        self.assertEqual(old.status, PeerHallMirror.Status.GONE)

    def test_new_formal_order_uses_initial_game_percent_and_real_reassignment_still_updates(self):
        self.binding.game_rules = [{"game_id": self.game.pk, "hall_price_percent": 30}]
        self.binding.save(update_fields=["game_rules"])
        self._apply(hall_event("SYN-FORMAL", kind="order.assign", price="81.00"))
        link = PeerOrderLink.objects.get(peer_order_no="SYN-FORMAL")
        self.assertEqual(link.hall_price_percent_at_sync, 30)
        self.assertEqual(link.order.total_price, Decimal("81.00"))
        self.assertEqual(link.order.hall_price, Decimal("24.30"))

        other_game = Game.objects.create(name="测试另选游戏")
        self.binding.game_rules = [{"game_id": other_game.pk, "hall_price_percent": 86}]
        self.binding.save(update_fields=["game_rules"])
        event = hall_event("SYN-FORMAL", kind="order.assign", price="100.00")
        event["occurred_at"] = "2026-10-25T10:01:00+00:00"
        self._apply(event)
        link.order.refresh_from_db()
        self.assertEqual(link.order.total_price, Decimal("100.00"))
        self.assertEqual(link.order.hall_price, Decimal("30.00"))
        self._apply(hall_event("SYN-NOT-FORMAL", kind="order.assign"))
        self.assertFalse(PeerOrderLink.objects.filter(peer_order_no="SYN-NOT-FORMAL").exists())

class HallSwitchGateTests(InboundFixture):
    """⭐ P1（业务方 2026-10-25）：接收侧开关**默认关**；关着 = 行为等同没有这条功能。

    三处闸各一条（本文件 = inbound 那一处）：`hall.*` **整批跳过**——不建行、不改行、
    ✅ **不报错**（⛔ 不进 `skipped_details`：那会让上家按失败退避重投、烧到终态 failed，
    而「本租户还没打开接收」不是对端的错）。
    """

    def test_the_model_defaults_are_off_and_hundred(self):
        """两列默认值 = `False` / `100`（迁移对存量行也是这两个值 ⇒ 老租户升级后是**关**）。"""
        from apps.business.models import PeerUpstreamBinding

        row = PeerUpstreamBinding.objects.create(
            peer_name="测试上家默认值",
            peer_base_url="https://peer-hall-default.test.invalid",
            peer_account="syn-up-default",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-hall-local-default.test.invalid",
            bound_by=self.binding.bound_by,
        )
        self.assertIs(row.hall_sync_enabled, False)
        self.assertEqual(row.hall_price_percent, 100)

    def test_switched_off_throw_creates_no_row_and_no_game(self):
        from apps.business.models import PeerUpstreamBinding

        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_sync_enabled=False
        )
        # ⚠️ 生产里 `binding` 是认证类现查的一行；夹具体例是 setUp 里那个对象 ⇒
        #    必须刷新，否则闸读到的是内存里的旧值（用例会假绿/假红）。
        self.binding.refresh_from_db()
        outcome = self._apply(hall_event("UP-OFF-1", game_name="syn-关闸不建的游戏"))
        # ⛔ 不报错：既不 applied（没落地）也不 skipped（不是"这条事件坏了"）。
        self.assertEqual(outcome.applied, 0)
        self.assertEqual(outcome.skipped, 0)
        self.assertEqual(outcome.skipped_details, [])
        self.assertEqual(PeerHallMirror.objects.count(), 0)
        # ⛔ 连游戏行都不建（整批跳过发生在 `resolve_game` 之前）。
        self.assertFalse(Game.objects.filter(name="syn-关闸不建的游戏").exists())

    def test_switched_off_take_and_reprice_leave_the_existing_row_alone(self):
        """存量行**一个字节都不改**：既不被 take 摘成 gone，价也不被 reprice 刷。"""
        from apps.business.models import PeerUpstreamBinding

        row = self._mirror("UP-OFF-2", price="50.00")
        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_sync_enabled=False
        )
        # ⚠️ 生产里 `binding` 是认证类现查的一行；夹具体例是 setUp 里那个对象 ⇒
        #    必须刷新，否则闸读到的是内存里的旧值（用例会假绿/假红）。
        self.binding.refresh_from_db()
        take = self._apply(hall_event("UP-OFF-2", kind=HALL_TAKE_KIND))
        reprice = self._apply(
            hall_event("UP-OFF-2", kind=HALL_REPRICE_KIND, price="99.00")
        )
        self.assertEqual((take.applied, take.skipped), (0, 0))
        self.assertEqual((reprice.applied, reprice.skipped), (0, 0))
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(fresh.gone_at)
        self.assertEqual(fresh.remote_hall_price, Decimal("50.00"))

    def test_switched_back_on_resumes_landing(self):
        """关着跳过的不是"毒事件"：打开之后同一张单的 throw 正常落地（正对照）。"""
        from apps.business.models import PeerUpstreamBinding

        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_sync_enabled=False
        )
        # ⚠️ 生产里 `binding` 是认证类现查的一行；夹具体例是 setUp 里那个对象 ⇒
        #    必须刷新，否则闸读到的是内存里的旧值（用例会假绿/假红）。
        self.binding.refresh_from_db()
        self._apply(hall_event("UP-OFF-3"))
        self.assertEqual(PeerHallMirror.objects.count(), 0)
        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_sync_enabled=True
        )
        self.binding.refresh_from_db()
        outcome = self._apply(hall_event("UP-OFF-3"))
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(PeerHallMirror.objects.count(), 1)


class HallTakeAndRepriceTests(InboundFixture):
    """`hall.take` / `hall.reprice` 的条件更新。"""

    def test_take_on_listed_marks_gone(self):
        row = self._mirror("UP-10")
        outcome = self._apply(hall_event("UP-10", kind=HALL_TAKE_KIND))
        self.assertEqual(outcome.applied, 1)
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.GONE)
        self.assertIsNotNone(fresh.gone_at)

    def test_take_on_claiming_row_keeps_status_but_leaves_evidence(self):
        """双事件竞态（assign 与 take 同窗）：claiming 行的**状态**收口在应答链，
        ⛔ 不被 take 摘走；⭐ M2：但必须留下「上家已摘牌」的证据（`gone_at`）。"""
        row = self._mirror("UP-11")
        PeerHallMirror.objects.filter(pk=row.pk).update(
            status=PeerHallMirror.Status.CLAIMING,
        )
        self._apply(hall_event("UP-11", kind=HALL_TAKE_KIND))
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.CLAIMING)
        self.assertIsNotNone(fresh.gone_at)

    def test_take_then_refused_lands_gone_not_listed(self):
        """⭐ M2 序列：claiming → take（留证据）→ refused ⇒ 收口成 `gone`
        （⛔ 不是 listed——上家那张单早已不在，回大厅是永远抢不到的僵尸行）。"""
        row = self._mirror("UP-15")
        PeerHallMirror.objects.filter(pk=row.pk).update(
            status=PeerHallMirror.Status.CLAIMING,
        )
        self._apply(hall_event("UP-15", kind=HALL_TAKE_KIND))
        outcome = self._apply(hall_event("UP-15", kind=HALL_CLAIM_REFUSED_KIND))
        self.assertEqual(outcome.applied, 1)
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.GONE)
        self.assertIsNotNone(fresh.gone_at)
        self.assertIsNone(fresh.claiming_by_id)
        self.assertIn("无法再抢", fresh.last_error)

    def test_take_on_missing_row_is_benign(self):
        """行不存在 = 摘牌终态已成立 ⇒ applied（不给发送侧退避重投一条废事件）。"""
        outcome = self._apply(hall_event("UP-12", kind=HALL_TAKE_KIND))
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(PeerHallMirror.objects.count(), 0)

    def test_reprice_updates_the_listed_row(self):
        row = self._mirror("UP-13", price="50.00")
        outcome = self._apply(
            hall_event("UP-13", kind=HALL_REPRICE_KIND, price="99.00")
        )
        self.assertEqual(outcome.applied, 1)
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.remote_hall_price, Decimal("99.00"))
        self.assertEqual(fresh.payload["hall_price"], "99.00")

    def test_reprice_on_gone_row_is_benign(self):
        """终态行不吃 reprice（摘了牌的单价跟不跟没有意义）。"""
        row = self._mirror("UP-14", price="50.00")
        PeerHallMirror.objects.filter(pk=row.pk).update(
            status=PeerHallMirror.Status.GONE, gone_at=parse_now(),
        )
        self._apply(hall_event("UP-14", kind=HALL_REPRICE_KIND, price="99.00"))
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.remote_hall_price, Decimal("50.00"))
        self.assertEqual(fresh.status, PeerHallMirror.Status.GONE)


class ClaimAnswerTests(InboundFixture):
    """`hall.claim_refused` / `hall.claim_ok` 的收口。"""

    def test_refused_returns_claiming_to_listed(self):
        """spec §4.3：`claiming → listed`（条件更新）+ 行上留人话原因。"""
        row = self._mirror("UP-20")
        PeerHallMirror.objects.filter(pk=row.pk).update(
            status=PeerHallMirror.Status.CLAIMING,
        )
        outcome = self._apply(hall_event("UP-20", kind=HALL_CLAIM_REFUSED_KIND))
        self.assertEqual(outcome.applied, 1)
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(fresh.claiming_by_id)
        self.assertEqual(fresh.last_error, CLAIM_REFUSED_NOTE)

    def test_refused_with_a_permanent_reason_takes_the_card_off_the_hall(self):
        """⭐ 业务方 2026-10-25：拒绝原因是**订单已不存在 / 没共享给这家** ⇒ 卡彻底下架。

        `gone` 而不是 `listed`：这张卡在上家那边**再也没有了**（或从来就没共享给这家），
        回大厅只会造一张永远抢不到的僵尸行。
        """
        for reason in (CLAIM_REFUSED_NOTE, CLAIM_REFUSED_NOT_SHARED_NOTE):
            with self.subTest(reason=reason):
                no = f"UP-30-{abs(hash(reason)) % 10000}"
                row = self._mirror(no)
                PeerHallMirror.objects.filter(pk=row.pk).update(
                    status=PeerHallMirror.Status.CLAIMING,
                )
                outcome = self._apply(
                    hall_event(no, kind=HALL_CLAIM_REFUSED_KIND, payload={"reason": reason})
                )
                self.assertEqual(outcome.applied, 1)
                fresh = PeerHallMirror.objects.get(pk=row.pk)
                self.assertEqual(fresh.status, PeerHallMirror.Status.GONE)
                self.assertIsNotNone(fresh.gone_at)
                self.assertIsNone(fresh.claiming_by_id)
                self.assertEqual(fresh.last_error, reason)

    def test_refused_with_a_transient_reason_returns_to_listed(self):
        """⭐ 拒绝原因是**可重试**的那一档（网络 / 上游暂时没响应 / 数据冲突）⇒ 回大厅等重试。

        ⚠️ 「上家一直没回话」**根本不会**发来 `hall.claim_refused`（那条路由
        `peer_hall.reclaim_stuck_claims` 300 秒兜底、也是回 `listed`）；本用例钉的是
        应答里**带可重试原因**与**不带原因**（未知 ⇒ fail-open 到可重试）这两档。
        """
        for payload in (
            {"reason": CLAIM_REFUSED_CONFLICT_NOTE},
            {},
        ):
            with self.subTest(payload=payload):
                no = f"UP-31-{abs(hash(str(payload))) % 10000}"
                row = self._mirror(no)
                PeerHallMirror.objects.filter(pk=row.pk).update(
                    status=PeerHallMirror.Status.CLAIMING,
                )
                self._apply(
                    hall_event(no, kind=HALL_CLAIM_REFUSED_KIND, payload=payload)
                )
                fresh = PeerHallMirror.objects.get(pk=row.pk)
                self.assertEqual(fresh.status, PeerHallMirror.Status.LISTED)
                self.assertIsNone(fresh.claiming_by_id)
                self.assertEqual(
                    fresh.last_error,
                    payload.get("reason") or CLAIM_REFUSED_NOTE,
                )

    def test_refused_on_listed_row_is_benign(self):
        """没抢过（非 claiming）⇒ 良性，且不把 listed 行写坏。"""
        row = self._mirror("UP-21")
        self._apply(hall_event("UP-21", kind=HALL_CLAIM_REFUSED_KIND))
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.LISTED)
        self.assertEqual(fresh.last_error, "")

    def test_claim_ok_changes_nothing(self):
        """成功应答本身不改行：收口是随后的 `order.assign`（→ promoted）。"""
        row = self._mirror("UP-22")
        PeerHallMirror.objects.filter(pk=row.pk).update(
            status=PeerHallMirror.Status.CLAIMING,
        )
        outcome = self._apply(hall_event("UP-22", kind=HALL_CLAIM_OK_KIND))
        self.assertEqual(outcome.applied, 1)
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.CLAIMING)

    def test_refused_via_real_endpoint_shape(self):
        """过一遍真接收端点（`receive_events` 的信封装配），确认 skipped 明细可回传。"""
        row = self._mirror("UP-23")
        PeerHallMirror.objects.filter(pk=row.pk).update(
            status=PeerHallMirror.Status.CLAIMING,
        )
        result = receive_events(self.binding, [hall_event("UP-23", kind=HALL_CLAIM_REFUSED_KIND)])
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["skipped"], 0)


class PromotionBackfillTests(InboundFixture):
    """转正回填（spec §4.3）：claiming 行 + 正式订单到达 ⇒ `promoted` + `promoted_order`。"""

    def _claiming(self, no, *, price="50.00"):
        """一条**带着抢单人**的 claiming 行（生产形状：条件更新恒同时写人）。"""
        from django.contrib.auth import get_user_model

        taker = get_user_model().objects.create_user(
            username=f"syn-peer-hall-claiming-{no}", password="pw-syn"
        )
        row = self._mirror(no, price=price)
        PeerHallMirror.objects.filter(pk=row.pk).update(
            status=PeerHallMirror.Status.CLAIMING,
            claiming_by_id=taker.pk,
            claiming_at=parse_now(),
        )
        return row

    def test_assign_promotes_the_claiming_mirror(self):
        row = self._claiming("UP-30")
        outcome = self._apply(
            {
                "idempotency_key": "syn-assign-30",
                "kind": "order.assign",
                "target_ref": "UP-30",
                "occurred_at": "2026-10-25T10:00:00+00:00",
                "payload": hall_payload("UP-30"),
            }
        )
        self.assertEqual(outcome.applied, 1)
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.PROMOTED)
        # 正式订单照既有 peer 路落库；⭐ D-0303（H1）：抢单转正落「代练中」
        # （P4 唯一例外，判据见 apply_events 建单支的 `promoting_claim`）。
        order = fresh.promoted_order
        self.assertIsNotNone(order)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(order.source_order_no, "UP-30")
        self.assertTrue(
            PeerOrderLink.objects.filter(
                upstream_binding=self.binding, peer_order_no="UP-30", order=order
            ).exists()
        )

    def test_existing_card_promotes_at_original_percent_after_game_is_removed(self):
        self.binding.game_rules = [{"game_id": self.game.pk, "hall_price_percent": 60}]
        self.binding.save(update_fields=["game_rules"])
        row = self._claiming("SYN-PROMOTION", price="50.00")
        PeerHallMirror.objects.filter(pk=row.pk).update(hall_price_percent_at_sync=60)
        self.binding.game_rules = []
        self.binding.save(update_fields=["game_rules"])
        event = hall_event("SYN-PROMOTION", kind="order.assign", price="50.00")
        self.assertEqual(self._apply(event).applied, 1)
        row.refresh_from_db()
        order = row.promoted_order
        self.assertEqual(row.status, PeerHallMirror.Status.PROMOTED)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(order.hall_price, Decimal("30.00"))
        self.assertEqual(PeerOrderLink.objects.get(order=order).hall_price_percent_at_sync, 60)

    def test_update_first_assign_later_also_promotes(self):
        """投递次序：`order.update`（建单支）先到也能转正——回填在两支的汇合处。"""
        row = self._claiming("UP-31")
        self._apply(
            {
                "idempotency_key": "syn-update-31",
                "kind": "order.update",
                "target_ref": "UP-31",
                "occurred_at": "2026-10-25T10:00:00+00:00",
                "payload": hall_payload("UP-31"),
            }
        )
        self._apply(
            {
                "idempotency_key": "syn-assign-31",
                "kind": "order.assign",
                "target_ref": "UP-31",
                "occurred_at": "2026-10-25T10:01:00+00:00",
                "payload": hall_payload("UP-31"),
            }
        )
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.PROMOTED)
        self.assertIsNotNone(fresh.promoted_order_id)

    def test_promotion_only_takes_claiming_rows(self):
        """⛔ 只认 claiming：listed 镜像行（普通挂单）收到 order.assign 不转正。"""
        row = self._mirror("UP-32")
        self._apply(
            {
                "idempotency_key": "syn-assign-32",
                "kind": "order.assign",
                "target_ref": "UP-32",
                "occurred_at": "2026-10-25T10:00:00+00:00",
                "payload": hall_payload("UP-32"),
            }
        )
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(fresh.promoted_order_id)

    def test_promoted_order_lands_at_the_display_price(self):
        """⭐ P1：90% ⇒ 正式单的发单价 = 上家价 50 × 90% = **45.00**。

        判据打在**转正后的订单行**上（不是镜像行）：打手照大厅里看到的 45 点进来，
        结算也必须按 45——两边差一成是这条线最贵的一种错。
        """
        from apps.business.models import PeerUpstreamBinding

        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_price_percent=90
        )
        row = self._claiming("UP-34")
        outcome = self._apply(
            {
                "idempotency_key": "syn-assign-34",
                "kind": "order.assign",
                "target_ref": "UP-34",
                "occurred_at": "2026-10-25T10:00:00+00:00",
                "payload": hall_payload("UP-34"),
            }
        )
        self.assertEqual(outcome.applied, 1)
        fresh = PeerHallMirror.objects.get(pk=row.pk)
        self.assertEqual(fresh.status, PeerHallMirror.Status.PROMOTED)
        self.assertEqual(fresh.promoted_order.hall_price, Decimal("45.00"))

    def test_promoted_price_rounds_half_up_to_two_decimals(self):
        """33.33 × 90% = 29.997 ⇒ **30.00**（`ROUND_HALF_UP`，不是截断 / 银行家舍入）。"""
        from apps.business.models import PeerUpstreamBinding

        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_price_percent=90
        )
        self._claiming("UP-35", price="33.33")
        self._apply(
            {
                "idempotency_key": "syn-assign-35",
                "kind": "order.assign",
                "target_ref": "UP-35",
                "occurred_at": "2026-10-25T10:00:00+00:00",
                "payload": hall_payload("UP-35", price="33.33"),
            }
        )
        order = PeerHallMirror.objects.get(peer_order_no="UP-35").promoted_order
        self.assertEqual(order.hall_price, Decimal("30.00"))

    def test_a_promotion_commission_failure_is_skipped_not_500(self):
        """⭐ A5 / M-8：转正撞上提成配置坏 ⇒ 事件 **skip**（`promotion_failed_business`）、
        整笔回滚，⛔ 不是 500（发送侧按业务失败退避重投，不计自动停绑）。"""
        from unittest import mock

        from apps.business.commission import CommissionError

        self._claiming("UP-36")
        with mock.patch(
            "apps.business.performance.set_taker",
            side_effect=CommissionError("提成配置坏（测试注入）", 1000),
        ):
            outcome = self._apply(
                {
                    "idempotency_key": "syn-assign-36",
                    "kind": "order.assign",
                    "target_ref": "UP-36",
                    "occurred_at": "2026-10-25T10:00:00+00:00",
                    "payload": hall_payload("UP-36"),
                }
            )
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertEqual(outcome.skipped, 1, outcome)
        detail = outcome.skipped_details[0]
        self.assertEqual(detail["code"], "promotion_failed_business")
        # 整笔回滚：镜像行还停在 claiming、订单 / 链行 / 绩效行一行都没有。
        fresh = PeerHallMirror.objects.get(peer_order_no="UP-36")
        self.assertEqual(fresh.status, PeerHallMirror.Status.CLAIMING)
        self.assertIsNone(fresh.promoted_order_id)
        self.assertFalse(Order.objects.filter(source_order_no="UP-36").exists())
        self.assertEqual(PeerOrderLink.objects.filter(peer_order_no="UP-36").count(), 0)

    def test_the_promotion_succeeds_once_the_commission_is_fixed(self):
        """⭐ A5 的另一半（可重投）：配置修好（桩撤掉）后**同一条事件**重投成功。"""
        from unittest import mock

        from apps.business.commission import CommissionError

        self._claiming("UP-37")
        event = {
            "idempotency_key": "syn-assign-37",
            "kind": "order.assign",
            "target_ref": "UP-37",
            "occurred_at": "2026-10-25T10:00:00+00:00",
            "payload": hall_payload("UP-37"),
        }
        with mock.patch(
            "apps.business.performance.set_taker",
            side_effect=CommissionError("提成配置坏（测试注入）", 1000),
        ):
            failed = self._apply(event)
        self.assertEqual(failed.skipped, 1, failed)
        ok = self._apply(event)
        self.assertEqual(ok.applied, 1, ok)
        fresh = PeerHallMirror.objects.get(peer_order_no="UP-37")
        self.assertEqual(fresh.status, PeerHallMirror.Status.PROMOTED)
        self.assertIsNotNone(fresh.promoted_order_id)

    def test_a_promotion_without_any_claimer_is_skipped_value_error_path(self):
        """两列都空的 `claiming` 行（数据被手改）⇒ `ValueError` 也走同一出口：skip 非 500。"""
        row = self._mirror("UP-38")
        PeerHallMirror.objects.filter(pk=row.pk).update(
            status=PeerHallMirror.Status.CLAIMING,
            claiming_at=parse_now(),
        )
        outcome = self._apply(
            {
                "idempotency_key": "syn-assign-38",
                "kind": "order.assign",
                "target_ref": "UP-38",
                "occurred_at": "2026-10-25T10:00:00+00:00",
                "payload": hall_payload("UP-38"),
            }
        )
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertEqual(outcome.skipped, 1, outcome)
        self.assertEqual(outcome.skipped_details[0]["code"], "promotion_failed_business")
        self.assertFalse(Order.objects.filter(source_order_no="UP-38").exists())

    def test_a_promotion_price_difference_beyond_a_cent_writes_the_audit_row(self):
        """⭐ A6 / M-6：转正覆盖展示价、差 > 0.01 ⇒ 操作日志**恰好一行**（M-6 的欠账）。"""
        from apps.audit.models import OperationLog
        from apps.business.models import PeerUpstreamBinding

        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_price_percent=90
        )
        self._claiming("UP-39", price="50.00")
        outcome = self._apply(
            {
                "idempotency_key": "syn-assign-39",
                "kind": "order.assign",
                "target_ref": "UP-39",
                "occurred_at": "2026-10-25T10:00:00+00:00",
                "payload": hall_payload("UP-39"),
            }
        )
        self.assertEqual(outcome.applied, 1, outcome)
        order = PeerHallMirror.objects.get(peer_order_no="UP-39").promoted_order
        self.assertEqual(order.hall_price, Decimal("45.00"))   # 覆盖真的发生了
        rows = OperationLog.objects.filter(action="order.peer_promote_price_override")
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.order_no, order.order_no)
        self.assertEqual(row.field_name, "hall_price")
        self.assertEqual(row.from_value, "50.00")
        self.assertEqual(row.to_value, "45.00")
        self.assertEqual(row.log_type, 10)                     # order.* 同族
        self.assertEqual(row.actor_id, None)                   # 机器来源显式声明

    def test_an_equal_price_promotion_writes_no_audit_row(self):
        """对照：展示价 == 上家价（100%）⇒ 不覆盖、零审计行。"""
        from apps.audit.models import OperationLog

        self._claiming("UP-40", price="50.00")
        outcome = self._apply(
            {
                "idempotency_key": "syn-assign-40",
                "kind": "order.assign",
                "target_ref": "UP-40",
                "occurred_at": "2026-10-25T10:00:00+00:00",
                "payload": hall_payload("UP-40"),
            }
        )
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(
            OperationLog.objects.filter(
                action="order.peer_promote_price_override"
            ).count(),
            0,
        )

    def test_plain_peer_order_has_no_mirror_side_effects(self):
        """普通 peer 单（无镜像行）：回填查询 0 行、订单照常落库（既有链路零变化）。"""
        outcome = self._apply(
            {
                "idempotency_key": "syn-assign-33",
                "kind": "order.assign",
                "target_ref": "UP-33",
                "occurred_at": "2026-10-25T10:00:00+00:00",
                "payload": hall_payload("UP-33", game_name="镜像接收夹具游戏"),
            }
        )
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(PeerHallMirror.objects.count(), 0)
        self.assertTrue(Order.objects.filter(source_order_no="UP-33").exists())


class UpstreamClaimTests(InboundFixture):
    """上家收 `hall.claim`（spec §4.2）：机器接手 + 既有 order.assign + 应答。"""

    def _upstream_side(self, *, status=OrderStatus.WAITING_GRAB, taker=None):
        """**上家视角**的夹具：上家自己的订单 + 收 claim 的 `PeerDownstreamBinding`。

        ⚠️ 同一 schema 里两族绑定并存（`PeerUpstreamBinding` / `PeerDownstreamBinding` 的
        pk 空间不相干），本夹具的 `binding` 是「我方为下家」、`down_side` 是「我方为上家」
        ——上家处理 claim 时认的是**后者**。
        """
        from django.contrib.auth import get_user_model

        from apps.business.models import PeerDownstreamBinding

        account = get_user_model().objects.create_user(
            username="syn-peer-hall-down-acct", password="pw-syn"
        )
        # ⭐ 机器接手照走节点门（`nodes_of(binding.account)`，照 `machine_claim_order`
        #    先例）⇒ 快路径夹具的绑定账号必须持接单节点；「无节点 ⇒ 回绝」由
        #    `test_nodeless_account_rolls_back_the_half_claim` 单独钉。
        #    ⚠️ `grant_nodes` 只把节点挂到**已有** Group 对应的 Role 上 ⇒ 先进组再授。
        from django.contrib.auth.models import Group

        from tests.p5_auth_helpers import grant_nodes

        worker_group, _ = Group.objects.get_or_create(name="代练员")
        account.groups.add(worker_group)
        grant_nodes(account, "grab_hall.take_order")
        down_side = PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url="https://peer-hall-down2.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        order = Order.objects.create(
            game=self.game,
            order_no="20261025000000080001",
            status=status,
            total_price=Decimal("100.00"),
            hall_price=Decimal("50.00"),
        )
        from apps.business.models import EmpType, Performance

        Performance.objects.create(
            order=order, user=taker, emp_type=EmpType.MAIN, temp_amount=Decimal("10.00")
        )
        return down_side, order

    def _claim_raw(self, ref):
        return {
            "idempotency_key": f"syn-claim-{ref}",
            "kind": "hall.claim",
            "target_ref": ref,
            "payload": {"claim_by": "syn-下家打手"},
        }

    def _share(self, down_side, order):
        """上家**真的把这单通过 `hall.throw` 投给这个下家绑定过**（⭐ Z20 的共享证据）。

        走真入队函数再把那一行记成 `done` ＋ 空 `reason`（⇔ worker 投递成功那一拍）。
        """
        from django.utils import timezone

        from apps.business import peer_dispatch

        made = peer_dispatch.enqueue_hall_event(order, kind=HALL_THROW_KIND)
        self.assertTrue(
            any(event.binding_id == str(down_side.pk) for event in made),
            "夹具自证：这条单没排给要测的那个下家绑定",
        )
        PeerSyncEvent.objects.filter(pk__in=[event.pk for event in made]).update(
            status=PeerSyncEvent.Status.DONE,
            reason="",
            processed_at=timezone.now(),
            next_attempt_at=None,
        )

    def _apply_upstream(self, down_side, *events):
        return apply_upstream_events(list(events), binding=down_side)

    def test_happy_path_assigns_to_binding_account_and_answers(self):
        """单还在大厅 ⇒ 机器接手给绑定账号 + 排 order.assign + hall.claim_ok。"""
        from apps.business.models import EmpType, Performance

        down_side, order = self._upstream_side()
        self._share(down_side, order)          # Z20：没被投递过的单 claim 会被回绝
        outcome = self._apply_upstream(down_side, self._claim_raw(order.order_no))
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(outcome.skipped, 0)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            down_side.account_id,
        )
        kinds = {
            row.kind: row for row in PeerSyncEvent.objects.filter(
                binding_id=str(down_side.pk)
            )
        }
        self.assertIn("order.assign", kinds)
        self.assertIn(HALL_CLAIM_OK_KIND, kinds)
        assign = kinds["order.assign"]
        self.assertEqual(assign.target_ref, order.order_no)
        self.assertEqual(assign.payload["order_no"], order.order_no)

    def test_claiming_when_not_in_hall_is_refused(self):
        """单不在大厅（UNASSIGNED）⇒ hall.claim_refused，订单一个字不改。"""
        down_side, order = self._upstream_side(status=OrderStatus.UNASSIGNED)
        outcome = self._apply_upstream(down_side, self._claim_raw(order.order_no))
        self.assertEqual(outcome.applied, 1)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        rows = PeerSyncEvent.objects.filter(binding_id=str(down_side.pk))
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().kind, HALL_CLAIM_REFUSED_KIND)

    def test_claim_of_unknown_order_is_refused(self):
        down_side, _order = self._upstream_side()
        outcome = self._apply_upstream(down_side, self._claim_raw("UP-NOT-EXIST"))
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(
            PeerSyncEvent.objects.get().kind, HALL_CLAIM_REFUSED_KIND
        )

    def test_a_peer_claim_does_not_resolve_to_an_upstream_hall_shadow(self):
        """⭐ 评审 R-2（2026-10-29）：peer 事件按单号撞上**上家镜像隐藏单** ⇒ 不解析到它。

        上家镜像隐藏单的 `source_order_no` 是鸡蛋铺**内部**数字单号，与 peer 解析键
        （`order_no` / `source_order_no`）同列对拍、理论可撞值；而它**永远不是** peer
        事件的合法目标（身份由 `upstream_hall_card` 那张卡承载，upstream 线管）⇒
        解析口排它（与 `hall_card` 同一道闸，peer 线 2026-10-26 拍板的同形收紧）。
        判别点 = 回绝的**永久 / 可重试**：撞上隐藏单若被解析，会走「从未投递」的
        可重试回绝；正确行为是「已不在对方大厅」的**永久**回绝（下家据此下架那张卡）。
        失败方向 = 摘掉解析口的 `.exclude(upstream_hall_card__isnull=False)` ⇒ 本用例红。
        """
        from datetime import datetime, timezone

        from django.contrib.auth import get_user_model

        from apps.business.models import JidanpuUpstreamHallOrder, PeerDownstreamBinding

        internal_no = "33160000000000002001"
        mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url="https://jidanpu-up-r2.test.invalid",
            upstream_order_id=internal_no,
            game=self.game,
            first_seen_at=datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc),
        )
        shadow = Order.objects.create(
            order_no="20261028000000000042",
            game=self.game,
            status=OrderStatus.WAITING_GRAB,
            total_price=Decimal("100.00"),
            source_order_no=internal_no,
            upstream_hall_card=mirror,
        )
        account = get_user_model().objects.create_user(
            username="syn-peer-hall-r2-acct", password="pw-syn"
        )
        down_side = PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url="https://peer-hall-down-r2.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        outcome = self._apply_upstream(down_side, self._claim_raw(internal_no))
        self.assertEqual(outcome.applied, 1)
        refused = PeerSyncEvent.objects.get(
            binding_id=str(down_side.pk), kind=HALL_CLAIM_REFUSED_KIND
        )
        # ⭐ 永久回绝那句（⛔ 不是「从未投递」那句——解析到隐藏单才会走那一支）。
        self.assertEqual(refused.payload.get("reason"), CLAIM_REFUSED_NOTE)
        # 隐藏单一个字不改。
        shadow.refresh_from_db()
        self.assertEqual(shadow.status, OrderStatus.WAITING_GRAB)

    def test_nodeless_account_rolls_back_the_half_claim(self):
        """绑定账号没有接单节点 ⇒ 回绝；**绩效条件更新的半截行必须消失**（E-0022）。

        `claim_order` 的绩效条件更新在状态机判定**之前**——节点门拒绝时那半截行
        已经写下去，靠本支的内层 savepoint 回滚。这条用例钉的就是它。
        ⚠️ 夹具的绑定账号**持**接手节点（快路径要用）⇒ 本条自建一个无节点的绑定。
        """
        from django.contrib.auth import get_user_model

        from apps.business.models import EmpType, Performance, PeerDownstreamBinding

        account = get_user_model().objects.create_user(
            username="syn-peer-hall-nodeless", password="pw-syn"
        )
        down_side = PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url="https://peer-hall-down3.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        _unused, order = self._upstream_side()
        # ⭐ Z20：先让它看起来「真的投递过」——否则本条的拒绝会来自来源校验，
        #    节点门那条路（半截绩效行回滚）就静默不被覆盖了。
        self._share(down_side, order)
        outcome = self._apply_upstream(down_side, self._claim_raw(order.order_no))
        self.assertEqual(outcome.applied, 1)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )
        self.assertEqual(
            PeerSyncEvent.objects.get(
                binding_id=str(down_side.pk), kind=HALL_CLAIM_REFUSED_KIND
            ).kind,
            HALL_CLAIM_REFUSED_KIND,
        )
        # ⛔ 没接手成功 ⇒ 一条 claim_ok 都不许排。
        self.assertFalse(
            PeerSyncEvent.objects.filter(
                binding_id=str(down_side.pk), kind=HALL_CLAIM_OK_KIND
            ).exists()
        )

class UpstreamClaimReplayTests(InboundFixture):
    """⭐ A9 / L-7（2026-10-25 审计批）：上家收 `hall.claim` 的**入站幂等**。

    夹具与 `UpstreamClaimTests` 同形（上家视角：`PeerDownstreamBinding` + 原生在厅单），
    刻意**拷贝不继承**——继承会让既有用例整组重跑一遍（unittest 连父类用例一起收）。
    """

    def _upstream_side(self, *, status=OrderStatus.WAITING_GRAB, taker=None):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Group

        from apps.business.models import EmpType, Performance, PeerDownstreamBinding
        from tests.p5_auth_helpers import grant_nodes

        account = get_user_model().objects.create_user(
            username="syn-peer-hall-replay-acct", password="pw-syn"
        )
        worker_group, _ = Group.objects.get_or_create(name="代练员")
        account.groups.add(worker_group)
        grant_nodes(account, "grab_hall.take_order")
        down_side = PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url="https://peer-hall-replay.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        order = Order.objects.create(
            game=self.game,
            order_no="20261025000000080002",
            status=status,
            total_price=Decimal("100.00"),
            hall_price=Decimal("50.00"),
        )
        Performance.objects.create(
            order=order, user=taker, emp_type=EmpType.MAIN,
            temp_amount=Decimal("10.00"),
        )
        return down_side, order

    def _claim_raw(self, ref):
        return {
            "idempotency_key": f"syn-replay-claim-{ref}",
            "kind": "hall.claim",
            "target_ref": ref,
            "payload": {"claim_by": "syn-下家打手"},
        }

    def _share(self, down_side, order):
        """上家**真的把这单投给过这个下家**（Z20 共享证据；照 `UpstreamClaimTests._share`）。"""
        from django.utils import timezone

        from apps.business import peer_dispatch

        made = peer_dispatch.enqueue_hall_event(order, kind=HALL_THROW_KIND)
        self.assertTrue(
            any(event.binding_id == str(down_side.pk) for event in made),
            "夹具自证：这条单没排给要测的那个下家绑定",
        )
        PeerSyncEvent.objects.filter(pk__in=[event.pk for event in made]).update(
            status=PeerSyncEvent.Status.DONE,
            reason="",
            processed_at=timezone.now(),
            next_attempt_at=None,
        )

    def test_a_replayed_claim_after_a_successful_one_is_answered_ok_not_refused(self):
        """受理成功后的**重放** ⇒ 回 `claim_ok`、⛔ 不按「已不在大厅」回永久拒绝。

        没有这一闸的重放序（L-7 现场）：应答在途丢了 → 下游重投同一请求 → 上家按
        「单已不在大厅」重评出一条**永久拒绝** → 下游把一张已受理的卡下架，
        随后迟到的 `order.assign` 落无主单。
        """
        from apps.business.models import EmpType, Performance

        down_side, order = self._upstream_side()
        self._share(down_side, order)
        first = apply_upstream_events(
            [self._claim_raw(order.order_no)], binding=down_side
        )
        self.assertEqual(first.applied, 1)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        # ── 重放：同一条 claim 再来一次（应答在途丢失的那一拍）──
        replay = apply_upstream_events(
            [self._claim_raw(order.order_no)], binding=down_side
        )
        self.assertEqual(replay.applied, 1, replay)
        rows = PeerSyncEvent.objects.filter(binding_id=str(down_side.pk))
        kinds = sorted(row.kind for row in rows)
        self.assertNotIn(HALL_CLAIM_REFUSED_KIND, kinds, kinds)
        self.assertIn(HALL_CLAIM_OK_KIND, kinds)
        # ⛔ 不重复处理：一次接手、一条 MAIN 行（接手人仍是绑定账号）、单没被二次改写。
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).count(), 1
        )
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            down_side.account_id,
        )

    def test_a_claim_for_an_order_taken_by_someone_else_is_still_refused(self):
        """判重的边界：MAIN 接手人是**别人** ⇒ 不是这条绑定的已受理重放 ⇒ 照旧永久拒绝。"""
        from django.contrib.auth import get_user_model

        other = get_user_model().objects.create_user(
            username="syn-peer-hall-replay-other", password="pw-syn"
        )
        down_side, order = self._upstream_side(taker=other)
        outcome = apply_upstream_events(
            [self._claim_raw(order.order_no)], binding=down_side
        )
        self.assertEqual(outcome.applied, 1)
        rows = PeerSyncEvent.objects.filter(binding_id=str(down_side.pk))
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().kind, HALL_CLAIM_REFUSED_KIND)



class HallShadowOrderTests(InboundFixture):
    """⭐ 2026-10-26 业务方拍板：镜像卡派生的**隐藏单**（`Order.hall_card`）。

    判据（每条都能单独弄红）：
    ① 卡落成 `listed` ⇒ 同事务建一张 `WAITING_GRAB` 的隐藏单（状态 / 展示价 / 来源号 / 游戏）；
    ② 建单后走**既有** `queue_hall_launch` ⇒ 只有 `hall_sync_enabled=True` 的绑定收到 `member.launch`；
    ③ 重复推送同一张卡 ⇒ 不建第二张；卡 `gone` 后又回到厅 ⇒ **同一张**单复活（不新建）；
    ④ 卡 `gone` ⇒ 隐藏单落 `REFUNDED`，且已收到它的下家排 `member.withdraw`。
    """

    def _jidanpu_binding(self, username, url, *, hall_sync_enabled=True):
        from django.contrib.auth import get_user_model

        from apps.business.models import JidanpuMemberBinding
        from tests.p5_auth_helpers import grant_games

        user = get_user_model().objects.create_user(username=username, password="pw-syn")
        grant_games(user, self.game)
        return JidanpuMemberBinding.objects.create(
            user=user,
            jidanpu_base_url=url,
            account=username,
            password="pw-syn",
            enabled=True,
            hall_sync_enabled=hall_sync_enabled,
        )

    def _launch_events(self, binding, order_no):
        from apps.business.jidanpu_member import KIND_LAUNCH
        from apps.business.models import JidanpuSyncEvent

        return JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_LAUNCH, target_ref=order_no,
            payload__member_binding_id=binding.pk,
        )

    def test_a_throw_lands_a_hidden_order_with_the_display_price(self):
        """① 卡进厅 ⇒ 隐藏单：状态 / 展示价（90% 档）/ 来源号 / 游戏全部按卡片落。"""
        from apps.business.models import PeerUpstreamBinding

        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_price_percent=90
        )
        self._apply(hall_event("UP-SH-1", price="50.00"))
        row = PeerHallMirror.objects.get(peer_order_no="UP-SH-1")
        order = Order.objects.get(hall_card=row)
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(order.game_id, self.game.pk)
        self.assertEqual(order.source_order_no, "UP-SH-1")
        # 上家发单价 50 × 本绑定 90% = **45.00**（与大厅里看到的价一致，⛔ 不落 50）。
        self.assertEqual(order.hall_price, Decimal("45.00"))
        self.assertEqual(order.project_text, "syn-项目")
        self.assertIsNone(order.server_id)
        self.assertIsNone(order.project_id)
        self.assertEqual(len(order.order_no), 20)
        self.assertTrue(order.order_no.isdigit())

    def test_a_card_without_a_price_leaves_the_hidden_order_price_empty(self):
        """无价卡 ⇒ 隐藏单 `hall_price` 为 NULL（⛔ 不补 0.00，E-0021 同族）。"""
        self._apply(hall_event("UP-SH-2", price=None))
        row = PeerHallMirror.objects.get(peer_order_no="UP-SH-2")
        order = Order.objects.get(hall_card=row)
        self.assertIsNone(order.hall_price)

    def test_launch_only_reaches_bindings_with_hall_sync_enabled(self):
        """② 建单后调既有 `queue_hall_launch` ⇒ 开关关的绑定一条都不排。"""
        synced = self._jidanpu_binding(
            "syn-shadow-synced", "https://syn-shadow-synced.test.invalid"
        )
        off = self._jidanpu_binding(
            "syn-shadow-off", "https://syn-shadow-off.test.invalid",
            hall_sync_enabled=False,
        )
        self._apply(hall_event("UP-SH-3", price="50.00"))
        row = PeerHallMirror.objects.get(peer_order_no="UP-SH-3")
        order = Order.objects.get(hall_card=row)
        self.assertEqual(self._launch_events(synced, order.order_no).count(), 1)
        self.assertEqual(self._launch_events(off, order.order_no).count(), 0)

    def test_a_duplicate_throw_does_not_create_a_second_order(self):
        """③ 重复推送同一张卡 ⇒ 只有一张隐藏单（同卡只能有一张）。"""
        self._apply(hall_event("UP-SH-4", price="50.00"))
        self._apply(hall_event("UP-SH-4", price="50.00"))
        row = PeerHallMirror.objects.get(peer_order_no="UP-SH-4")
        self.assertEqual(Order.objects.filter(hall_card=row).count(), 1)

    def test_a_take_refunds_the_hidden_order_and_withdraws_from_the_shop(self):
        """④ 卡 `gone` ⇒ 隐藏单 `REFUNDED` + 已收到它的下家排 `member.withdraw`。"""
        from apps.business.jidanpu_member import KIND_LAUNCH, KIND_WITHDRAW
        from apps.business.models import JidanpuSyncEvent

        synced = self._jidanpu_binding(
            "syn-shadow-refund", "https://syn-shadow-refund.test.invalid"
        )
        self._apply(hall_event("UP-SH-5", price="50.00"))
        row = PeerHallMirror.objects.get(peer_order_no="UP-SH-5")
        order = Order.objects.get(hall_card=row)
        # 让 `launch` 跑完（生产形状：先落单进对方大厅，之后才被撤下）。
        self._launch_events(synced, order.order_no).update(
            status=JidanpuSyncEvent.Status.DONE, reason=""
        )
        self._apply(hall_event("UP-SH-5", kind=HALL_TAKE_KIND))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        # 仍然是隐藏单（`hall_card` 不清）⇒ 退掉的派生单不在列表里冒充真退单。
        self.assertEqual(order.hall_card_id, row.pk)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind=KIND_WITHDRAW, target_ref=order.order_no,
                payload__member_binding_id=synced.pk,
            ).count(),
            1,
        )
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind=KIND_LAUNCH, target_ref=order.order_no,
                payload__member_binding_id=synced.pk,
            ).count(),
            1,
        )

    def test_a_soft_deleted_hidden_order_is_revived_not_duplicated(self):
        """⭐ 评审 Important 5：隐藏单被软删后再落卡 ⇒ **复活同一张**，⛔ 不撞 0099 的唯一索引。

        `ensure_for_card` 原先用默认 manager 的 `get_or_create(hall_card=…)`：软删的行对它
        不可见 ⇒ 它去**新建**一张，而 `Order.hall_card` 是 OneToOne（0099 的唯一索引）
        ⇒ `IntegrityError` 500。修法 = `all_objects` 查回 + 复活（同一条 1:1 口径）。
        """
        from django.utils import timezone

        self._apply(hall_event("UP-SH-7", price="50.00"))
        row = PeerHallMirror.objects.get(peer_order_no="UP-SH-7")
        order = Order.objects.get(hall_card=row)
        first_pk = order.pk
        Order.all_objects.filter(pk=order.pk).update(
            deleted_at=timezone.now(), deleted_by="syn-shadow"
        )
        # 卡仍在厅、再来一次 throw（生产形状：拉取刷新 / 重复推送）⇒ 不许 500。
        self._apply(hall_event("UP-SH-7", price="50.00"))
        revived = Order.all_objects.get(hall_card=row)
        self.assertEqual(revived.pk, first_pk, "软删之后新建了第二张隐藏单")
        self.assertIsNone(revived.deleted_at)
        self.assertEqual(revived.deleted_by, "")
        self.assertEqual(revived.status, OrderStatus.WAITING_GRAB)

    def test_a_resurrected_card_revives_the_same_hidden_order(self):
        """③ 另一半：`gone` 后又见到（复活）⇒ **同一张**隐藏单回 `WAITING_GRAB` + 重新抛。"""
        from apps.business.jidanpu_member import KIND_LAUNCH
        from apps.business.models import JidanpuSyncEvent

        synced = self._jidanpu_binding(
            "syn-shadow-revive", "https://syn-shadow-revive.test.invalid"
        )
        self._apply(hall_event("UP-SH-6", price="50.00"))
        row = PeerHallMirror.objects.get(peer_order_no="UP-SH-6")
        order = Order.objects.get(hall_card=row)
        first_pk = order.pk
        self._launch_events(synced, order.order_no).update(
            status=JidanpuSyncEvent.Status.DONE, reason=""
        )
        self._apply(hall_event("UP-SH-6", kind=HALL_TAKE_KIND))
        # 摘牌发生在**这条 throw 之前**（真实时序）：`gone_at` 早于事件时刻才允许复活
        # （A3 / M-3 的迟到 throw 闸；本用例要钉的是复活之后隐藏单怎么走）。
        PeerHallMirror.objects.filter(pk=row.pk).update(gone_at=parse_now())
        self._apply(hall_event("UP-SH-6", price="50.00"))
        order.refresh_from_db()
        self.assertEqual(order.pk, first_pk, "复活时新建了第二张隐藏单")
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind=KIND_LAUNCH, target_ref=order.order_no,
                payload__member_binding_id=synced.pk,
                status=JidanpuSyncEvent.Status.PENDING,
            ).count(),
            1,
            "复活后没有重新抛给下家大厅",
        )

    def test_a_resurrected_card_refreshes_the_hidden_order_content(self):
        """⭐ N4（R3-M5）：卡 `gone` 后带着新载荷回来 ⇒ 隐藏单的**内容列**也要刷成新的。

        判据分两层（计划 N4 测试网第 1 条）：
        ① 隐藏单 **DB 行** = 新载荷（消费端 `_consume_launch` 按单号**现查 DB**，所以 DB 就是
           重抛内容的真判据；实例回填只是同一事务内后续代码的配套）；
        ② 接上消费端桩：`member.launch` 里 `add_order` 实际收到的项目 / 备注 / 联系方式 /
           内容 / 游戏 = 新值（防「DB 改了、消费端却拼了旧缓存」）。
        """
        from apps.business import jidanpu_member
        from apps.business.models import JidanpuSyncEvent
        from tests.test_jidanpu_member_consume import CLIENT, _Upstream, _stub

        old_payload = hall_payload("UP-SH-N4", price="50.00")
        old_payload.update({
            "remark": "N4-旧备注",
            "content": "N4-旧需求",
            "project_text": "N4-旧项目",
            "customer_phone": "13800000001",
            "customer_qq": "10001",
        })
        new_payload = hall_payload("UP-SH-N4", price="70.00", game_name="N4-新游戏")
        new_payload.update({
            "remark": "N4-新备注",
            "content": "N4-新需求",
            "project_text": "N4-新项目",
            "customer_phone": "13900000002",
            "customer_qq": "20002",
        })
        from apps.business.jidanpu_member import KIND_LAUNCH
        from tests.p5_auth_helpers import grant_games

        synced = self._jidanpu_binding(
            "syn-shadow-n4-content",
            "https://syn-shadow-n4-content.test.invalid",
        )
        # 新载荷会把卡换到一款新游戏；下家绑定要**先**拿到那款游戏的权限，否则复活后的
        # `queue_hall_launch` 会在游戏范围闸上一条事件都不排（那样测的就不是内容了）。
        new_game = Game.objects.create(name="N4-新游戏")
        grant_games(synced.user, new_game)
        self._apply(hall_event("UP-SH-N4", payload=old_payload))
        card = PeerHallMirror.objects.get(peer_order_no="UP-SH-N4")
        order = Order.objects.get(hall_card=card)
        self._launch_events(synced, order.order_no).update(
            status=JidanpuSyncEvent.Status.DONE, reason=""
        )
        self._apply(hall_event("UP-SH-N4", kind=HALL_TAKE_KIND))
        PeerHallMirror.objects.filter(pk=card.pk).update(gone_at=parse_now())
        self._apply(hall_event("UP-SH-N4", payload=new_payload))
        order.refresh_from_db()
        card.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(order.game_id, card.game_id)
        self.assertEqual(card.game.name, "N4-新游戏")
        self.assertEqual(order.source_order_no, "UP-SH-N4")
        self.assertEqual(order.remark, "N4-新备注")
        self.assertEqual(order.content, "N4-新需求")
        self.assertEqual(order.project_text, "N4-新项目")
        self.assertEqual(order.customer_phone, "13900000002")
        self.assertEqual(order.customer_qq, "20002")

        # 消费端桩：pending 的 `member.launch` 里，录单表单必须是**新**内容。
        event = JidanpuSyncEvent.objects.get(
            direction="out", kind=jidanpu_member.KIND_LAUNCH,
            target_ref=order.order_no,
            payload__member_binding_id=synced.pk,
            status=JidanpuSyncEvent.Status.PENDING,
        )
        withdraw = JidanpuSyncEvent.objects.get(
            direction="out", kind=jidanpu_member.KIND_WITHDRAW,
            target_ref=order.order_no,
            payload__member_binding_id=synced.pk,
            status=JidanpuSyncEvent.Status.PENDING,
        )
        up = _Upstream(
            source_order_no="UP-SH-N4",
            lookup={
                "UP-SH-N4": {"orderId": "JID-N4", "orderIdView": "UP-SH-N4"},
                "JID-N4": {"orderId": "JID-N4", "isThrowOrder": "Y"},
            },
        )
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "deferred")
            self.assertEqual(jidanpu_member.consume_event(withdraw, client=CLIENT), "skipped")
            self.assertEqual(up.calls, [])
            self.assertEqual(
                jidanpu_member.consume_event(event, client=CLIENT), "done"
            )
        sent = dict(up.calls)["add_order"]
        form = sent["form_payload"]
        self.assertEqual(form["project"], "N4-新项目")
        self.assertEqual(form["remark"], "N4-新备注")
        self.assertEqual(form["dlcontent"], "N4-新需求")
        self.assertEqual(form["moblie"], "13900000002")
        self.assertEqual(form["qq"], "20002")
        self.assertEqual(sent["top_level"]["gameName"], "N4-新游戏")

    def test_non_string_contact_fields_are_dropped_not_500(self):
        """⭐ N4 类型闸（Minor 全收）：`customer_phone` / `customer_qq` / `project_text`
        收到 dict / list ⇒ 按「没带该字段」落空串、事件其余字段照常应用（⛔ 不 500）。

        ⚠️ 走**拉取行**这条真实入口：推送路在 `_peer_value_shape_error` 早就把联系方式挡成
        `bad_shape`，只有拉取行今天不查这几列 ⇒ 直接落进 `ensure_for_card` 的裸 `.get(...)`。
        """
        from apps.business.peer_inbound import apply_hall_pull_rows

        payload = hall_payload("UP-SH-N4-TYPES", price="50.00")
        payload["customer_phone"] = {"bad": "shape"}
        payload["customer_qq"] = ["bad", "shape"]
        payload["project_text"] = {"bad": "shape"}
        outcome = apply_hall_pull_rows(
            self.binding,
            [{"target_ref": "UP-SH-N4-TYPES", "payload": payload}],
            now=parse_now(), round_started_at=parse_now(),
        )
        self.assertEqual(outcome["applied"], 1, outcome)
        self.assertEqual(outcome["failures"], 0, outcome)
        order = Order.objects.get(
            hall_card=PeerHallMirror.objects.get(peer_order_no="UP-SH-N4-TYPES")
        )
        self.assertEqual(order.customer_phone, "")
        self.assertEqual(order.customer_qq, "")
        self.assertEqual(order.project_text, "")
        # 阳性对照：同一事件的其余字段照常应用（否则「整条被跳过了」也会让上面三条成立）。
        self.assertEqual(order.remark, "syn-备注")
        self.assertEqual(order.content, "syn-需求")

    def test_a_resurrected_card_drops_non_string_contacts(self):
        """⭐ N4 类型闸的**复活支**：新载荷里联系方式 / 项目是容器 ⇒ 落空串，
        ⛔ 不清成旧值、⛔ 也不把容器硬塞进 `CharField`。

        ⚠️ 同样走拉取行（推送路的形状闸会把这条事件挡在落地之前，到不了复活支）。
        """
        from apps.business.peer_inbound import apply_hall_pull_rows

        old_payload = hall_payload("UP-SH-N4-REVIVE-TYPES", price="50.00")
        old_payload.update({
            "customer_phone": "13800000001",
            "customer_qq": "10001",
            "project_text": "N4-旧项目",
        })
        self._apply(hall_event("UP-SH-N4-REVIVE-TYPES", payload=old_payload))
        card = PeerHallMirror.objects.get(peer_order_no="UP-SH-N4-REVIVE-TYPES")
        order = Order.objects.get(hall_card=card)
        self._apply(
            hall_event("UP-SH-N4-REVIVE-TYPES", kind=HALL_TAKE_KIND)
        )
        PeerHallMirror.objects.filter(pk=card.pk).update(gone_at=parse_now())
        new_payload = hall_payload("UP-SH-N4-REVIVE-TYPES", price="50.00")
        new_payload.update({
            "customer_phone": {"bad": "shape"},
            "customer_qq": ["bad"],
            "project_text": ["bad"],
        })
        outcome = apply_hall_pull_rows(
            self.binding,
            [{"target_ref": "UP-SH-N4-REVIVE-TYPES", "payload": new_payload}],
            now=parse_now(), round_started_at=parse_now(),
        )
        self.assertEqual(outcome["applied"], 1, outcome)
        self.assertEqual(outcome["failures"], 0, outcome)
        order.refresh_from_db()
        card.refresh_from_db()
        self.assertEqual(card.status, PeerHallMirror.Status.LISTED)
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(order.customer_phone, "")
        self.assertEqual(order.customer_qq, "")
        self.assertEqual(order.project_text, "")
        self.assertEqual(order.remark, "syn-备注")

    def test_a_reprice_event_refreshes_the_hidden_order_price(self):
        """⭐ N5（R3-M6）：`hall.reprice` 到达 ⇒ 隐藏单 `hall_price` = 新展示价，
        `updated_at` 真的推进，已推过鸡蛋铺下家的那张单排一条 `member.price`。

        判据打 **DB 行**（消费端读 DB）；`updated_at` 是承重的——`update()` 不触发
        `auto_now`，漏掉显式列会让时间停在旧值。
        """
        from datetime import timedelta

        from django.utils import timezone as dj_timezone

        from apps.business import jidanpu_member
        from apps.business.models import JidanpuMemberOrderLink, JidanpuSyncEvent

        synced = self._jidanpu_binding(
            "syn-shadow-reprice", "https://syn-shadow-reprice.test.invalid"
        )
        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_price_percent=90
        )
        self._apply(hall_event("UP-SH-RP", price="50.00"))
        card = PeerHallMirror.objects.get(peer_order_no="UP-SH-RP")
        order = Order.objects.get(hall_card=card)
        self.assertEqual(order.hall_price, Decimal("45.00"))
        JidanpuMemberOrderLink.objects.create(
            binding=synced, order=order, jidanpu_order_id="JID-RP", in_hall=True
        )
        old = dj_timezone.now() - timedelta(days=1)
        Order.objects.filter(pk=order.pk).update(updated_at=old)

        self._apply(
            hall_event("UP-SH-RP", kind=HALL_REPRICE_KIND, price="60.00")
        )
        order.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("54.00"), "隐藏单价没跟上改价")
        self.assertGreater(order.updated_at, old, "`updated_at` 没被显式写")
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind=jidanpu_member.KIND_PRICE,
                target_ref=order.order_no,
                payload__member_binding_id=synced.pk,
            ).count(),
            1,
            "已推过鸡蛋铺下家的隐藏单没有排改价事件",
        )

    def test_a_reprice_without_a_price_never_erases_the_hidden_order_price(self):
        """⭐ N5：`display_price(card)` 为 `None` ⇒ helper no-op（⛔ 不把已有的价擦成 NULL，
        E-0021 同族）。"""
        from datetime import timedelta

        from django.utils import timezone as dj_timezone

        from apps.business import hall_shadow

        self._apply(hall_event("UP-SH-RP-NONE", price=None))
        card = PeerHallMirror.objects.get(peer_order_no="UP-SH-RP-NONE")
        order = Order.objects.get(hall_card=card)
        self.assertIsNone(order.hall_price)
        old = dj_timezone.now() - timedelta(days=1)
        Order.objects.filter(pk=order.pk).update(
            hall_price=Decimal("12.34"), updated_at=old
        )
        card.remote_hall_price = None
        card.save(update_fields=["remote_hall_price"])
        hall_shadow.sync_hidden_order_price(card, now=parse_now())
        order.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("12.34"))
        self.assertEqual(order.updated_at, old)

    def test_the_hidden_price_helper_is_a_no_op_for_the_same_price(self):
        """⭐ N5：展示价与隐藏单现价相等 ⇒ 不刷 `updated_at`、不排改价事件。"""
        from datetime import timedelta

        from django.utils import timezone as dj_timezone

        from apps.business import hall_shadow
        from apps.business.models import JidanpuMemberOrderLink, JidanpuSyncEvent

        synced = self._jidanpu_binding(
            "syn-shadow-same-price", "https://syn-shadow-same-price.test.invalid"
        )
        self._apply(hall_event("UP-SH-SAME", price="50.00"))
        card = PeerHallMirror.objects.get(peer_order_no="UP-SH-SAME")
        order = Order.objects.get(hall_card=card)
        JidanpuMemberOrderLink.objects.create(
            binding=synced, order=order, jidanpu_order_id="JID-SAME", in_hall=True
        )
        old = dj_timezone.now() - timedelta(days=1)
        Order.objects.filter(pk=order.pk).update(updated_at=old)
        self.assertFalse(hall_shadow.sync_hidden_order_price(card, now=parse_now()))
        order.refresh_from_db()
        self.assertEqual(order.updated_at, old)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind="member.price").exists()
        )
