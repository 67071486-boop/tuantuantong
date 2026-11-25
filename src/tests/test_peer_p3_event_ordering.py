"""上下游 P3：一次**带价指派 / 抢单**的两条事件必须按业务顺序落地（I-1 顺序回归）。

## 这条用例守的是什么

一次带 `price` 的 `/assign/`、或一次 `/claim/`，会在**同一个请求、同一个事务**里给同一条
下游绑定入队**两条**事件：

1. `order.update`（`/assign/` 是发单价变化 `notify_order_price_changed`；`/claim/` 是
   `apply_transition` 里的状态漏斗——此刻 MAIN 行的归属人**已经**是抢单人）——**先**入队；
2. `order.assign` / `order.grab`（指派 / 抢单本身）——**后**入队。

worker 取活按 `created_at` 最老优先（`claim_due_events`）⇒ 下家先收到 `order.update`、
再收到那条更全的事件。而两条事件若**共用同一个事件时刻**（都取 `Order.updated_at`）：

- 第一条 `order.update` 把 `PeerOrderLink.last_applied_at` 推到那个时刻（D-0377 起
  这一支持自己也写客户价与内容面，它写不到的只剩「接手事实」那一半——落「未分配」）；
- 第二条 `order.assign` / `order.grab` 的事件时刻**不晚于** `last_applied_at` ⇒ 命中 `<=`
  的 `stale` 闸被**良性跳过**，于是接手事实与其余白名单**静默丢掉**。

自愈也补不回来：对账用的水位就是那个 `last_applied_at`，它的判据同样是 `<=`。

⭐ 修法是给两个 P1/P2 壳加显式 `occurred_at` 直通、并在两处调用点传 `timezone.now()`
（与 `/assign/` 换人档 / `/delete/` 的先例同一个写法）⇒ 后一条的事件时刻**严格晚于**
那条 `order.update` ⇒ 它自己成为落地的那一条。**两个调用点各有一条用例**（E-0021：
判据要打在"这类路径"上，⛔ 不是只打在被点名的那一条）。

## 这条用例的判别力（⛔ 不是"两条都收到"就算过）

D-0377 起 `order.update` 支自己也写客户价与内容面 ⇒ 「`total_price` / `remark` 落地」
**不再能分辨哪条事件落了**；现在把变异（两条事件共用时刻 ⇒ 较全那条被 stale 吃掉）
分开的是三把闸：

- **状态**：只有 `order.assign` / `order.grab` 落「未分配」（`order.update` 照抄对端的
  `IN_PROGRESS`）⇒ 变异下先红；
- **applied / skipped**：变异下第二条被跳过 ⇒ `skipped` 断言红；
- **水位**：被跳过的条不推进 `last_applied_at` ⇒ 「水位 = 最后那条事件的时刻」断言红。

（历史上本网靠「`order.update` 不写 total_price / remark」做判别；那两列随 D-0307 /
D-0377 先后进更新支后，判别力收敛到上面三把。）⭐ 实测 RED 证据（去掉调用点的
`occurred_at`）见 Task 10 报告。

⚠️ 夹具值**全部是合成值**（`2026…` 单号 / `测试*` / `*.test.invalid`），⛔ 不含真实租户数据。
⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.api.peer_views import receive_events
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_sync import hash_token, new_token
from tests.p5_auth_helpers import grant_games, make_logged_in_client

ORDERS = "/api/business/orders"
HALL = "/api/business/hall"

#: `/assign/`（带价）要过的门：类级 `order.assign_taker`、价格闸 `order.submit_payout_price`、
#: 状态机那条边用的 `order.change_status`、取数层 `order.list`（与既有夹具同源）。
NODES_ASSIGN = (
    "order.assign_taker",
    "order.submit_payout_price",
    "order.change_status",
    "order.list",
)
#: `/hall/{单号}/claim/` 要过的门：类级 `grab_hall.take_order`（也就是
#: `WAITING_GRAB → IN_PROGRESS` 那条边需要的节点之一）、取数层 `order.list`。
NODES_CLAIM = ("grab_hall.take_order", "order.list")

#: 上家侧（发单方）的订单号；必须 20 位数字（`OrderViewSet.lookup_value_regex`）。
SENDER_NO = "20260928000000000901"
#: 下家侧（接单方）本地订单号，与上家单号**不同**（两边是同一笔真实订单的各自一行）。
LOCAL_NO = "20260928000000000902"
#: 两端按**名字**对齐的游戏（裁决 R21）。
GAME_NAME = "P3 事件顺序夹具游戏"


class EventOrderingRegressionTests(TenantTestCase):
    """`/assign/` 与 `/claim/` 各自的两条事件：落地的是**较全**的那一条。"""

    def setUp(self):
        self.game = Game.objects.create(name=GAME_NAME)

        # ── 上家侧（`/assign/` 用）：一条生效的下游绑定 + 那个账号的提成档案 ──
        self.taker = get_user_model().objects.create_user(
            username="syn-p3-order-taker", password="pw-syn-p3-order"
        )
        StaffProfile.objects.create(
            user=self.taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=无游戏=拒）
        # ⇒ 被指派的夹具账号要持本类的游戏（专钉：tests/test_order_assign_game_gate.py）。
        grant_games(self.taker, self.game)
        self.downstream_binding = PeerDownstreamBinding.objects.create(
            account=self.taker,
            peer_base_url="https://peer-down.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )

        # ── 下家侧：一条启用的上游绑定 + 一张**已经链接**的本地单 ──
        # ⭐ "已经链接"是这条回归的前提：`apply_events` 走**更新**支（不是建单支），
        #    两条事件才能在同一张单上按时刻裁决出「谁落地」。
        self.receiver_kefu = get_user_model().objects.create_user(
            username="syn-p3-order-kefu", password="pw-syn-p3-order"
        )
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家甲",
            peer_base_url="https://peer-up.test.invalid",
            peer_account="syn-up-account",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-local.test.invalid",
            bound_by=self.receiver_kefu,
            # ⭐ P4 修夹具：P1 起 `hall.*` 事件受**接收侧开关**管
            #    （`PeerUpstreamBinding.hall_sync_enabled`，**默认关**）⇒ 本夹具不打开它，
            #    下面 `_deliver_oldest_first` 里那两条 `hall.take` 会被整批静默跳过
            #    （`applied` 少 2）⇒ 这条用例自 P1 合入起一直红。
            #    ⚠️ 打开开关**不改**本用例的判据：两条 take 落在一张没有镜像行的租户上
            #    = 良性 applied（`_apply_hall_event` 的 take 支命中 0 行），
            #    它钉的仍然是「`order.grab` 不被 `order.update` 顶成 stale」。
            hall_sync_enabled=True,
        )
        self.local_order = Order.objects.create(
            order_no=LOCAL_NO,
            game=self.game,
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("10.00"),
            hall_price=Decimal("10.00"),
            remark="本地旧备注",
        )
        self.link = PeerOrderLink.objects.create(
            order=self.local_order,
            upstream_binding=self.upstream_binding,
            peer_order_no=SENDER_NO,
            # `last_applied_at` 留空 = 下家还没应用过任何上家事件 ⇒ 第一条事件一定应用得上。
        )

        # `/assign/` 由客服驱动（被指派人是 `self.taker`）。
        self.api = make_logged_in_client(
            username="syn-p3-order-cs", groups=("客服",), nodes=NODES_ASSIGN
        )

    # ── 夹具 ─────────────────────────────────────────────────────
    def _make_sender_order(self, status):
        """上家侧那张单：`total_price` / `remark` 与下家那一行**取值不同**，好分辨谁落地了。"""
        order = Order.objects.create(
            order_no=SENDER_NO,
            game=self.game,
            status=status,
            total_price=Decimal("100.00"),
            hall_price=Decimal("100.00"),
            remark="上游指派备注",
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("20.00")
        )
        return order

    def _deliver_oldest_first(self, expected_kinds, expected_total_price="66.00"):
        """按 worker 取活的顺序（`created_at` 最老优先）投递，并断言**较全的那条**落地了。

        ⚠️ 顺带钉住**入队顺序**：`order.update` 必须排在较全那条之前（先状态 / 先写价、
        后记指派 / 抢单）——顺序若反了，顺序断言直接红，⛔ 不让用例在"两条恰好都应用"的
        假绿里过去。
        """
        events = list(
            PeerSyncEvent.objects.filter(binding_role="downstream").order_by(
                "created_at", "pk"
            )
        )
        self.assertEqual([event.kind for event in events], expected_kinds)
        # ⭐ 按 worker 的真实顺序投递（最老优先）：先 order.update、后较全那条。
        result = receive_events(
            self.upstream_binding, [self._inbound(event) for event in events]
        )

        self.local_order.refresh_from_db()
        # 前置哨兵：预期那几条事件确实写下了（否则"白名单落地"可能是因为压根没有事件）。
        self.assertEqual(len(events), len(expected_kinds))
        # ⭐ 白名单哨兵：客户价与备注真的落了。⚠️ D-0377 起更新支也写这两列 ⇒ 它们
        #    **不是判别位**（变异下照样过）；判别在下面的状态 / applied / 水位三把。
        #    仍放在 `applied` 之前：整条链路死了它们先红，读数好认。
        # ⛔ 业务方裁定：客户价（= 接单价）取**发单价** ⇒ 两列同源。
        self.assertEqual(
            self.local_order.total_price, Decimal(expected_total_price)
        )
        self.assertEqual(self.local_order.remark, "上游指派备注")
        # ⚠️ P4 起接手类落地即未分配（业务方）：较全的那支是 `order.assign` /
        #    `order.grab` ⇒ 状态落 `UNASSIGNED`（⛔ 不是对端的 `IN_PROGRESS`）。
        #    ⚠️ 这仍是**强判别**：只落地 `order.update` 时状态会是 `IN_PROGRESS`，
        #    只有较全那支落下来才会被改成 `UNASSIGNED`（正是本条要证明的事）。
        self.assertEqual(self.local_order.status, OrderStatus.UNASSIGNED)
        # 两条都该算"应用"：后一条不是 `stale`。
        self.assertEqual(result["applied"], len(expected_kinds), result)
        self.assertEqual(result["skipped"], 0, result)
        # 水位推进到最后那条事件（对账据此判"没有缺口"）。
        self.link.refresh_from_db()
        self.assertEqual(
            self.link.last_applied_at.isoformat(),
            events[-1].meta["occurred_at"],
        )
        return result, events

    @staticmethod
    def _inbound(event):
        """把一条出站事件翻成接收侧的入站事件对象（事件级字段在顶层，⛔ 不在 payload）。"""
        return {
            "kind": event.kind,
            "target_ref": event.target_ref,
            "payload": event.payload,
            "occurred_at": event.meta["occurred_at"],
        }

    # ── 两条路径各一条用例 ────────────────────────────────────────
    def test_the_full_whitelist_lands_because_the_assign_is_not_stale_skipped(self):
        """`/assign/` 带价：两条事件都投递完之后，下家拿到的是 `order.assign` 的**整张白名单**。

        判别力：把 `/assign/` 那处调用点的 `occurred_at=timezone.now()` 去掉（两条事件共用
        `Order.updated_at`）⇒ `order.assign` 被判 `stale` 跳过 ⇒ 本网的判别在
        `UNASSIGNED` 状态断言、`skipped` 计数与水位三处红（`total_price` / `remark`
        在 D-0377 起的更新支上照样落地，不再是判别位）。
        """
        self._make_sender_order(OrderStatus.UNASSIGNED)

        response = self.api.post(
            f"{ORDERS}/{SENDER_NO}/assign/",
            {"user_id": self.taker.id, "price": "66.00"},
            content_type="application/json",
        )
        body = response.json()
        self.assertEqual(body["code"], 0, body)

        _, events = self._deliver_oldest_first(["order.update", "order.assign"])
        # 两条事件都带的发单价照旧落地（不是"整条链路死了"）。
        self.assertEqual(self.local_order.hall_price, Decimal("66.00"))
        self.assertEqual(events[-1].kind, "order.assign")

    def test_the_full_whitelist_lands_because_the_grab_is_not_stale_skipped(self):
        """`/claim/`：状态漏斗先写出 `order.update` ⇒ 若不给 `order.grab` 更晚的时刻，
        它同样会被 `stale` 吃掉、接手事实与其余白名单丢掉。

        判别力：把 `/claim/` 那处调用点的 `occurred_at=timezone.now()` 去掉 ⇒
        状态 / skipped / 水位三处断言红（与 `/assign/` 那条同一个形状，⛔ 不是重复：两个调用点两处修）。
        """
        self._make_sender_order(OrderStatus.WAITING_GRAB)
        # 抢单人 = 这条下游绑定的账号（`order.grab` 的投递对象由当前 MAIN 归属人决定）。
        # ⚠️ 必须给一个**组**：`_grant_nodes` 把节点挂到该用户**所在组的 `Role`** 上，
        #    `groups=()` 时节点一个都挂不上 ⇒ 403（与判据写错同一个症状）。
        claimer_client = make_logged_in_client(
            username="syn-p3-order-claimer", groups=("代练员",), nodes=NODES_CLAIM
        )
        claimer = get_user_model().objects.get(username="syn-p3-order-claimer")
        PeerDownstreamBinding.objects.create(
            account=claimer,
            peer_base_url="https://peer-down.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )

        response = claimer_client.post(
            f"{HALL}/{SENDER_NO}/claim/", {}, content_type="application/json"
        )
        body = response.json()
        self.assertEqual(body["code"], 0, body)

        # ⭐ D-0303起：抢单路径会向**全部启用推送的下家绑定**广播
        #    `hall.take`（大厅摘牌，本线要的行为）——夹具里有两个启用的下家绑定
        #    ⇒ 队列里是两条 `hall.take`，都排在 `order.grab` 之前（claim 事务里先广播后记抢）。
        _, events = self._deliver_oldest_first(
            ["order.update", "hall.take", "hall.take", "order.grab"],
            expected_total_price="100.00",
        )
        self.assertEqual(self.local_order.hall_price, Decimal("100.00"))
        self.assertEqual(events[-1].kind, "order.grab")
