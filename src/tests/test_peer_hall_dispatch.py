"""D-0303 团团通大厅镜像：**上家侧**出方向的入队内核与触发点穷举（spec §2.1 / §2.2）。

判据分两层（照 `tests/test_peer_p3_triggers.py` 的两层形状）：

① **入队内核**（`enqueue_hall_event` / `enqueue_hall_claim` / `enqueue_hall_claim_reply`）：
   推给**全部** `state=ACTIVE` 且 `hall_push_enabled=True` 的下游绑定（⛔ 不是「taker 是
   绑定账号才推」——那是 `enqueue_order_event` 的判据，本线不能复用）；pending 去重键
   `(binding_role, binding_id, kind, target_ref)`、命中刷新载荷。
② **触发点穷举**（打真 HTTP + 断数据库行）：进 `WAITING_GRAB` 的三条路（`/throw-to-hall/`、
   通用 `/transition/` 目标 `WAITING_GRAB`、录单即抛——后者与另两条收敛在
   `apply_transition` 的 `hall_publisher is not None` 一支，用直接调漏斗钉）；离开
   `WAITING_GRAB` 的四条路（被接走 `/claim/`、撤回 `/withdraw/`、删单 `/delete/`、
   指派 `/assign/`）；在挂期间改价 `/hall-price/`。⚠️ 软删不经状态机 ⇒ `/delete/` 是
   `recycle.soft_delete_order` 里的**第二个**写点，单独钉。

⚠️ 夹具值全部合成值（`syn-*` / `*.test.invalid` / `测试*`）；断言打数据库行（E-0024）。
⚠️ 必须用 `TenantTestCase`（`apps.business` 只在 `TENANT_APPS`，照 peer 线先例）。
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerSyncEvent,
    Performance,
)
from apps.business.peer_dispatch import (
    enqueue_hall_claim,
    enqueue_hall_claim_reply,
    enqueue_hall_event,
)
from apps.business.peer_sync import (
    HALL_CLAIM_OK_KIND,
    HALL_CLAIM_REFUSED_KIND,
    HALL_REPRICE_KIND,
    HALL_TAKE_KIND,
    HALL_THROW_KIND,
    hash_token,
    new_token,
    order_payload,
)
from tests.p5_auth_helpers import grant_games, make_logged_in_client

ORDERS = "/api/business/orders"
HALL_CLAIM = "/api/business/hall/{no}/claim/"

#: 打真 HTTP 要过的端点门（照 `test_peer_p3_triggers` 的 NODES，别凭记忆缩）：
#: `order.send_to_grab_hall` ⇒ `/throw-to-hall/`、`order.change_status` ⇒ `/transition/` /
#: `/withdraw/`、`order.set_offer_price` + `order.submit_payout_price` ⇒ `/hall-price/`、
#: `order.assign_taker` ⇒ `/assign/`、`order.list` ⇒ 取数层、`grab_hall.take_order` ⇒ `/claim/`、
#: `order.delete` ⇒ `/delete/`（`apps/rbac/catalog.py` 的节点 23）。
NODES = (
    "order.change_status",
    "order.set_offer_price",
    "order.submit_payout_price",
    "order.assign_taker",
    "order.send_to_grab_hall",
    "order.list",
    "grab_hall.take_order",
    "order.delete",
)


def _no(n):
    """20 位订单号（`OrderViewSet.lookup_value_regex` 要求 URL 里必须 20 位）。"""
    return f"2026102500000000{n:04d}"


class HallDispatchFixture(TenantTestCase):
    """共用夹具：一款游戏、两条生效绑定（一条关了推送）+ 一条已解除绑定。"""

    def setUp(self):
        self.game = Game.objects.create(name="大厅镜像出方向夹具游戏")
        self.taker = self._account("syn-peer-hall-throw-taker")
        self.binding_on = self._bind("syn-peer-hall-throw-on")
        self.binding_muted = self._bind(
            "syn-peer-hall-throw-muted", hall_push_enabled=False
        )
        self.binding_revoked = self._bind(
            "syn-peer-hall-throw-revoked",
            state=PeerDownstreamBinding.State.REVOKED,
        )
        self.api = make_logged_in_client(
            username="syn-peer-hall-cs", groups=("客服",), nodes=NODES
        )

    # ── 账号 / 绑定 / 订单 ────────────────────────────────────────
    def _account(self, username):
        user = get_user_model().objects.create_user(username=username, password="pw-syn")
        StaffProfile.objects.create(
            user=user,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        grant_games(user, self.game)
        return user

    def _bind(self, username, *, hall_push_enabled=True, state=None):
        account = self._account(username)
        return PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url="https://peer-hall-down.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            hall_push_enabled=hall_push_enabled,
            **({"state": state} if state is not None else {}),
        )

    def _order(self, no, *, status=OrderStatus.UNASSIGNED, taker=None, price="50.00"):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            total_price=Decimal("100.00"),
            hall_price=Decimal(price),
        )
        Performance.objects.create(
            order=order, user=taker, emp_type=EmpType.MAIN, temp_amount=Decimal("10.00")
        )
        return order

    # ── 请求 ─────────────────────────────────────────────────────
    def _post_ok(self, url, payload):
        response = self.api.post(url, payload, content_type="application/json")
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body


class EnqueueHallEventTests(HallDispatchFixture):
    """第 ① 层：`enqueue_hall_event` 的广播面、开关面与 pending 去重。"""

    def test_broadcasts_to_every_active_enabled_binding(self):
        """只有**生效且开了推送**的绑定收得到：本夹具里恰好一条（关推送 / 已解除的各一条做对照）。"""
        order = self._order(_no(1))
        made = enqueue_hall_event(order, kind=HALL_THROW_KIND)
        self.assertEqual(len(made), 1)
        rows = list(PeerSyncEvent.objects.order_by("binding_id"))
        self.assertEqual(len(rows), 1)
        by_binding = {row.binding_id: row for row in rows}
        self.assertIn(str(self.binding_on.pk), by_binding)
        # ⛔ 关推送 / 已解除的绑定不得收到（两条判据分开钉，去掉哪条过滤哪条红）。
        self.assertNotIn(str(self.binding_muted.pk), by_binding)
        self.assertNotIn(str(self.binding_revoked.pk), by_binding)
        row = by_binding[str(self.binding_on.pk)]
        # 落库形状逐键（照 enqueue_order_event 的形状 + spec §2.1）。
        self.assertEqual(row.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(row.kind, HALL_THROW_KIND)
        self.assertEqual(row.binding_role, "downstream")
        self.assertEqual(row.direction, "out")
        self.assertEqual(row.target_type, "order")
        self.assertEqual(row.target_ref, order.order_no)
        # 载荷 = order_payload 白名单原样（含 game_name / hall_price）。
        self.assertEqual(row.payload, order_payload(order))
        self.assertEqual(row.payload["game_name"], self.game.name)
        self.assertEqual(row.payload["hall_price"], "50.00")

    def test_a_negative_price_card_is_not_thrown(self):
        """⭐ D-0306 推送路不变量：**负价卡不抛**（入队前闸，位置在 `_enqueue_hall_row`）。"""
        bad = self._order(_no(90), price="-5.00")
        self.assertEqual(enqueue_hall_event(bad, kind=HALL_THROW_KIND), [])
        self.assertEqual(PeerSyncEvent.objects.count(), 0, "负价卡竟然排了事件")
        # 阳性对照：正价照旧（否则「零条」可能只是广播面坏了）。
        good = self._order(_no(91), price="5.00")
        self.assertEqual(len(enqueue_hall_event(good, kind=HALL_THROW_KIND)), 1)

    def test_a_negative_reprice_is_not_enqueued(self):
        """⭐ D-0306：**负价 reprice 不传播**（入队侧；应用侧另有一道兜底闸）。"""
        bad = self._order(_no(92), price="-5.00")
        self.assertEqual(enqueue_hall_event(bad, kind=HALL_REPRICE_KIND), [])
        self.assertEqual(PeerSyncEvent.objects.count(), 0, "负价改价竟然排了事件")
        good = self._order(_no(93), price="70.00")
        self.assertEqual(len(enqueue_hall_event(good, kind=HALL_REPRICE_KIND)), 1)

    def test_muted_and_revoked_bindings_get_nothing(self):
        """单看两个被排除的绑定：`hall_push_enabled=False` 与 `REVOKED` 都一行不写。"""
        order = self._order(_no(2))
        enqueue_hall_event(order, kind=HALL_TAKE_KIND)
        self.assertFalse(
            PeerSyncEvent.objects.filter(
                binding_id=str(self.binding_muted.pk)
            ).exists()
        )
        self.assertFalse(
            PeerSyncEvent.objects.filter(
                binding_id=str(self.binding_revoked.pk)
            ).exists()
        )

    def test_pending_dedup_refreshes_payload_and_moment(self):
        """同一 `(绑定, kind, 单号)` 的 pending 只留一行，载荷刷成最新（「以最后更改的为主」）。"""
        order = self._order(_no(3))
        first = enqueue_hall_event(order, kind=HALL_THROW_KIND)
        order.hall_price = Decimal("66.00")
        order.save(update_fields=["hall_price", "updated_at"])
        second = enqueue_hall_event(order, kind=HALL_THROW_KIND)
        self.assertEqual(PeerSyncEvent.objects.count(), 1)
        self.assertEqual(second[0].pk, first[0].pk)
        row = PeerSyncEvent.objects.get()
        self.assertEqual(row.payload["hall_price"], "66.00")

    def test_different_kinds_are_separate_rows(self):
        """throw 与 take 是两件事：各占一行（kind 在去重键里）。"""
        order = self._order(_no(4))
        enqueue_hall_event(order, kind=HALL_THROW_KIND)
        enqueue_hall_event(order, kind=HALL_TAKE_KIND)
        self.assertEqual(PeerSyncEvent.objects.count(), 2)
        self.assertEqual(
            set(PeerSyncEvent.objects.values_list("kind", flat=True)),
            {HALL_THROW_KIND, HALL_TAKE_KIND},
        )

    def test_explicit_bindings_still_respect_the_switch(self):
        """显式传 `bindings=`（铺底命令那一路）也绕不过开关：关推送的绑定照样不写。"""
        order = self._order(_no(5))
        made = enqueue_hall_event(
            order, kind=HALL_THROW_KIND, bindings=[self.binding_muted]
        )
        self.assertEqual(made, [])
        self.assertEqual(PeerSyncEvent.objects.count(), 0)


class ThrowTriggerTests(HallDispatchFixture):
    """进入 `WAITING_GRAB` 的三条路（spec §2.2 穷举表的上半）。"""

    def test_throw_to_hall_endpoint_enqueues_hall_throw(self):
        """路 ①：`/throw-to-hall/`。"""
        order = self._order(_no(11))
        self._post_ok(f"{ORDERS}/{order.order_no}/throw-to-hall/", {})
        row = PeerSyncEvent.objects.get(kind=HALL_THROW_KIND)
        self.assertEqual(row.target_ref, order.order_no)
        self.assertEqual(row.binding_id, str(self.binding_on.pk))

    def test_transition_to_waiting_grab_enqueues_hall_throw(self):
        """路 ②：通用 `/transition/` 目标 `WAITING_GRAB`。"""
        order = self._order(_no(12))
        self._post_ok(
            f"{ORDERS}/{order.order_no}/transition/", {"target": "WAITING_GRAB"}
        )
        row = PeerSyncEvent.objects.get(kind=HALL_THROW_KIND)
        self.assertEqual(row.target_ref, order.order_no)

    def test_intake_style_direct_transition_enqueues_hall_throw(self):
        """路 ③：录单即抛单（`order_intake` 与另两条收敛在 `apply_transition` 的
        `hall_publisher is not None` 一支）——用直接调漏斗钉同一个收口。"""
        from apps.business.order_status import apply_transition

        order = self._order(_no(13))
        publisher = get_user_model().objects.get(username="syn-peer-hall-cs")
        apply_transition(
            order, OrderStatus.WAITING_GRAB,
            nodes=frozenset({"order.send_to_grab_hall"}),
            hall_publisher=publisher,
        )
        self.assertEqual(PeerSyncEvent.objects.filter(kind=HALL_THROW_KIND).count(), 1)

    def test_no_binding_means_no_event(self):
        """零绑定 ⇒ 一行都不写（打真端点，防止「挂钩挂错分支」的静默）。"""
        PeerDownstreamBinding.objects.all().delete()
        order = self._order(_no(14))
        self._post_ok(f"{ORDERS}/{order.order_no}/throw-to-hall/", {})
        self.assertEqual(PeerSyncEvent.objects.count(), 0)


class TakeTriggerTests(HallDispatchFixture):
    """离开 `WAITING_GRAB` 的四条路（spec §2.2 穷举表的下半）。"""

    def _thrown(self, no, *, taker=None):
        order = self._order(no, status=OrderStatus.WAITING_GRAB, taker=taker)
        return order

    def test_claim_success_enqueues_hall_take(self):
        """路 ①：被接走（内部大厅 `/hall/<no>/claim/`，打手真节点）。"""
        order = self._thrown(_no(21))
        # ⚠️ `make_logged_in_client` 自己建账号（同名二次 create 会撞唯一约束）。
        claim_api = make_logged_in_client(
            username="syn-peer-hall-claimer", groups=("代练员",), nodes=NODES,
        )
        response = claim_api.post(
            HALL_CLAIM.format(no=order.order_no), {}, content_type="application/json"
        )
        self.assertEqual(response.json()["code"], 0, response.json())
        row = PeerSyncEvent.objects.get(kind=HALL_TAKE_KIND)
        self.assertEqual(row.target_ref, order.order_no)

    def test_withdraw_enqueues_hall_take(self):
        """路 ②：撤回（`WAITING_GRAB → UNASSIGNED`，走 `/transition/`）。"""
        order = self._thrown(_no(22))
        self._post_ok(
            f"{ORDERS}/{order.order_no}/transition/", {"target": "UNASSIGNED"}
        )
        self.assertEqual(PeerSyncEvent.objects.filter(kind=HALL_TAKE_KIND).count(), 1)

    def test_soft_delete_enqueues_hall_take(self):
        """路 ③：删单（软删**不经状态机** ⇒ `recycle.soft_delete_order` 的第二个写点）。"""
        order = self._thrown(_no(23))
        self._post_ok(f"{ORDERS}/{order.order_no}/delete/", {"is_del_emp": "N"})
        row = PeerSyncEvent.objects.get(kind=HALL_TAKE_KIND)
        self.assertEqual(row.target_ref, order.order_no)

    def test_assign_from_hall_enqueues_hall_take(self):
        """路 ④：指派给绑定账号（`/assign/` 会把 `WAITING_GRAB` 推进 `IN_PROGRESS`）。

        ⭐ 这一条同时是 spec §4.2 的桥：上家把单派给绑定账号 ⇒ 下家看到摘牌
        （他的镜像行在 `claiming`、不受 take 影响）+ 既有 `order.assign` 推送。
        """
        order = self._thrown(_no(24))
        self._post_ok(
            f"{ORDERS}/{order.order_no}/assign/",
            {"user_id": self.binding_on.account_id},
        )
        kinds = set(PeerSyncEvent.objects.values_list("kind", flat=True))
        self.assertIn(HALL_TAKE_KIND, kinds)
        # 既有 order.assign 推送照旧出发（收件人 = 绑定账号）。
        self.assertIn("order.assign", kinds)

    def test_leave_reason_marks_only_the_assign_take(self):
        """⭐ 分流钉子（判定表 ①②）：/assign/ 的 take 顶层带 leave_reason="assigned"；
        /claim/ 受理的 take 不带——接收侧收单支只认前者。"""
        order = self._thrown(_no(25))
        self._post_ok(
            f"{ORDERS}/{order.order_no}/assign/",
            {"user_id": self.binding_on.account_id},
        )
        row = PeerSyncEvent.objects.get(kind=HALL_TAKE_KIND)
        self.assertEqual(row.meta.get("leave_reason"), "assigned")

        grabbed = self._thrown(_no(26))
        claim_api = make_logged_in_client(
            username="syn-peer-hall-split-claimer",
            groups=("代练员",),
            nodes=NODES,
        )
        response = claim_api.post(
            HALL_CLAIM.format(no=grabbed.order_no), {}, content_type="application/json"
        )
        self.assertEqual(response.json()["code"], 0, response.json())
        bare = PeerSyncEvent.objects.get(
            kind=HALL_TAKE_KIND, target_ref=grabbed.order_no
        )
        self.assertNotIn("leave_reason", bare.meta or {})


class RepriceTriggerTests(HallDispatchFixture):
    """在挂期间改发单价 ⇒ `hall.reprice`（spec §2.1 第三行）。"""

    def test_price_change_while_listed_enqueues_hall_reprice(self):
        order = self._order(_no(31), status=OrderStatus.WAITING_GRAB)
        self._post_ok(f"{ORDERS}/{order.order_no}/hall-price/", {"price": "88.00"})
        row = PeerSyncEvent.objects.get(kind=HALL_REPRICE_KIND)
        self.assertEqual(row.target_ref, order.order_no)
        self.assertEqual(row.payload["hall_price"], "88.00")

    def test_price_change_off_hall_enqueues_no_reprice(self):
        """不在厅（已被接走）⇒ 不发 reprice（那张单没有在挂镜像行可刷）。"""
        order = self._order(_no(32), status=OrderStatus.WAITING_GRAB)
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        perf.user = self.binding_on.account
        perf.save(update_fields=["user", "updated_at"])
        order.status = OrderStatus.IN_PROGRESS
        order.save(update_fields=["status", "updated_at"])
        self._post_ok(f"{ORDERS}/{order.order_no}/hall-price/", {"price": "88.00"})
        self.assertFalse(
            PeerSyncEvent.objects.filter(kind=HALL_REPRICE_KIND).exists()
        )


class ContentEditTriggerTests(HallDispatchFixture):
    """⭐ D-0377 的第五条 throw 路：**在挂期间编辑内容面字段** ⇒ 重发 `hall.throw`
    （刷新下家的镜像卡）；已被接走 ⇒ 只排 `order.update`、⛔ 不发 throw（防复活拉回大厅）。
    ⚠️ 走真 HTTP PATCH（编辑保存那道门的节点是 `order.add_edit` / `order.submit_edit`，
    基类 NODES 没带 ⇒ 本类自己开客户端）。"""

    def _editor(self):
        return make_logged_in_client(
            username="syn-peer-hall-editor",
            groups=("客服",),
            nodes=NODES + ("order.add_edit", "order.submit_edit"),
        )

    def _order(self, no, *, status=OrderStatus.UNASSIGNED, taker=None, price="50.00"):
        """编辑保存那条路有 `project_text` 必填闸 ⇒ 本类夹具单带上它（其余照基类）。"""
        order = super()._order(no, status=status, taker=taker, price=price)
        order.project_text = "大厅镜像编辑夹具项目"
        order.save(update_fields=["project_text"])
        return order

    def _bound_taker_on(self, order):
        """把 MAIN 行挂到那条「开推送」绑定的账号上（⇒ order.update 支可达）。"""
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        perf.user = self.binding_on.account
        perf.save(update_fields=["user", "updated_at"])

    def test_content_edit_while_listed_rethrows_hall_throw(self):
        """在挂（`WAITING_GRAB`）+ PATCH 改备注 ⇒ `hall.throw` 载荷带着**新备注**重发
        （pending 去重键命中刷新载荷；`order.update` 同事务一起排给绑定的下家）。"""
        order = self._order(_no(33), status=OrderStatus.WAITING_GRAB)
        self._bound_taker_on(order)
        response = self._editor().patch(
            f"{ORDERS}/{order.order_no}/",
            {"remark": "编辑改过的备注"},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0, response.content)
        order.refresh_from_db()
        self.assertEqual(order.remark, "编辑改过的备注")
        row = PeerSyncEvent.objects.get(kind=HALL_THROW_KIND)
        self.assertEqual(row.target_ref, order.order_no)
        self.assertEqual(row.payload["remark"], "编辑改过的备注")
        self.assertEqual(row.payload["hall_price"], "50.00")
        # 同一次编辑也把正式单的更新排给了开推送的下家绑定。
        update_row = PeerSyncEvent.objects.get(kind="order.update")
        self.assertEqual(update_row.binding_id, str(self.binding_on.pk))
        self.assertEqual(update_row.payload["remark"], "编辑改过的备注")

    def test_content_edit_off_hall_enqueues_update_but_no_throw(self):
        """已被接走（不在厅）⇒ `order.update` 照排（正式单跟着改）、⛔ 无 `hall.throw`
        （下家已接走的卡不许被 throw 复活拉回大厅）。"""
        order = self._order(_no(34), status=OrderStatus.WAITING_GRAB)
        self._bound_taker_on(order)
        order.status = OrderStatus.IN_PROGRESS
        order.save(update_fields=["status", "updated_at"])

        response = self._editor().patch(
            f"{ORDERS}/{order.order_no}/",
            {"remark": "离厅后的备注"},
            content_type="application/json",
        )

        self.assertEqual(response.json()["code"], 0, response.content)
        self.assertTrue(
            PeerSyncEvent.objects.filter(kind="order.update").exists(),
            "下家正式单没收到内容更新",
        )
        self.assertFalse(
            PeerSyncEvent.objects.filter(kind=HALL_THROW_KIND).exists(),
            "离厅单竟然被 throw 复活了",
        )

    def test_a_non_content_edit_enqueues_nothing(self):
        """触发面外字段（`game_account`，计划 §1 显式「不同步」）⇒ 零事件（闸的零开销穿透）。"""
        order = self._order(_no(35), status=OrderStatus.WAITING_GRAB)
        self._bound_taker_on(order)
        response = self._editor().patch(
            f"{ORDERS}/{order.order_no}/",
            {"game_account": "TT-ACC-EDIT"},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0, response.content)
        order.refresh_from_db()
        self.assertEqual(order.game_account, "TT-ACC-EDIT")
        self.assertEqual(PeerSyncEvent.objects.count(), 0)


class ThrowTakeRethrowSequenceTests(HallDispatchFixture):
    """⭐ M1（评审修复轮）：throw → take → 再抛 的三连序列，终态必须 = listed。

    病根：投递**最老优先**、pending 去重不刷 `created_at` ⇒ 老 throw 行载新载荷先投、
    take 后投 ⇒ 下家把一张实挂的单钉成 `gone`。修法：排 take 时**当场作废**同键的
    未投 throw（见 `enqueue_hall_event` 的 docstring）。
    """

    def setUp(self):
        super().setUp()
        from apps.business.models import PeerUpstreamBinding

        operator = get_user_model().objects.create_user(
            username="syn-peer-hall-seq-op", password="pw-syn"
        )
        self.up_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家庚",
            peer_base_url="https://peer-hall-seq.test.invalid",
            peer_account="syn-up-seq",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-hall-seq-local.test.invalid",
            bound_by=operator,
            # ⭐ P1：接收侧开关默认关 ⇒ 投递落地夹具显式打开。
            hall_sync_enabled=True,
        )

    def _deliver_to_downstream(self):
        """把全部 pending 的 hall 事件按**投递顺序**（created_at 最老优先）递给下家。"""
        from apps.business.peer_inbound import apply_events

        rows = list(
            PeerSyncEvent.objects.filter(
                kind__startswith="hall.", status=PeerSyncEvent.Status.PENDING
            ).order_by("created_at", "id")
        )
        return apply_events(
            [{
                "idempotency_key": row.idempotency_key,
                "kind": row.kind,
                "target_ref": row.target_ref,
                "occurred_at": (row.meta or {}).get("occurred_at"),
                "payload": row.payload,
            } for row in rows],
            binding=self.up_binding,
        )

    def test_rethrow_after_take_lands_listed_not_gone(self):
        from apps.business.models import PeerHallMirror

        order = self._order(_no(41))
        enqueue_hall_event(order, kind=HALL_THROW_KIND)   # ① 未投
        order.status = OrderStatus.IN_PROGRESS
        order.save(update_fields=["status", "updated_at"])
        enqueue_hall_event(order, kind=HALL_TAKE_KIND)    # ② 离厅：作废 ①
        order.status = OrderStatus.WAITING_GRAB
        order.save(update_fields=["status", "updated_at"])
        enqueue_hall_event(order, kind=HALL_THROW_KIND)   # ③ 再抛（去重回填？⛔ 不——①已作废 ⇒ 新行）

        # 台账：老 throw 行 = done + 人话原因；pending = take + 新 throw（按序）。
        rows = list(PeerSyncEvent.objects.order_by("created_at", "id"))
        self.assertEqual(
            [(row.kind, row.status) for row in rows],
            [
                (HALL_THROW_KIND, PeerSyncEvent.Status.DONE),   # ① 作废
                (HALL_TAKE_KIND, PeerSyncEvent.Status.PENDING),  # ②
                (HALL_THROW_KIND, PeerSyncEvent.Status.PENDING),  # ③ 新行
            ],
        )
        self.assertIn("作废", rows[0].reason)

        # 按**投递顺序**下家收一遍 ⇒ 终态 listed（修前这里是 gone）。
        self._deliver_to_downstream()
        mirror = PeerHallMirror.objects.get(peer_order_no=order.order_no)
        self.assertEqual(mirror.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(mirror.gone_at)


class ClaimAndReplyEnqueueTests(HallDispatchFixture):
    """上行 `hall.claim` 与应答两类的入队形状。"""

    def _mirror(self, binding, *, peer_order_no="UP-HALL-1", status=None):
        from apps.business.models import PeerHallMirror

        return PeerHallMirror.objects.create(
            peer_upstream_binding=binding,
            peer_base_url="https://peer-hall-up.test.invalid",
            peer_order_no=peer_order_no,
            status=status or PeerHallMirror.Status.LISTED,
            game=self.game,
            payload={"order_no": peer_order_no},
            first_seen_at=timezone.now(),
        )

    def test_enqueue_hall_claim_shape(self):
        """上行 claim：`binding_role="upstream"`、`target_ref` = 上家单号、载荷带显示名。"""
        from apps.business.models import PeerUpstreamBinding

        upstream = PeerUpstreamBinding.objects.create(
            peer_name="测试上家乙",
            peer_base_url="https://peer-hall-up.test.invalid",
            peer_account="syn-up-acct",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-hall-local.test.invalid",
            bound_by=self.binding_on.account,
        )
        mirror = self._mirror(upstream)
        event = enqueue_hall_claim(mirror, taker_id=self.binding_on.account_id)
        row = PeerSyncEvent.objects.get(pk=event.pk)
        self.assertEqual(row.binding_role, "upstream")
        self.assertEqual(row.binding_id, str(upstream.pk))
        self.assertEqual(row.direction, "out")
        self.assertEqual(row.kind, "hall.claim")
        self.assertEqual(row.target_ref, "UP-HALL-1")
        self.assertIn("claim_by", row.payload)

    def test_reply_kinds_land_as_pending_downstream_rows(self):
        """应答两类发回**那个**下家绑定（downstream 方向、pending、载荷空对象）。"""
        for kind in (HALL_CLAIM_OK_KIND, HALL_CLAIM_REFUSED_KIND):
            enqueue_hall_claim_reply(
                self.binding_on, kind=kind, target_ref="UP-HALL-9"
            )
        rows = PeerSyncEvent.objects.order_by("kind")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            {row.kind for row in rows},
            {HALL_CLAIM_OK_KIND, HALL_CLAIM_REFUSED_KIND},
        )
        for row in rows:
            self.assertEqual(row.binding_role, "downstream")
            self.assertEqual(row.binding_id, str(self.binding_on.pk))
            # ⛔ 空对象而不是 None：接收侧对「payload 不是对象」统一 bad_shape 硬跳。
            self.assertEqual(row.payload, {})

    def test_reply_rejects_non_answer_kinds(self):
        """协议外 kind 响亮拒绝（⛔ 不让一个拼错的 kind 变成对端 hard skip 的噪声）。"""
        with self.assertRaises(ValueError):
            enqueue_hall_claim_reply(
                self.binding_on, kind=HALL_THROW_KIND, target_ref="UP-HALL-9"
            )
