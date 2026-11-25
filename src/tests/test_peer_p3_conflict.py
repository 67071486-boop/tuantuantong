"""上下游 P3 Task 7：**冲突裁决**「以最后更改的为主」（spec §16.3 的时刻裁决）。

业务方口径：**最后更改的为主**。两边各自都可能改状态 ⇒ 下家侧收到上家事件时，必须拿
「我这边的最后一次本地动作时刻」与事件的 `occurred_at` 比一比：

- 事件**不晚于**本地最后一次动作（`occurred_at <= link.local_action_at`）⇒ 良性跳过
  `stale`（本地那次更新更晚，别被更旧的上家事件盖掉）；
- 事件**严格晚于**它 ⇒ 照常应用（上家那次更新更晚，本地让位）。

本文件钉三层：

① **本地动作时刻真的被盖上了**（`PeerOrderLink.local_action_at`）：只有挂着
   **启用且未软删**的上游绑定时才盖——否则这张单每改一次状态都会写一行无意义的时刻；
② **挡更旧**：本地刚改过（`local_action_at` 晚于事件）⇒ 事件 `stale`、库里状态一个字不变；
   相等也算（`<=`：同刻的本地动作视为不比事件旧）；
③ **不误挡更新**：事件晚于本地动作 ⇒ 照常应用；没有本地动作时刻（`None`）⇒ 新判据不参与
   （既有 P1/P2 行为逐字不变，回归网是 `tests/test_peer_inbound_update.py`）。

⚠️ 判据走**真端点** `/transition/` 产生本地动作（`order_status.apply_transition` 是唯一写入口），
⛔ 不直接调内部函数——否则「挂钩没接上」这类静默缺陷测不出来。
⚠️ 夹具一律**合成值**（`测试游戏` / `SYN-P3-*` / `*.test.invalid`）。本地订单号必须是
`OrderViewSet.lookup_value_regex` 要求的 **20 位数字**（形状照 `apps/business/order_no.py`
的产物，值本身无意义，⛔ 不是真实数据）。
⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里，普通 `TestCase`
的 search_path 里没有 `business_*` 表。
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.api.peer_views import receive_events
from apps.business.models import (
    Game,
    Order,
    OrderStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
)
from apps.business.peer_sync import hash_token, new_token
from tests.p5_auth_helpers import make_logged_in_client

ORDERS = "/api/business/orders"

#: 本地订单号：URL 里的单号必须 20 位数字（`OrderViewSet.lookup_value_regex`）。
LOCAL_NO = "20260926000000000071"
#: 对端（上家）单号（去重键是 `(绑定, 对端单号)`）。
PEER_NO = "SYN-P3-UP-1"

#: 本文件走到的端点门：`/transition/` 的类级门 + `get_object()` 的取数层。
NODES = ("order.change_status", "order.list")


def _now():
    """此刻（带时区）。测试自己造时刻，⛔ 不依赖某处写死的秒数。"""
    return timezone.now()


def _now_minus(seconds):
    """`seconds` 秒之前（带时区）。"""
    return timezone.now() - timedelta(seconds=seconds)


class TestPeerBase(TenantTestCase):
    """共用夹具：一张**挂着生效上游绑定**的本地单 + 一个能改状态的登录客服。

    - `self.order`：本地单，初始 `WAITING_GRAB`；
    - `self.link`：`self.order` ↔ `self.upstream_binding` 的对应行（`local_action_at` 为空）；
    - `self.api`：持 `order.change_status` 的登录客户端（驱动真 `/transition/` 端点）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏")
        self.kefu = get_user_model().objects.create_user(
            username="syn-p3-conflict-kefu", password="pw-syn-p3"
        )
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家甲",
            peer_base_url="https://peer-up.test.invalid",
            peer_account="syn-p3-account",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-local.test.invalid",
            bound_by=self.kefu,
        )
        self.order = Order.objects.create(
            order_no=LOCAL_NO,
            game=self.game,
            status=OrderStatus.WAITING_GRAB,
            total_price=Decimal("10.00"),
        )
        self.link = PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.upstream_binding,
            peer_order_no=PEER_NO,
        )
        # ⚠️ `groups` 必须给：`_grant_nodes` 只把节点挂到**已有 Group** 对应的 Role 上，
        #    不给组就等于一个节点都没挂上（症状是 403，看起来像判据写错）。
        self.api = make_logged_in_client(
            username="syn-p3-conflict-cs", groups=("客服",), nodes=NODES
        )

    # ── 夹具构造器 ────────────────────────────────────────────────────────
    def _event(self, *, occurred_at, status=OrderStatus.ISSUE):
        """造一条下家侧的 `order.update` 事件（`occurred_at` 在**事件级**顶层）。"""
        return {
            "kind": "order.update",
            "target_ref": PEER_NO,
            "occurred_at": occurred_at.isoformat(),
            "payload": {"status": str(status), "game_name": self.game.name},
        }

    def _set_link_local_action(self, link, moment):
        """把「本地最后一次动作时刻」直接写成给定值（判据要能分辨两个取值）。"""
        link.local_action_at = moment
        link.save(update_fields=["local_action_at"])

    def _transition(self, order, target):
        """走**真端点**改本地状态（`apply_transition` 是唯一写入口，挂钩必须真的接上）。"""
        response = self.api.post(
            f"{ORDERS}/{order.order_no}/transition/",
            {"target": str(target)},
            content_type="application/json",
        )
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()


class ConflictArbitrationTests(TestPeerBase):
    """「最后更改的为主」在**下家侧**的三条判据（Brief Step 1）。"""

    def test_a_local_change_newer_than_the_incoming_event_wins(self):
        """本地刚改过、上家事件更旧 ⇒ 事件良性 `stale`，库存状态一个字不变。"""
        self._set_link_local_action(self.link, _now())
        outcome = receive_events(
            self.upstream_binding, [self._event(occurred_at=_now_minus(600))]
        )
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.status, "ISSUE")
        self.assertTrue(
            any(d["code"] == "stale" for d in outcome["skipped_details"]),
            outcome,
        )

    def test_a_newer_incoming_event_still_wins(self):
        """上家事件更晚 ⇒ 照常应用（新判据⛔ 不许把"本地改过"一律当成保护）。"""
        self._set_link_local_action(self.link, _now_minus(600))
        receive_events(self.upstream_binding, [self._event(occurred_at=_now())])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "ISSUE")

    def test_a_local_transition_stamps_local_action_at(self):
        """下家本地改状态 ⇒ 那张单的上游对应行盖上本地动作时刻。"""
        self._transition(self.order, OrderStatus.PAUSED)
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.local_action_at)

    def test_the_boundary_equality_is_stale(self):
        """⭐ 边界：`occurred_at == local_action_at` 也算本地赢（判据是 `<=`，⛔ 不是 `<`）。

        判别力：把实现里的 `<=` 改成 `<` ⇒ 这条用例的 `stale` 断言红、状态被事件盖上。
        """
        moment = _now()
        self._set_link_local_action(self.link, moment)
        outcome = receive_events(
            self.upstream_binding, [self._event(occurred_at=moment)]
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(
            [d["code"] for d in outcome["skipped_details"]], ["stale"], outcome
        )

    def test_a_disabled_binding_is_not_stamped(self):
        """绑定被停用 ⇒ ⛔ 不盖本地动作时刻（不同步的单不该留同步用的记号）。"""
        self.upstream_binding.enabled = False
        self.upstream_binding.save(update_fields=["enabled"])
        self._transition(self.order, OrderStatus.PAUSED)
        self.link.refresh_from_db()
        self.assertIsNone(self.link.local_action_at)

    def test_a_soft_deleted_binding_is_not_stamped(self):
        """绑定已软删（解绑）⇒ 同样不盖。"""
        self.upstream_binding.deleted_at = _now()
        self.upstream_binding.save(update_fields=["deleted_at"])
        self._transition(self.order, OrderStatus.PAUSED)
        self.link.refresh_from_db()
        self.assertIsNone(self.link.local_action_at)
