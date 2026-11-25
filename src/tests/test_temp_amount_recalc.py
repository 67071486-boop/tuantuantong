"""B1-7：认领 / 指派后**按人重算** `temp_amount`（spec §6.1 的「顺序问题」表；§16.4 #17）。

背景：spec §6.1 写着「**抢单 / 指派**时按该代练员的 `StaffCommission` 重算 `temp_amount`」，
而 P3a 只写了 `user`（当时没有提成读取层）⇒ **录单时的「全额估值」会一直留着**，
客服事后改接单价也不回写。本任务把它补上：**两个入口都要重算**。

⚠️ **为什么两个入口都要有测试**：认领（`claim_order`）与指派（`set_taker`）走的是**不同的写路径**
——抢单必须是**条件更新**（`UPDATE … WHERE user_id IS NULL`，原子性），
没法与指派共用一个 funnel。所以「新增第三个入口时漏掉重算」是真实风险，
判据是：**任一入口的重算被去掉，都必须有测试变红**（变异自证见 Part 2）。

⚠️ 金额断言钉**字面值**（E-0024 的教训）。
"""
from decimal import Decimal
from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.claim import claim_order
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.business.performance import set_taker
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 本文件走到的订单端点门（`OrderViewSet.ORDER_PERMISSIONS`）。
#: `groups` 只建 Group、**不放行** ⇒ 每个真实登录的夹具都要显式声明它要过的门。
RECALC_ORDER_NODES = (
    "order.assign_taker",           # `/assign/`
    "order.submit_payout_price",
    "grab_hall.take_order",         # `/claim/` 与大厅
    "order.view_detail",
)

TAKER_NODES = frozenset({"grab_hall.take_order"})
PERCENT = CommissionMode.ORDER_PERCENT


class _Base(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")

    def _order(self, no, status=OrderStatus.WAITING_GRAB, price="88.00"):
        order = Order.objects.create(
            game=self.game, order_no=no, status=status, hall_price=Decimal(price)
        )
        perf = Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal(price)
        )
        return order, perf

    def _taker(self, username, *, mode=PERCENT, value="20.00", with_profile=True):
        user = User.objects.create_user(username=username)
        if with_profile:
            StaffProfile.objects.create(
                user=user, commission_mode=mode,
                commission_value=Decimal(value) if value is not None else Decimal("0.00"),
            )
        return user


class AssignRecomputeTests(_Base):
    """/assign 这一路（`performance.set_taker`）。"""

    def test_assign_recomputes_the_estimate_for_the_taker(self):
        """`88.00 × 20 / 100 = 17.60`——**按人**算，不再是录单时的全额估值。"""
        order, _ = self._order("20260920000000000001", status=OrderStatus.UNASSIGNED)
        taker = self._taker("taker_assign")
        client = make_logged_in_client(
            username="cs1", groups=("客服",), nodes=RECALC_ORDER_NODES
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。发请求的客服要经
        # `get_object()`（被 `game_scope` 收窄的 queryset）读到 `self.game` 名下这张单
        # ⇒ 缺游戏权限会拿 `1002`。被指派的 `taker_assign` 不登录、不发请求，不补。
        # 该用例的期望值一个字没改。
        # ⭐ D-0252改准：被指派的 `taker_assign` 现在**也要**持游戏；期望值照旧。
        grant_games(User.objects.get(username="cs1"), self.game)
        grant_games(taker, self.game)
        body = client.post(
            f"/api/business/orders/{order.order_no}/assign/", {"user_id": taker.id},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        perf = Performance.objects.get(order=order)
        self.assertEqual(perf.temp_amount, Decimal("17.60"))

    def test_assign_replaces_a_stale_estimate(self):
        order, perf = self._order("20260920000000000002", status=OrderStatus.UNASSIGNED)
        perf.temp_amount = Decimal("5.00")          # 一个明显过期的估值
        perf.save(update_fields=["temp_amount"])
        taker = self._taker("taker_stale")
        client = make_logged_in_client(
            username="cs2", groups=("客服",), nodes=RECALC_ORDER_NODES
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。发请求的客服同样要
        # 过 `get_object()` 的 `game_scope`（缺权限 ⇒ `1002`，指派根本执行不到）。
        # 被指派的 `taker_stale` 不登录、不发请求，不补。该用例的期望值一个字没改。
        # ⭐ D-0252改准：被指派的目标现在**也要**持游戏（「无档案=无游戏=拒」），
        # 上面那句「被指派的不补」就目标方而言过期；期望值照旧一个字没改。
        grant_games(User.objects.get(username="cs2"), self.game)
        grant_games(taker, self.game)
        client.post(
            f"/api/business/orders/{order.order_no}/assign/", {"user_id": taker.id},
            content_type="application/json",
        )
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("17.60"))

    def test_recompute_falls_back_to_full_without_a_profile(self):
        """无档案 ⇒ 提成回落全额——⭐ D-0252（2026-10-18）起**改道**：`/assign/` 咽喉现在会先被
        游戏范围闸拒掉（**无档案 = 无游戏 = 拒**），「无档案被指派」这条 HTTP 路径再也走不到重算；
        而**回落分支本身还活着**（机器接手 / 镜像单等不经指派闸的路仍然依赖它）⇒ 本用例改调
        `/assign/` 底下同一个重算原语（`performance.set_taker`）直达，钉的还是那条分支。
        原 `/assign/` 版本（2026-09-23 夹具补齐轮写的）按「原文在提交历史」惯例退役。"""
        order, _ = self._order("20260920000000000003", status=OrderStatus.UNASSIGNED)
        taker = self._taker("taker_no_profile", with_profile=False)
        # 构造物：该打手确实没有档案（本用例的前提）——⛔ 不要给它补 `grant_games`。
        self.assertFalse(StaffProfile.objects.filter(user=taker).exists())
        perf = Performance.objects.get(order=order)
        set_taker(perf, taker)
        self.assertEqual(Performance.objects.get(order=order).temp_amount, Decimal("88.00"))

    def test_assign_with_an_uncomputable_mode_is_a_business_code_and_rolls_back(self):
        """提成模式不可算 ⇒ **业务码**，且**整个指派回滚**（不能留下「人了但金额没算」的半成品）。"""
        order, _ = self._order("20260920000000000004", status=OrderStatus.UNASSIGNED)
        taker = self._taker("taker_fixed", mode=CommissionMode.FIXED, value="50.00")
        client = make_logged_in_client(
            username="cs4", groups=("客服",), nodes=RECALC_ORDER_NODES
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。发请求的客服要过
        # `get_object()` 的 `game_scope`（缺权限 ⇒ `1002`，那个"不可算"的业务码分支
        # 根本走不到）。被指派的 `taker_fixed` 不登录、不发请求，不补。
        # 该用例的期望值一个字没改。
        # ⭐ D-0252改准：被指派的 `taker_fixed` 现在**也要**持游戏，闸才放行到
        # 「提成不可算 ⇒ 业务码+回滚」那一支；期望值照旧。
        grant_games(User.objects.get(username="cs4"), self.game)
        grant_games(taker, self.game)
        body = client.post(
            f"/api/business/orders/{order.order_no}/assign/", {"user_id": taker.id},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION)
        self.assertIn("固定金额", body["msg"])
        order.refresh_from_db()
        perf = Performance.objects.get(order=order)
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)     # 状态回滚
        self.assertIsNone(perf.user_id)                            # 接单人回滚
        self.assertEqual(perf.temp_amount, Decimal("88.00"))       # 估值没被改


class ClaimRecomputeTests(_Base):
    """/claim 这一路（`claim.claim_order`）——与 /assign **不是**同一条写路径。"""

    def test_claim_recomputes_the_estimate_for_the_taker(self):
        order, _ = self._order("20260920000000000011")
        taker = self._taker("taker_claim", value="20.00")
        claim_order(user_id=taker.id, order_id=order.id, nodes=TAKER_NODES)
        self.assertEqual(Performance.objects.get(order=order).temp_amount, Decimal("17.60"))

    def test_claim_uses_the_claimers_own_rate(self):
        """换一个人、换一个比例 ⇒ 值跟着换（证明比例来自**接单人**而不是常量）。"""
        order, _ = self._order("20260920000000000012")
        taker = self._taker("taker_half", value="50.00")
        claim_order(user_id=taker.id, order_id=order.id, nodes=TAKER_NODES)
        self.assertEqual(Performance.objects.get(order=order).temp_amount, Decimal("44.00"))

    def test_claim_falls_back_to_full_without_a_profile(self):
        order, _ = self._order("20260920000000000013")
        taker = self._taker("taker_claim_np", with_profile=False)
        claim_order(user_id=taker.id, order_id=order.id, nodes=TAKER_NODES)
        self.assertEqual(Performance.objects.get(order=order).temp_amount, Decimal("88.00"))


class ClaimApiRecomputeRollbackTests(_Base):
    """`/claim` **端点**上的回滚：抢单是条件更新 ⇒「绩效已写人、但重算失败」必须整体回滚。"""

    def test_claim_with_an_uncomputable_mode_rolls_back_the_conditional_update(self):
        order, _ = self._order("20260920000000000021")
        # ⚠️ 抢单的人**必须**用真实登录夹具建（`make_logged_in_client` 不接受已存在的用户名），
        #    档案在登录之后再补——`has_perm`/查询都是实时查库，不影响本用例。
        client = make_logged_in_client(
            username="taker_claim_fixed", groups=("代练员",),
            nodes=("grab_hall.take_order",),
            # ⚠️ 显式 opt-out（默认值翻面）：下面自己建带提成率的档案 ⇒
            # 夹具必须**不建**，否则撞唯一约束；游戏由下面那句 `grant_games` 逐款补。
            games=None,
        )
        taker = User.objects.get(username="taker_claim_fixed")
        StaffProfile.objects.create(
            user=taker, commission_mode=CommissionMode.FIXED,
            commission_value=Decimal("50.00"),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。抢单的账号既是发请求
        # 的一方，也要经 `get_object()`（被 `game_scope` 收窄的 queryset）读 `self.game`
        # 名下这张单 ⇒ 缺游戏权限会拿 `1002`，那个"不可算"的业务码分支根本走不到。
        # `grant_games` 是 `get_or_create`，复用上面那行已建好的档案（并集语义，不抹掉模式）。
        # 该用例的期望值一个字没改。
        grant_games(taker, self.game)
        body = client.post(
            f"/api/business/hall/{order.order_no}/claim/", {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION)
        self.assertIn("固定金额", body["msg"])
        order.refresh_from_db()
        perf = Performance.objects.get(order=order)
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)   # 订单还在大厅
        self.assertIsNone(perf.user_id)                            # 条件更新被回滚
        self.assertEqual(perf.temp_amount, Decimal("88.00"))
