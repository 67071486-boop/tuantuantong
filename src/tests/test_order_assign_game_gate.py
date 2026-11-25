"""D-0252 回归钉：指派必须校验**目标打手**的游戏范围（打手没有这个游戏就无法指派）。

口径（业务方裁定，`docs/DECISIONS.md` D-0252）：

- 判据 = **目标打手档案的游戏集合 ∋ 订单的游戏**；**无档案 = 无游戏 = 拒**；
- 三条人工指派入口**同闸**（单笔 `/assign/`、批量 `batch/assign`、录单快速指派
  `assignee`）——判据挂领域咽喉 `order_writes.assign_order`（三条路的公共落库口）；
- 拒绝文案带**游戏名**，错误码对齐既有指派失败档位（`1002`）；
- 机器自动接手 / 镜像单路径**已有各自闸**（镜像单写侧的 `game_scope` 收窄，回报口径
  与「这一行不存在」同形）——本裁定不改它们，最后一条用例钉「那条路还是它自己的闸形」。

⚠️ 夹具一律合成值（E-0077）；断言打在**响应体 + 数据库行**上（全局纪律 4）。
"""
from decimal import Decimal
from datetime import datetime
from datetime import timezone as dt_timezone

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    Order,
    OrderStatus,
    Performance,
)
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import grant_games, make_logged_in_client

User = get_user_model()

BASE = "/api/business/orders"
CLAIM_PATH = "/api/business/hall/upstream/{pk}/claim/"
MIRROR_BASE_URL = "https://assign-gate-mirror.test.invalid"
#: 文案里会出现的游戏名（合成值；断言逐字对它）。
GAME_NAME = "闸门游戏甲"


def _gate_msg():
    return f"该打手没有「{GAME_NAME}」的接单权限"


class AssignGameGateTests(TenantTestCase):
    """三条人工指派入口对「无该游戏的打手」各拒一次；有该游戏的照常成功。"""

    def setUp(self):
        self.game = Game.objects.create(name=GAME_NAME)
        self.other_game = Game.objects.create(name="闸门游戏乙")
        # 对照组：有档案、且持这张单的游戏。
        self.taker_with_game = User.objects.create_user(
            username="gate-taker-yes", password="pw-合成-1"
        )
        grant_games(self.taker_with_game, self.game)
        # 「无档案 = 无游戏」那一支：D-0252 明文的最严档。
        self.taker_no_game = User.objects.create_user(
            username="gate-taker-no", password="pw-合成-2"
        )
        self.client = make_logged_in_client(
            username="gate-cs", groups=("客服",),
            nodes=("order.add_edit", "order.assign_taker", "order.list", "order.view_detail"),
        )
        self._seq = 0

    def _order(self, **kw):
        """单号 20 位数字（`OrderViewSet.lookup_value_regex`）。"""
        self._seq += 1
        kw.setdefault("game", self.game)
        kw.setdefault("project_text", "测试项目甲")
        kw.setdefault("hall_price", Decimal("88.00"))
        kw.setdefault("status", OrderStatus.UNASSIGNED)
        return Order.objects.create(order_no=f"2026101800000000{self._seq:04d}", **kw)

    def _assert_order_untouched(self, order):
        """零写入三断言：状态没进「代练中」、MAIN 行没挂人也没被补出来、指派留痕零行。"""
        row = Order.objects.get(pk=order.pk)
        self.assertEqual(row.status, OrderStatus.UNASSIGNED)
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).exists()
        )
        self.assertEqual(OperationLog.objects.count(), 0)

    # ── ① 单笔 /assign/ ──────────────────────────────────────────────────

    def test_single_assign_rejects_a_taker_without_the_game(self):
        order = self._order()
        body = self.client.post(
            f"{BASE}/{order.order_no}/assign/",
            {"user_id": self.taker_no_game.id},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], int(ErrorCode.BUSINESS), body)
        self.assertEqual(body["msg"], _gate_msg())
        self._assert_order_untouched(order)

    def test_single_assign_succeeds_when_the_taker_has_the_game(self):
        order = self._order()
        body = self.client.post(
            f"{BASE}/{order.order_no}/assign/",
            {"user_id": self.taker_with_game.id},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            Order.objects.get(pk=order.pk).status, OrderStatus.IN_PROGRESS
        )
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, self.taker_with_game.id)

    # ── ② 批量 batch/assign ─────────────────────────────────────────────

    def test_batch_assign_dry_run_reports_ineligible_with_the_game_name(self):
        """批量预检要能**预演出**这一拒（与实名闸 ⓪ 的 `dry_run` 照判同一条纪律）。"""
        order = self._order()
        body = self.client.post(
            f"{BASE}/batch/assign/",
            {"order_nos": [order.order_no], "user_id": self.taker_no_game.id,
             "dry_run": True},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["eligible"], [])
        self.assertEqual(len(body["data"]["ineligible"]), 1)
        issue = body["data"]["ineligible"][0]
        self.assertEqual(issue["order_no"], order.order_no)
        self.assertEqual(issue["code"], int(ErrorCode.BUSINESS))
        self.assertEqual(issue["msg"], _gate_msg())
        self._assert_order_untouched(order)

    def test_batch_assign_apply_rejects_and_writes_nothing(self):
        order = self._order()
        body = self.client.post(
            f"{BASE}/batch/assign/",
            {"order_nos": [order.order_no], "user_id": self.taker_no_game.id,
             "dry_run": False},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], int(ErrorCode.BUSINESS), body)
        self.assertIn(_gate_msg(), body["msg"])
        self._assert_order_untouched(order)

    def test_batch_assign_succeeds_when_the_taker_has_the_game(self):
        order = self._order()
        body = self.client.post(
            f"{BASE}/batch/assign/",
            {"order_nos": [order.order_no], "user_id": self.taker_with_game.id,
             "dry_run": False},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], [order.order_no])
        self.assertEqual(
            Order.objects.get(pk=order.pk).status, OrderStatus.IN_PROGRESS
        )
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, self.taker_with_game.id)

    # ── ③ 录单快速指派（`POST /orders/` 带 `assignee`）────────────────────

    def test_quick_assign_fails_as_1022_and_keeps_the_order(self):
        """快速指派：**订单留下**（既有裁定，⛔ 不回滚）、指派那一步回可分辨的 `1022`。"""
        body = self.client.post(
            f"{BASE}/",
            {"game": self.game.id, "project_text": "测试项目甲",
             "hall_price": "88.00", "assignee": self.taker_no_game.pk},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1022, body)
        self.assertEqual(set(body["data"]), {"order_no", "reason", "assign_code"})
        self.assertEqual(body["data"]["assign_code"], int(ErrorCode.BUSINESS))
        self.assertEqual(body["data"]["reason"], _gate_msg())
        order = Order.objects.get(order_no=body["data"]["order_no"])
        # 单在库里（设计如此），但没被指派出去：状态没进「代练中」、MAIN 行没挂人。
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertIsNone(perf.user_id)

    def test_quick_assign_succeeds_when_the_taker_has_the_game(self):
        body = self.client.post(
            f"{BASE}/",
            {"game": self.game.id, "project_text": "测试项目甲",
             "hall_price": "88.00", "assignee": self.taker_with_game.pk},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, self.taker_with_game.pk)


class MirrorPathKeepsItsOwnGateTests(TenantTestCase):
    """镜像单接单路径**行为不变**：它有自己的写侧 `game_scope` 闸，回报与「不存在」同形。

    ⛔ 本裁定不改那条路（D-0252 ③）；这条用例钉的是「两路没有被人好心并成一路」——
    若有人把镜像接单也改走 `assign_order` 的指派闸，这里的码 / 文案会变成
    `1002` +「该打手没有「…」的接单权限」，当场红。
    """

    def setUp(self):
        self.game = Game.objects.create(name=GAME_NAME)
        self.other_game = Game.objects.create(name="闸门游戏乙")
        owner = User.objects.create_user(username="gate-mirror-owner", password="pw-合成-3")
        binding = JidanpuUpstreamBinding.objects.create(
            user=owner, jidanpu_base_url=MIRROR_BASE_URL,
            account="acct-合成-甲", password="pw-合成", enabled=True,
        )
        self.mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=MIRROR_BASE_URL,
            upstream_order_id="20261018000000000999",
            owner_binding=binding,
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game,
            project_text="合成项目",
            remote_remark="合成备注",
            remote_role_name="合成角色",
            remote_rec_fee=Decimal("88.00"),
            upstream_payload={
                "orderId": "20261018000000000999", "orderContent": "合成需求",
                "orderRemark": "合成备注", "roleName": "合成角色",
                "gameAcc": "acc-合成", "zgameAcc": "sub-合成", "recFee": "88.00",
            },
            first_seen_at=datetime(2026, 10, 18, tzinfo=dt_timezone.utc),
        )

    def test_out_of_scope_claimer_still_gets_the_mirror_gate_shape(self):
        """持节点但**只有另一只游戏**的打手 ⇒ 镜像路自己的 `1012` /「已被接走」形，零写入。"""
        outsider = make_logged_in_client(
            username="gate-mirror-outsider", groups=("代练员",),
            games=[self.other_game], nodes=("grab_hall.take_order",),
        )
        body = outsider.post(CLAIM_PATH.format(pk=self.mirror.pk)).json()
        self.assertEqual(body["code"], ErrorCode.ORDER_ALREADY_TAKEN, body)
        self.assertIsNone(body["data"])
        # 判别口：不是新指派闸的那句话（两路若被并成一路，这里会变成 1002 + 游戏名文案）。
        self.assertNotIn("接单权限", body["msg"])
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(
            JidanpuUpstreamHallOrder.objects.get(pk=self.mirror.pk).status,
            JidanpuUpstreamHallOrder.Status.LISTED,
        )
