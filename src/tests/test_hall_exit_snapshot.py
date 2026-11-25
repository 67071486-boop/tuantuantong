"""大厅**离厅快照**的写点枚举网（D-0296，2026-10-24）。

## 这张网钉什么

「一张单离开大厅 ⇒ 恰好一条 `HallExitSnapshot`，`exit_kind` 正确、payload 是
**在厅视图**」。写点的全集（枚举即契约，漏一个 = 大厅少一张灰卡）：

| 入口 | 期望 |
|---|---|
| 内部 `/claim`（`claim_order` → `apply_transition`） | 恰 1 条 `claimed` |
| takerapi `/claim`（同一个漏斗） | 恰 1 条 `claimed` |
| 机器接手（`claim_order(enforce_limits=False)`） | 恰 1 条 `claimed` |
| `/withdraw/` | 恰 1 条 `withdrawn` |
| `/transition/`（大厅单 → 非大厅目标） | 恰 1 条 `withdrawn` |
| 批量 `batch/transition` | 每单恰 1 条 |
| 软删（在厅单；**不经状态机**，写点在 `recycle.soft_delete_order`） | 恰 1 条 `withdrawn` |
| 非在厅单软删 / 进厅（throw-to-hall） | **零**快照（否定面，两路证据） |
| 回池 → 重抛 → 再抢 | **第二条**（不覆盖第一条） |
| 镜像 `listed → claiming` | 恰 1 条 `claimed`；随后转正**不重复** |
| 镜像 `_mark_gone`（原状态 listed） | 恰 1 条 `withdrawn` |
| 镜像救回（rescue 命令） | 快照**被删**（防一单双卡） |

⚠️ **payload 的三条覆写判据**（为什么见 `hall_snapshot.native_payload`）：
`status == WAITING_GRAB`（在厅视图）、`taker_name is None`（在厅没人）、
`hall_price == 抛单价`（即使绩效行有覆盖价——判价打在**原始在厅价**上）。
⚠️ 每条钉子的变异方向都写在 docstring 里（把它弄红的那个改法）。
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business import jidanpu, jidanpu_upstream
from apps.business.claim import claim_order
from apps.business.models import (
    EmpType,
    Game,
    HallExitSnapshot,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    Order,
    OrderStatus,
    Performance,
)
from apps.business.recycle import soft_delete_order
from apps.business.api.serializers import HallListSerializer
from apps.takerapi.models import TakerApp
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

HALL_CLAIM_URL = "/api/business/hall/{order_no}/claim/"
TAKER_CLAIM_URL = "/api/taker/v1/hall/{order_no}/claim/"
TAKER_TOKEN_URL = "/api/taker/oauth/token"
WITHDRAW_URL = "/api/business/orders/{order_no}/withdraw/"
TRANSITION_URL = "/api/business/orders/{order_no}/transition/"
BATCH_TRANSITION_URL = "/api/business/orders/batch/transition/"
CLEAR_TAKER_URL = "/api/business/orders/{order_no}/clear-taker/"
THROW_URL = "/api/business/orders/{order_no}/throw-to-hall/"

CLAIM_NODES = ("grab_hall.take_order", "order.change_status")
#: 机器路径跳的是**按人**闸门（`enforce_limits=False`），状态机的边照样要节点
#: ⇒ 机器用例传与人类入口同一份节点集。
MACHINE_NODES = CLAIM_NODES

BASE_URL = "https://jidanpu-snapshot.test.invalid"
PASSWORD = "pw-canary-snapshot"

#: 文档保留段（⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.51"


def _no(n: int) -> str:
    return f"2026102400000000{n:04d}"


class _SnapshotFixture(TenantTestCase):
    """原生单 + 登录夹具的公共部分。"""

    def setUp(self):
        self.game = Game.objects.create(name="快照测试游戏")
        # 客服（能走 /withdraw /transition /throw 等写动作）
        self.staff = make_logged_in_client(
            username="snapshot-staff", groups=("客服",), games=[self.game],
            nodes=("grab_hall.take_order", "order.change_status", "order.clear_taker",
                   # ⭐ 计划 ④：抛大厅改判独占 36 ⇒ 夹具补它（20 不再开这道门）。
                   "order.send_to_grab_hall", "order.set_offer_price", "order.add_edit",
                   "order.list", "order.withdraw_from_grab_hall"),
        )
        self.staff_user = User.objects.get(username="snapshot-staff")

    def _order(self, no, *, status=OrderStatus.WAITING_GRAB, price="88.00",
               taker_price=None):
        order = Order.objects.create(
            game=self.game, order_no=no, status=status,
            hall_price=Decimal(price) if price is not None else None,
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN,
            temp_amount=Decimal(price) if price is not None else Decimal("0.00"),
        )
        if taker_price is not None:
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).update(
                taker_price=Decimal(taker_price)
            )
        return order

    def _snapshots(self, **filters):
        return HallExitSnapshot.objects.filter(**filters).order_by("pk")

    def _claim(self, order, *, user, nodes=CLAIM_NODES, **kwargs):
        return claim_order(user_id=user.pk, order_id=order.pk, nodes=nodes, **kwargs)


class NativeExitSnapshotTests(_SnapshotFixture):
    """原生单的写点枚举：每种「离厅」恰好一条，方向正确。"""

    def setUp(self):
        super().setUp()
        # taker 走 takerapi 真路由的形状（照 test_takerapi_claim_api 的 `_make_taker`）：
        # 代练员组 + `grab_hall.take_order` 节点（状态机的边与对外端点都判它）。
        from django.contrib.auth.models import Group

        from tests.p5_auth_helpers import grant_nodes

        self.taker = User.objects.create_user(username="snapshot-taker")
        group, _ = Group.objects.get_or_create(name="代练员")
        self.taker.groups.add(group)
        StaffProfile.objects.create(user=self.taker).games.set([self.game])
        grant_nodes(self.taker, "grab_hall.take_order")

    # ── 内部 claim ──────────────────────────────────────────────────────────

    def test_a_native_claim_writes_exactly_one_claimed_snapshot(self):
        """内部 claim ⇒ 恰 1 条 `claimed`。

        变异方向：把 `apply_transition` 里那段快照写入删掉 ⇒ 本用例红（0 ≠ 1）；
        把 `exit_kind` 的分支反过来（进厅也写 / 目标判反）⇒ 分别红在「数量」与「取值」。
        """
        order = self._order(_no(1))
        self._claim(order, user=self.taker)
        snaps = self._snapshots()
        self.assertEqual(len(snaps), 1)
        snap = snaps[0]
        self.assertEqual(snap.source, HallExitSnapshot.Source.NATIVE)
        self.assertEqual(snap.exit_kind, HallExitSnapshot.ExitKind.CLAIMED)
        self.assertEqual(snap.order_id, order.pk)
        self.assertIsNone(snap.mirror_id)
        self.assertEqual(snap.game_id, self.game.pk)

    def test_the_takerapi_claim_writes_the_same_one_snapshot(self):
        """takerapi 的对外 claim ⇒ 同样恰 1 条（两条对外入口共用同一个漏斗）。"""
        order = self._order(_no(2))
        app = TakerApp.objects.create(
            name="快照测试应用", app_key="snapshottakekey01",
            app_secret="snapshot-taker-secret-0000000001",
            taker=self.taker, created_by=self.taker,
        )
        from django.test import Client

        anon = Client(SERVER_NAME="tenant.test.com")
        token_response = anon.post(
            TAKER_TOKEN_URL,
            {
                "grant_type": "client_credentials",
                "client_id": app.app_key,
                "client_secret": app.app_secret,
            },
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(token_response.status_code, 200, token_response.content[:200])
        token = token_response.json()["access_token"]
        response = anon.post(
            TAKER_CLAIM_URL.format(order_no=order.order_no),
            HTTP_AUTHORIZATION=f"Bearer {token}", REMOTE_ADDR=SOURCE_IP,
            # 幂等键是这条对外写端点的入参契约（8–128 字符；合成值）。
            HTTP_IDEMPOTENCY_KEY="snapshot-taker-claim-0001",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 0, response.json())
        snaps = self._snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].exit_kind, HallExitSnapshot.ExitKind.CLAIMED)
        self.assertEqual(snaps[0].order_id, order.pk)

    def test_the_machine_claim_writes_the_snapshot_too(self):
        """机器接手（`enforce_limits=False` 那条路）⇒ 快照照写（跳的只是按人闸门）。"""
        order = self._order(_no(3))
        self._claim(order, user=self.taker, enforce_limits=False)
        snaps = self._snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].exit_kind, HallExitSnapshot.ExitKind.CLAIMED)

    # ── withdraw / transition / 批量 / 软删 ────────────────────────────────

    def test_a_withdraw_writes_one_withdrawn_snapshot(self):
        order = self._order(_no(4))
        response = self.staff.post(WITHDRAW_URL.format(order_no=order.order_no), {})
        self.assertEqual(response.json()["code"], 0, response.json())
        snaps = self._snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].exit_kind, HallExitSnapshot.ExitKind.WITHDRAWN)

    def test_a_generic_transition_writes_one_withdrawn_snapshot(self):
        """`/transition/` 大厅单 → 未分配：同一张网盖住通用流转那一个入口。"""
        order = self._order(_no(5))
        response = self.staff.post(
            TRANSITION_URL.format(order_no=order.order_no),
            {"target": OrderStatus.UNASSIGNED},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0, response.json())
        snaps = self._snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].exit_kind, HallExitSnapshot.ExitKind.WITHDRAWN)

    def test_a_batch_transition_writes_one_snapshot_per_order(self):
        first = self._order(_no(6))
        second = self._order(_no(7))
        response = self.staff.post(
            BATCH_TRANSITION_URL,
            {
                "order_nos": [first.order_no, second.order_no],
                "target": OrderStatus.UNASSIGNED,
                "dry_run": False,
            },
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertEqual(len(self._snapshots()), 2)
        self.assertEqual(
            {s.order_id for s in self._snapshots()}, {first.pk, second.pk}
        )
        self.assertEqual(
            {s.exit_kind for s in self._snapshots()},
            {HallExitSnapshot.ExitKind.WITHDRAWN},
        )

    def test_soft_deleting_an_in_hall_order_writes_one_withdrawn_snapshot(self):
        """⭐ 软删**不经状态机**——这个写点长在 `recycle.soft_delete_order` 的首次删除分支。

        变异方向：把那段写入删掉 ⇒ 本用例红。幂等路径（第二次删）**不**再写
        （下面那条对照断言）。
        """
        order = self._order(_no(8))
        soft_delete_order(order=order, actor=self.staff_user, is_del_emp="N")
        snaps = self._snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].exit_kind, HallExitSnapshot.ExitKind.WITHDRAWN)
        # 幂等对照：已删单再删一次 ⇒ 不再写（"删过几次"的审计语义不被稀释）。
        soft_delete_order(order=order, actor=self.staff_user, is_del_emp="N")
        self.assertEqual(len(self._snapshots()), 1)

    def test_soft_deleting_an_off_hall_order_writes_nothing(self):
        """否定面：不在厅的单被删，没有「离厅」可记（两路证据：非大厅状态 + 零快照）。"""
        pool_order = self._order(_no(9), status=OrderStatus.UNASSIGNED)
        done_order = self._order(_no(10), status=OrderStatus.COMPLETED)
        soft_delete_order(order=pool_order, actor=self.staff_user, is_del_emp="N")
        soft_delete_order(order=done_order, actor=self.staff_user, is_del_emp="N")
        self.assertEqual(self._snapshots().count(), 0)
        # 两个订单都真的被删了（夹具自证：不是"没删成所以没写"）。
        self.assertIsNotNone(Order.all_objects.get(pk=pool_order.pk).deleted_at)
        self.assertIsNotNone(Order.all_objects.get(pk=done_order.pk).deleted_at)

    # ── 进厅不写 / 回池重抛再抢 = 第二条 ───────────────────────────────────

    def test_throwing_to_the_hall_writes_nothing(self):
        """进厅不写（快照只记「离」）。否定面配对照：同一单随后被抢 ⇒ 恰 1 条。"""
        order = self._order(_no(11), status=OrderStatus.UNASSIGNED)
        response = self.staff.post(THROW_URL.format(order_no=order.order_no), {})
        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertEqual(self._snapshots().count(), 0)
        self._claim(order, user=self.taker)
        self.assertEqual(self._snapshots().count(), 1)

    def test_pool_then_rethrow_then_reclaim_writes_a_second_snapshot(self):
        """回池 → 重抛 → 再抢 = **第二条**（快照是历史序列，⛔ 不去重也不覆盖）。"""
        order = self._order(_no(12))
        self._claim(order, user=self.taker)
        # 回池两步（D2 之后的产品路径）：清人 → 未分配；重抛 → 待抢单。
        cleared = self.staff.post(CLEAR_TAKER_URL.format(order_no=order.order_no), {})
        self.assertEqual(cleared.json()["code"], 0, cleared.json())
        thrown = self.staff.post(THROW_URL.format(order_no=order.order_no), {})
        self.assertEqual(thrown.json()["code"], 0, thrown.json())
        self.assertEqual(Order.objects.get(pk=order.pk).status, OrderStatus.WAITING_GRAB)
        self._claim(order, user=self.taker)
        snaps = self._snapshots()
        self.assertEqual(len(snaps), 2)
        self.assertEqual(
            {s.exit_kind for s in snaps}, {HallExitSnapshot.ExitKind.CLAIMED}
        )
        # 两条快照都指向同一张单，且 `exited_at` 可分辨先后（灰区排序的输入）。
        self.assertEqual({s.order_id for s in snaps}, {order.pk})
        self.assertLessEqual(snaps[0].exited_at, snaps[1].exited_at)


class SnapshotPayloadTests(_SnapshotFixture):
    """payload 的三条覆写判据（在厅视图）。"""

    def setUp(self):
        super().setUp()
        self.taker = User.objects.create_user(username="snapshot-payload-taker")
        StaffProfile.objects.create(user=self.taker).games.set([self.game])

    def test_the_payload_keys_exactly_match_the_hall_serializer(self):
        """payload 键集与 `HallListSerializer` 输出**恰好同形**（对拍网）。"""
        order = self._order(_no(21))
        self._claim(order, user=self.taker)
        snap = self._snapshots().get()
        self.assertEqual(set(snap.payload), set(HallListSerializer.Meta.fields))

    def test_the_payload_keeps_the_in_hall_view(self):
        """status / taker_name / hall_price 三条覆写（转换之后的实例不带在厅视图）。"""
        order = self._order(_no(22), price="88.00", taker_price="500.00")
        self._claim(order, user=self.taker)
        snap = self._snapshots().get()
        self.assertEqual(snap.payload["status"], str(OrderStatus.WAITING_GRAB))
        self.assertIsNone(snap.payload["taker_name"])
        # ⭐ 覆盖价 500 是接管后的结算口径；在厅视图就是抛单价 88。
        self.assertEqual(Decimal(snap.payload["hall_price"]), Decimal("88.00"))

    def test_the_payload_survives_a_null_hall_price(self):
        """没抛价的在厅单（合法状态）离厅 ⇒ payload 的 `hall_price` 是 `None`（不伪装 0）。

        ⚠️ 走软删那条写点（claim 会先被「NULL 接单价」那支限制拒——Ruling 49）。
        """
        order = self._order(_no(23), price=None)
        soft_delete_order(order=order, actor=self.staff_user, is_del_emp="N")
        snap = self._snapshots().get()
        self.assertIsNone(snap.payload["hall_price"])


class MirrorExitSnapshotTests(TenantTestCase):
    """镜像行的写点：claiming / gone / 救回。"""

    def setUp(self):
        self.game = Game.objects.create(name="镜像快照游戏")
        self.owner_user = User.objects.create_user(
            username="snapshot-up-owner", password=PASSWORD
        )
        # 接单的是**另一个人**：转正建单时 `sales_user` = 绑定客服，同一个人不能再当
        # 该单的打手（`(order, user)` 唯一约束）。
        self.taker_user = User.objects.create_user(
            username="snapshot-up-taker", password=PASSWORD
        )
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.owner_user, jidanpu_base_url=BASE_URL, account="acc-snapshot",
            password=PASSWORD, enabled=True,
        )
        self.mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id="20261024000000091111",
            owner_binding=self.binding,
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game, project_text="镜像合成项目",
            remote_rec_fee=Decimal("88.00"),
            remote_game_server="微信区",
            upstream_payload={"orderId": "20261024000000091111", "orderIdView": "V-91111",
                              "recFee": "88.00"},
            first_seen_at=timezone.now() - timedelta(hours=1),
        )

    # ── 接单（listed → claiming → promoted）────────────────────────────────

    def test_claiming_a_mirror_writes_one_claimed_snapshot_and_promotion_does_not_duplicate(self):
        """`listed → claiming` 恰 1 条 `claimed`；随后转正**不重复写**（同一次请求内）。

        变异方向：把 `promote_upstream_mirror` 那条「不写」的约束去掉、在转正处也写
        一条 ⇒ 本用例红（1 ≠ 2，一单双卡正是它要防的）。
        """
        stub = _StubClient()
        with mock.patch.object(jidanpu, "claim_session", lambda _b: _Ctx(stub)):
            order = jidanpu_upstream.claim_upstream_mirror(
                user_id=self.taker_user.pk, mirror_id=self.mirror.pk,
                actor="snapshot-up-taker", actor_id=self.taker_user.pk,
            )
        self.assertIsNotNone(order)
        snaps = list(HallExitSnapshot.objects.all())
        self.assertEqual(len(snaps), 1)
        snap = snaps[0]
        self.assertEqual(snap.source, HallExitSnapshot.Source.UPSTREAM)
        self.assertEqual(snap.exit_kind, HallExitSnapshot.ExitKind.CLAIMED)
        self.assertEqual(snap.mirror_id, self.mirror.pk)
        self.assertIsNone(snap.order_id)
        self.assertEqual(snap.game_id, self.game.pk)
        # payload = 在厅视图（镜像行的投影 + 勾兑价）。⚠️ 镜像行的展示号在
        # `remote_order_view`（`order_no` 键保持基线空串——既有投影形状，不因快照改）。
        self.assertEqual(snap.payload["status"], str(OrderStatus.WAITING_GRAB))
        self.assertEqual(snap.payload["remote_order_view"], "V-91111")
        self.assertEqual(snap.payload["order_no"], "")
        self.assertEqual(Decimal(snap.payload["hall_price"]), Decimal("88.00"))
        # 转正成功（行已 promoted）⇒ 快照仍然恰好一条。
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)
        self.assertEqual(HallExitSnapshot.objects.count(), 1)

    def test_the_mirror_payload_created_at_is_a_project_timezone_string(self):
        """D-0302（2026-10-24）：镜像快照 payload 的 `created_at` 是**项目时区** ISO 串。

        写点把 `serialize_upstream_row` 的投影固化进 JSONField；`remote_created_at` 钉在
        UTC 04:30 ⇒ payload 里是北京墙钟 12:30（+08:00）。旧写法裸 `.isoformat()` 出 UTC 串，
        灰卡行透传给大厅时与原生行（+08:00）混屏差 8 小时。
        """
        from apps.business import hall_snapshot

        JidanpuUpstreamHallOrder.objects.filter(pk=self.mirror.pk).update(
            remote_created_at=datetime(2026, 10, 24, 4, 30, tzinfo=dt_timezone.utc),
        )
        self.mirror.refresh_from_db()
        snap = hall_snapshot.record_mirror_exit(
            self.mirror, exit_kind=HallExitSnapshot.ExitKind.CLAIMED,
        )
        self.assertEqual(snap.payload["created_at"], "2026-10-24T12:30:00+08:00")

    def test_the_gray_read_side_tolerates_old_utc_and_new_local_payloads(self):
        """**存量混合窗口**：payload 里旧 UTC 串与新 +08:00 串并存时，读侧原样透出、都不炸。

        旧行不做迁移（D-0302 裁定：7 天保留期自愈）⇒ 读侧（`snapshot_row`）不做任何
        时区换算。本用例钉「两个形状都原样进行字典、不抛异常、不被改写」。
        """
        from apps.business import hall_preferred
        from apps.business.models import HallExitSnapshot

        old_shape = HallExitSnapshot.objects.create(
            order=None, mirror=self.mirror,
            source=HallExitSnapshot.Source.UPSTREAM,
            exit_kind=HallExitSnapshot.ExitKind.WITHDRAWN,
            game=self.game,
            payload={"created_at": "2026-10-20T04:30:00+00:00", "hall_price": "88.00"},
            exited_at=timezone.now() - timedelta(days=1),
        )
        new_shape = HallExitSnapshot.objects.create(
            order=None, mirror=self.mirror,
            source=HallExitSnapshot.Source.UPSTREAM,
            exit_kind=HallExitSnapshot.ExitKind.CLAIMED,
            game=self.game,
            payload={"created_at": "2026-10-24T12:30:00+08:00", "hall_price": "88.00"},
            exited_at=timezone.now(),
        )
        old_row = hall_preferred.snapshot_row(old_shape, can_view_price=True)
        new_row = hall_preferred.snapshot_row(new_shape, can_view_price=True)
        # 原样透出：不做换算、不炸、不伪装成统一形状。
        self.assertEqual(old_row["created_at"], "2026-10-20T04:30:00+00:00")
        self.assertEqual(new_row["created_at"], "2026-10-24T12:30:00+08:00")

    # ── 消失（listed → gone）───────────────────────────────────────────────

    def test_mark_gone_writes_one_withdrawn_snapshot(self):
        """`_mark_gone`（原状态 listed，条件更新保证）⇒ 恰 1 条 `withdrawn`。"""
        now = timezone.now()
        # ⭐ D-0366：复核原语 = 该游戏段**一跳新鲜段查**（key1 单查已随 E-0314 删除）。
        # 桩回「count=0 的完整空段」⇒ 复核仍不见 ⇒ 标 gone。
        with mock.patch.object(
            jidanpu_upstream, "_pull_all_pages_detail",
            return_value=([], 0, False, False),
        ):
            gone = jidanpu_upstream._mark_gone(
                binding=self.binding, client=_StubClient(), seen=set(), now=now,
            )
        self.assertEqual(gone, 1)
        snaps = list(HallExitSnapshot.objects.all())
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].exit_kind, HallExitSnapshot.ExitKind.WITHDRAWN)
        self.assertEqual(snaps[0].mirror_id, self.mirror.pk)

    def test_mark_gone_skips_rows_that_are_not_listed(self):
        """否定面：`claiming → gone`（`_claim_refused` 那一支的形状）**不写**——
        它离开大厅的时刻在 claiming 之前那次已写过。"""
        self.mirror.status = JidanpuUpstreamHallOrder.Status.CLAIMING
        self.mirror.save(update_fields=["status"])
        now = timezone.now()
        with mock.patch.object(
            jidanpu_upstream, "_pull_all_pages_detail",
            return_value=([], 0, False, False),
        ):
            gone = jidanpu_upstream._mark_gone(
                binding=self.binding, client=_StubClient(), seen=set(), now=now,
            )
        self.assertEqual(gone, 0)
        self.assertEqual(HallExitSnapshot.objects.count(), 0)

    # ── 救回（gone → listed）⇒ 清快照 ──────────────────────────────────────

    def test_rescuing_a_mirror_clears_its_snapshots(self):
        """救回命令复活 gone 行 ⇒ 该镜像的快照被**删除**（防一单双卡）。

        变异方向：把 `rescue_gone_mirrors` 里那段 `clear_mirror_snapshots` 删掉 ⇒
        本用例红（快照残留）。
        """
        # 先造「已判 gone + 有一条 claimed 快照」的现场（快照直接落库——它的写点
        # 在 claim 路径上，这里只关心救回的清理面）。
        self.mirror.status = JidanpuUpstreamHallOrder.Status.GONE
        self.mirror.gone_at = timezone.now() - timedelta(hours=1)
        self.mirror.save(update_fields=["status", "gone_at"])
        HallExitSnapshot.objects.create(
            mirror=self.mirror, source=HallExitSnapshot.Source.UPSTREAM,
            exit_kind=HallExitSnapshot.ExitKind.CLAIMED, game=self.game,
            payload={"order_no": "V-91111"}, exited_at=timezone.now() - timedelta(hours=2),
        )
        # 再现台账（救回证据，形状照 test_jidanpu_upstream_rescue 的同款）。
        from apps.business.jidanpu_common import TERMINAL_REAPPEAR_REASON

        from apps.business.models import JidanpuSyncEvent

        JidanpuSyncEvent.objects.create(
            direction="in", kind="upstream.hall_skipped", status="skipped",
            target_type="hall",
            target_ref=f"{BASE_URL}/{self.mirror.upstream_order_id}"[:191],
            reason=TERMINAL_REAPPEAR_REASON,
            payload={"upstream_binding_id": self.binding.pk},
        )
        from io import StringIO

        from django_tenants.utils import connection

        out = StringIO()
        call_command(
            "rescue_gone_mirrors", "--schema", connection.schema_name,
            "--execute", stdout=out,
        )
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertEqual(HallExitSnapshot.objects.count(), 0)


class _StubClient:
    """SDK 边界替身（照 `test_jidanpu_upstream_hall` 的同款：写端点成功码必须 > 0）。"""

    def __init__(self, *, claim_code=1, claim_msg="接单成功，请到「我的订单」里查看订单资料"):
        self.claim_code = claim_code
        self.claim_msg = claim_msg
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {}

    def ensure_login(self):
        pass

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, dict(kwargs)))
        if path == "/OrderAjax/RecOrderByInternal":
            self.last_envelope = {
                "code": self.claim_code, "msg": self.claim_msg, "data": "",
            }
            return ""
        raise AssertionError(f"未预期的端点：{method} {path}")


class _Ctx:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *_exc):
        return False
