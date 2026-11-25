"""下游鸡蛋铺线的不变式引擎（Task 4）：期望形态三档 + 对账差集（**纯函数**）。

被钉住的契约（spec §5.1 的不变式表 / §5.2 的一轮对账 / §5.4 的去重）：

- `expected_shape(order, binding)`：**先判「是不是他的」**（我方 MAIN 绩效行的接手人），
  **再判「在不在我方大厅」**——两支各有能把它弄红的用例；
- `orders_of(binding)`：**queryset 过滤**（`game_id__in=…`），⛔ 不是把全表拉进 Python 再筛；
- `reconcile(binding, *, now)`：只比**本地 `in_hall` 标记**，⛔ 不逐张问上游、⛔ 一行都不写；
- `dedupe_owner(order, bindings)`：同地址重叠 ⇒ 只由 `id` 最小（最早绑定）的那份推。

⚠️ 期望形态一律写**字面值** `"hall"` / `"order_only"` / `"absent"`，⛔ 不从被测模块导入常量
（`AGENTS.md` §5-G / E-0024：自指断言在被断言的常量被改坏时不会红）；常量本身另有
一条测试钉住它 = 接口契约（Task 5 按这三个字面值消费）。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `member-a`、
密码 `pw-a`。⛔ 本模块零网络、零事件写入。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db.models import QuerySet
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business.jidanpu_member import (
    SHAPE_ABSENT,
    SHAPE_HALL,
    SHAPE_ORDER_ONLY,
    dedupe_owner,
    expected_shape,
    orders_of,
    queue_hall_launch,
    reconcile,
)
from apps.business.models import (
    Approval,
    ApprovalKind,
    ApprovalStatus,
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
    Performance,
)

BASE_URL = "https://jidanpu-a.test.invalid"
BASE_URL_B = "https://jidanpu-b.test.invalid"

#: 「我方别的打手」——用来把「不是他接的」这一支与「没人接」这一支分开。
OTHER_USERNAME = "member-b"


# ── 夹具（合成值）──────────────────────────────────────────────────────


def _user(username):
    return User.objects.create_user(username=username, password="pw-a")


def _scope(user, *games):
    """建档案并勾游戏（不勾 ⇒ 空集，严格口径）。"""
    profile = StaffProfile.objects.create(user=user)
    if games:
        profile.games.set(games)
    return profile


def _binding(user, *, base_url=BASE_URL, enabled=True):
    return JidanpuMemberBinding.objects.create(
        user=user,
        jidanpu_base_url=base_url,
        account=f"acc-{user.username}",
        password="pw-a",
        enabled=enabled,
    )


def _openapi_binding(user, *, base_url=BASE_URL_B, enabled=True):
    """一行**和界面真建出来一模一样**的②式绑定（账号密码**都是空的**，E-0202）。

    ⚠️ 与 `_binding` 的差别只有「用哪套凭据」那几列：②式走 `client_id` / `client_secret`，
    ⛔ **不塞假密码**（塞了就会绕开「②式没有账号密码」这个真实形态）。
    """
    return JidanpuMemberBinding.objects.create(
        user=user,
        jidanpu_base_url=base_url,
        account="",
        password="",
        mode="openapi",
        client_id="cid-synthetic",
        client_secret="secret-synthetic",
        enabled=enabled,
    )


class _EngineTestCase(TenantTestCase):
    def setUp(self):
        super().setUp()
        self._seq = 0
        self.game_a = Game.objects.create(name="合成游戏甲")
        self.game_b = Game.objects.create(name="合成游戏乙")
        self.me = _user("member-a")
        _scope(self.me, self.game_a)
        self.other = _user(OTHER_USERNAME)
        _scope(self.other, self.game_a)
        self.binding = _binding(self.me)

    def _no(self):
        self._seq += 1
        return f"2026092600000000{self._seq:04d}"

    def _order(self, game, *, status=OrderStatus.UNASSIGNED):
        return Order.objects.create(game=game, order_no=self._no(), status=status)

    def _hall_order(self, game):
        return self._order(game, status=OrderStatus.WAITING_GRAB)

    def _taken(self, order, user):
        """把这张单的接手人写成 `user`（我方「谁接了」= MAIN 绩效行的 `user`）。"""
        return Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=user, temp_amount=Decimal("88.00"),
        )

    def _taker_row_without_user(self, order):
        """池内形态：MAIN 行在、`user` 为空（D-0032）。"""
        return Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00"),
        )

    def _sales_row(self, order, user):
        """客服的「销售提成」行（D-0011：与 MAIN 行并存）——让订单 JOIN 出多行。"""
        return Performance.objects.create(
            order=order, emp_type=EmpType.SALES, user=user, temp_amount=Decimal("5.00"),
        )

    def _link(self, binding, order, *, in_hall, withdrawn_at=None):
        return JidanpuMemberOrderLink.objects.create(
            binding=binding,
            order=order,
            jidanpu_order_id=f"remote-{order.pk}",
            in_hall=in_hall,
            #: ⭐ 加的那一列（「这张单在这一份绑定上**上一次退成**是什么时候」）：
            #: 默认 `None` = 从没退成过（既有用例的行为一个字不变）。
            withdrawn_at=withdrawn_at,
        )


# ── ① 期望形态的三档（spec §5.1 的表）───────────────────────────────────


class ExpectedShapeTests(_EngineTestCase):
    def test_shape_constants_are_the_wire_values(self):
        """三档的字面值就是 Task 5 消费的契约（⛔ 改名会静默换掉整个下游的语义）。"""
        self.assertEqual(SHAPE_HALL, "hall")
        self.assertEqual(SHAPE_ORDER_ONLY, "order_only")
        self.assertEqual(SHAPE_ABSENT, "absent")

    def test_expected_shape_three_buckets(self):
        """三档各一条：大厅 / 指派给他 / 我方别的打手接了。"""
        in_hall = self._hall_order(self.game_a)
        self._taker_row_without_user(in_hall)
        assigned_to_me = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(assigned_to_me, self.me)
        taken_by_someone_else = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(taken_by_someone_else, self.other)

        cases = [
            (in_hall, "hall"),
            (assigned_to_me, "order_only"),
            (taken_by_someone_else, "absent"),
        ]
        for order, want in cases:
            with self.subTest(order_no=order.order_no, want=want):
                self.assertEqual(expected_shape(order, self.binding), want)

    def test_hall_order_without_any_main_row_is_hall(self):
        """⚠️ MAIN 行**可能整条不存在**（业务方 2026-09-26 允许删无主 MAIN 行，E-0148）。

        容错读缺行 ⇒ 仍然算「大厅里挂着」；⛔ 用会抛的 `main_performance_of` 会在这里炸。
        """
        order = self._hall_order(self.game_a)
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).exists()
        )  # 构造物：真的没有那条行
        self.assertEqual(expected_shape(order, self.binding), "hall")

    def test_assigned_to_him_while_still_in_hall_state_is_order_only(self):
        """⭐ 判据**顺序**的钉子：先判「是不是他的」，再判「在不在大厅」。

        同一张单不可能既挂在大厅又已有接手人（仓里「进池 ⇔ 无人」的同一条不变量），
        但代码要显式按这个顺序表达 ⇒ 一张「状态还是待抢单、MAIN 行已是他」的单落在
        `order_only`。若把大厅那一支提到前面 ⇒ 本用例红。
        """
        order = self._hall_order(self.game_a)
        self._taken(order, self.me)
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)  # 构造物
        self.assertEqual(expected_shape(order, self.binding), "order_only")

    def test_hall_order_outside_his_games_is_absent(self):
        """范围外的大厅单 ⇒ `absent`（⛔ 不推给这个代练员）。"""
        order = self._hall_order(self.game_b)
        self._taker_row_without_user(order)
        self.assertEqual(expected_shape(order, self.binding), "absent")

    def test_assigned_order_outside_his_games_is_still_order_only(self):
        """**指派**比游戏范围强：不在他游戏范围内、但指派给了他 ⇒ 仍要留在对方订单列表里。

        若把范围判据提到「是不是他的」之前 ⇒ 本用例红（会得到 `absent`）。
        """
        order = self._order(self.game_b, status=OrderStatus.IN_PROGRESS)
        self._taken(order, self.me)
        self.assertEqual(expected_shape(order, self.binding), "order_only")

    def test_unassigned_without_taker_is_absent(self):
        """没人接手、也不在大厅（未分配）⇒ `absent`。"""
        order = self._order(self.game_a)
        self._taker_row_without_user(order)
        self.assertEqual(expected_shape(order, self.binding), "absent")

    def test_completed_order_assigned_to_him_stays_order_only(self):
        """终态（已完成）也仍该留在对方系统里（spec §5.2 的「含终态」）。"""
        order = self._order(self.game_a, status=OrderStatus.COMPLETED)
        self._taken(order, self.me)
        self.assertEqual(expected_shape(order, self.binding), "order_only")

    def test_soft_deleted_order_assigned_to_him_is_absent(self):
        """已删除 ⇒ 对方不该再有这张单（spec §5.1 第三行的「已删除」）。

        若去掉删除判据 ⇒ 本用例红（会得到 `order_only`，那张单永远退不掉）。
        """
        order = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(order, self.me)
        order.deleted_at = timezone.now()
        order.save(update_fields=["deleted_at"])
        self.assertEqual(expected_shape(order, self.binding), "absent")


# ── ② 归属该代练员的单（queryset 过滤）──────────────────────────────────


class OrdersOfTests(_EngineTestCase):
    def test_orders_of_takes_hall_orders_in_his_games_and_all_his_orders(self):
        """① MAIN 行是他的单（**含终态**）∪ ② 大厅里他范围内的单。

        ⚠️ 大厅那张单**同时挂了两条绩效行**（MAIN 无主 + 客服提成）⇒ 查询会 JOIN 出**两行**：
        断言用 `sorted(...)` 而**不是** `set(...)`——集合会把重复悄悄吃掉，这条就再也接不住
        「忘了 `distinct()`」那种变异（`AGENTS.md` §5-G：比对前归一化会把判据变恒真）。
        """
        hall_mine = self._hall_order(self.game_a)
        self._taker_row_without_user(hall_mine)
        self._sales_row(hall_mine, self.other)
        assigned = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(assigned, self.me)
        completed = self._order(self.game_a, status=OrderStatus.COMPLETED)
        self._taken(completed, self.me)
        refunded = self._order(self.game_a, status=OrderStatus.REFUNDED)
        self._taken(refunded, self.me)

        found = list(orders_of(self.binding).values_list("pk", flat=True))
        self.assertEqual(sorted(found), sorted([hall_mine.pk, assigned.pk, completed.pk, refunded.pk]))

    def test_orders_of_excludes_out_of_scope_and_other_takers(self):
        """反面：范围外的大厅单、别人接的单**确实**不在结果里（不是「没造出来」）。"""
        self._hall_order(self.game_b)  # 范围外的大厅单
        other_took = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(other_took, self.other)
        mine = self._hall_order(self.game_a)

        found = list(orders_of(self.binding).values_list("pk", flat=True))
        self.assertEqual(found, [mine.pk])

    def test_orders_of_filters_the_games_in_sql(self):
        """⚠️ 范围收窄必须发生在**查询里**（`game_id__in=games_of(...)` 的子查询）。

        「把全表拉进 Python 再筛」在当前夹具下结果一样（行为看不出差别），所以这里直接钉
        **编译出来的 SQL 引用了 `business_game`**：只有把 `games_of(...)` 当子查询交给数据库，
        那个表才会出现在订单的查询里。⛔ 这不是自指断言——把过滤挪回 Python，SQL 里就没有它。
        """
        queryset = orders_of(self.binding)
        self.assertIsInstance(queryset, QuerySet)
        self.assertIn("business_game", str(queryset.query))

    def test_orders_of_hides_soft_deleted_orders(self):
        """软删的大厅单不在「归属他」的集合里（退单靠链路那一侧兜，见 reconcile）。"""
        deleted = self._hall_order(self.game_a)
        deleted.deleted_at = timezone.now()
        deleted.save(update_fields=["deleted_at"])
        self.assertEqual(list(orders_of(self.binding)), [])


# ── ③ 一轮对账的差集（spec §5.2）──────────────────────────────────────



    def test_orders_of_excludes_hall_shadow_orders(self):
        """⭐ 评审 Important 7：对账候选集不含隐藏单（它的生命周期归 `hall_shadow` 的钩子）。"""
        from tests.hall_shadow_helpers import make_hidden_order, make_mirror

        normal = self._hall_order(self.game_a)
        self._taker_row_without_user(normal)
        card = make_mirror(game=self.game_a, bound_by=self.me, no="SYN-SHADOW-ENGINE")
        hidden = make_hidden_order(
            game=self.game_a, card=card, order_no=self._no(), with_main=True
        )
        found = list(orders_of(self.binding).values_list("pk", flat=True))
        self.assertIn(normal.pk, found)                    # 正对照
        self.assertNotIn(hidden.pk, found)


class ReconcileTests(_EngineTestCase):
    NOW = timezone.now()

    def _groups(self, binding=None):
        return reconcile(binding or self.binding, now=self.NOW)

    def test_hall_without_any_link_goes_to_launch(self):
        """期望大厅、本地没有链路 ⇒ 待录单 + 抛单。"""
        order = self._hall_order(self.game_a)
        result = self._groups()
        self.assertEqual([item.order.pk for item in result.to_launch], [order.pk])
        self.assertIn("录单", result.to_launch[0].reason)
        self.assertEqual(result.to_withdraw, ())

    def test_penalty_in_local_hall_never_enters_downstream_hall(self):
        penalty = self._hall_order(self.game_a)
        penalty.penalty_approval = Approval.objects.create(
            kind=ApprovalKind.PENALTY, status=ApprovalStatus.APPROVED,
            target=self.binding.user, amount=Decimal("30.00"),
        )
        penalty.hall_price = Decimal("-30.00")
        penalty.save(update_fields=["hall_price", "penalty_approval"])
        normal = self._hall_order(self.game_a)
        self.assertEqual(queue_hall_launch(penalty), 0)
        self.assertEqual([item.order.pk for item in self._groups().to_launch], [normal.pk])
        self.assertFalse(JidanpuSyncEvent.objects.filter(target_ref=penalty.order_no).exists())

    def test_hall_with_local_link_not_in_hall_goes_to_launch(self):
        """本地链路标着「不在对方大厅」⇒ 仍要补一次（⛔ 不是「有链路就跳过」）。"""
        order = self._hall_order(self.game_a)
        self._link(self.binding, order, in_hall=False)
        result = self._groups()
        self.assertEqual([item.order.pk for item in result.to_launch], [order.pk])
        self.assertIn("补", result.to_launch[0].reason)

    def test_a_takerless_in_progress_order_is_never_withdrawn(self):
        """⭐ 2026-09-27 真机（业务方报的环）：**没有接手人 ≠「对方不该留着这张单」**。

        真机现场：对方打手接了我方抛过去的单 ⇒ 我方自动接手因为**这个人在这张单上已经有绩效行**
        （他既是这张单的客服、又是绑定的代练员 ⇒ DB 约束 `uniq_performance_order_user`）而失败
        ⇒ 慢路把状态推成「代练中」却**没有接手人** ⇒ 对账按「没人接的单对方不该留着」
        去**退单**，把对方正在做的那张单退掉了（对方那边从「代练中」变成「已退单」）。

        判据：状态已越过「在大厅」的单，**没有接手人也一律留着**——⛔ 一条退单都不排。
        ⚠️ 本地标记 `in_hall` 可能是**过期的**（对方打手接走之后快路不改它）⇒ 引擎仍会算一条
        `to_unlist`，而**消费者**「先读一遍、确认还在大厅才写」那一步保证不会对着已被接走的单
        发撤销（见 `test_jidanpu_member_consume.UnlistConsumeTests`）。
        判别力：去掉 `NO_TAKER_KEEP_STATUSES` 那条守卫 ⇒ 本条红（`to_withdraw` 里出现它）。
        """
        order = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taker_row_without_user(order)          # MAIN 行在、`user` 为空（真机那个状态）
        self._link(self.binding, order, in_hall=True)  # 对方那张单正被他的打手做着
        result = self._groups()
        self.assertEqual(
            (result.to_launch, result.to_withdraw, result.to_record), ((), (), ())
        )
        self.assertEqual([item.order.pk for item in result.to_unlist], [order.pk])

    def test_the_same_holds_for_review_and_completed(self):
        """同一条守卫覆盖「待验收 / 已完成」（都意味着已经有人在推进这张单）。"""
        for status in (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED):
            with self.subTest(status=status):
                order = self._order(self.game_a, status=status)
                self._taker_row_without_user(order)
                self.assertEqual(expected_shape(order, self.binding), SHAPE_ORDER_ONLY)

    def test_hall_with_local_link_in_hall_needs_nothing(self):
        order = self._hall_order(self.game_a)
        self._link(self.binding, order, in_hall=True)
        result = self._groups()
        self.assertEqual(
            (result.to_launch, result.to_withdraw, result.to_record), ((), (), ())
        )

    def test_assigned_with_hall_link_goes_to_unlist(self):
        """⭐ 业务方 2026-09-28：期望「只在他家订单列表」、本地链路却标着还在大厅 ⇒ **撤下**。

        业务方报的错：我方打手在大厅接了之后，下家那张单被从抢单大厅拉回、并且直接变成
        「代练中」⇒ 要的是**停在「未分配」**（不在他们的抢单大厅、也不指派给他们任何人）。
        ⚠️ 本线早先因为「上游写端点载荷未实测」而**取消**了这个动作；现在业务方明确要求 ⇒
        用 SDK 登记里的 `RelesOrderToHomeByOrderID` 把它做出来（路径与载荷见
        `test_jidanpu_member_consume.ReleaseOrderToHomePrimitiveTests`）。
        ⛔ **不是退单**：`to_withdraw` 必须仍然是空的（退单会把这张单在对方系统里作废）。
        判别力：删掉 `to_unlist` 那一支 ⇒ 本用例红。
        """
        order = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(order, self.me)
        self._link(self.binding, order, in_hall=True)
        result = self._groups()
        self.assertEqual([item.order.pk for item in result.to_unlist], [order.pk])
        self.assertTrue(result.to_unlist[0].reason, "撤下也要有人话原因，⛔ 不静默")
        self.assertEqual(
            (result.to_launch, result.to_withdraw, result.to_record), ((), (), ())
        )

    def test_assigned_with_link_not_in_hall_needs_no_unlist(self):
        """本地链路已经标着「不在对方大厅」⇒ 没有可撤下的东西（反向控制）。"""
        order = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(order, self.me)
        self._link(self.binding, order, in_hall=False)
        result = self._groups()
        self.assertEqual(result.to_unlist, ())

    def test_assigned_with_link_not_in_hall_needs_nothing(self):
        order = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(order, self.me)
        self._link(self.binding, order, in_hall=False)
        result = self._groups()
        self.assertEqual(result.to_withdraw, ())
        self.assertEqual(result.to_record, ())

    def test_assigned_without_any_link_goes_to_record_only(self):
        """期望「只在他家订单列表」、本地连链路都没有 ⇒ 待**只录单**（spec §5.1 第二行）。

        ⛔ 不在 `to_launch` 里：那一档会在对方大厅也挂出来，是错的。
        """
        order = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(order, self.me)
        result = self._groups()
        self.assertEqual([item.order.pk for item in result.to_record], [order.pk])
        self.assertEqual(result.to_launch, ())

    def test_taken_by_someone_else_with_link_goes_to_withdraw(self):
        """期望 `absent`、本地还有链路 ⇒ 退单（spec §5.1 第三行）。"""
        order = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(order, self.other)
        self._link(self.binding, order, in_hall=True)
        result = self._groups()
        self.assertEqual([item.order.pk for item in result.to_withdraw], [order.pk])
        self.assertIn("退单", result.to_withdraw[0].reason)

    def test_soft_deleted_order_with_link_goes_to_withdraw(self):
        """已删除的单**只剩链路行**能指认它（订单已不在默认读路径里）⇒ 退单。

        本用例同时钉住：链路行的 `order` 能在软删后仍取到（正向 FK 走 base manager）；
        以及删除的判据在 `expected_shape` 里、`reconcile` 不必另写一份。

        ⚠️ 接单人**故意用外人**（`self.other`，没有绑定）：2026-10-25 起「接手人是这家下家
        自己的账号」那一档**整店不撤**——本用例钉的是「软删 ⇒ 该退」，与那一档无关。
        """
        order = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(order, self.other)
        self._link(self.binding, order, in_hall=True)
        order.deleted_at = timezone.now()
        order.save(update_fields=["deleted_at"])

        result = self._groups()
        self.assertEqual([item.order.pk for item in result.to_withdraw], [order.pk])

    def test_out_of_scope_hall_order_is_not_touched(self):
        """范围外的大厅单：⛔ 一张都不推（范围判据在差集里也生效）。"""
        self._hall_order(self.game_b)
        result = self._groups()
        self.assertEqual(
            (result.to_launch, result.to_withdraw, result.to_record), ((), (), ())
        )

    # ── ⭐⭐ 判据打在「终态」上（真机那个环：同一张单被反复退）──────────

    def test_an_order_that_has_already_been_withdrawn_is_not_queued_again(self):
        """⭐⭐ **主判据（引擎那一半）**：链路标着「**已经退成过**」（`withdrawn_at` 非空）
        ⇒ ⛔ **一张都不进 `to_withdraw`**（旧形态只看 `link is not None` ⇒ 每 180 秒又排一条）。

        真机读数（kexing 租户）：8710 条退单事件只牵涉 185 张单、平均一张被退了
        47 次（其中 47 张 ≥10 次 ⇒ 累计 8276 条 = 95% 是重复排放）。

        ⚠️ **反向控制在同一用例里**（⛔ 不另立一条会被漏跑）：把那个标记清掉 ⇒ 同一条链路
        又该退 ⇒ 少了这一半，一个「`to_withdraw` 恒为空」的实现也能让上面那句绿（E-0024 同族）。
        """
        order = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(order, self.other)                       # 我方别的打手接了 ⇒ 期望 `absent`
        link = self._link(self.binding, order, in_hall=False, withdrawn_at=self.NOW)

        self.assertEqual(self._groups().to_withdraw, (), "退成过的单又被排了一遍")

        # 反向控制：同一条链路、只把「已经退成」那个标记清掉 ⇒ 又该退。
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(withdrawn_at=None)
        result = self._groups()
        self.assertEqual([item.order.pk for item in result.to_withdraw], [order.pk])

    def test_a_restored_order_is_launched_again_rather_than_withdrawn(self):
        """⭐ **可逆性（引擎那一半）**：单被恢复（回池 ⇒ 期望大厅）⇒ 对账排的是
        `to_launch`（⛔ 不是退单）——这就是「已经退成」那个标记**能被回置**的那条路
        （真正清空它的那一跳在消费者 `_consume_launch`，见 `test_jidanpu_member_consume`）。
        """
        order = self._hall_order(self.game_a)
        self._link(self.binding, order, in_hall=False, withdrawn_at=self.NOW)

        result = self._groups()
        self.assertEqual([item.order.pk for item in result.to_launch], [order.pk])
        self.assertEqual(result.to_withdraw, ())

    def test_reconcile_writes_no_rows_and_queues_no_events(self):
        """⛔ **纯函数**：一轮对账一行都不写（事件是 Task 5/6 的事）。"""
        self._hall_order(self.game_a)
        assigned = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(assigned, self.me)
        taken_by_other = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(taken_by_other, self.other)
        self._link(self.binding, taken_by_other, in_hall=True)

        before = (
            Order.objects.count(),
            Performance.objects.count(),
            JidanpuMemberOrderLink.objects.count(),
            JidanpuSyncEvent.objects.count(),
        )
        self._groups()
        after = (
            Order.objects.count(),
            Performance.objects.count(),
            JidanpuMemberOrderLink.objects.count(),
            JidanpuSyncEvent.objects.count(),
        )
        self.assertEqual(before, after)
        self.assertEqual(JidanpuSyncEvent.objects.count(), 0)

    def test_a_linked_hall_shadow_order_is_not_reconciled(self):
        """⭐ 二轮评审 Important 3：**有本地链路**的隐藏单也不进对账（`to_launch` / `to_withdraw`）。

        隐藏单建好就 `queue_hall_launch` 过 ⇒ **天然有链路**。`orders_of` 只排掉了「直接归属
        他」那一支，链路那一支过去会把单加回集合 `orders`：
        * 在厅形态（本用例的 `hall_shadow`）⇒ 进了 `to_launch`（「要补抛」）；
        * `absent` 形态（本用例的 `lost_shadow`）⇒ 进了 `to_withdraw`（「要退单」）。
        两者都与 `hall_shadow` 的钩子把同一条同步动作排两遍（且隐藏单的期望形态不同）。

        ⚠️ 正对照：同一轮里那张**普通**在厅单仍必须进 `to_launch`——否则「全都不排」的实现
        也能让本用例绿（E-0024 同族）。
        """
        from tests.hall_shadow_helpers import make_hidden_order, make_mirror

        normal = self._hall_order(self.game_a)
        self._taker_row_without_user(normal)

        card = make_mirror(game=self.game_a, bound_by=self.me, no="SYN-SHADOW-RECON")
        hall_shadow = make_hidden_order(
            game=self.game_a, card=card, order_no=self._no()
        )
        # 在厅形态 ⇒ 不排除时会进 `to_launch`（`in_hall=False` 那一支：「要补一次抛单」）。
        self._link(self.binding, hall_shadow, in_hall=False)

        other_card = make_mirror(
            game=self.game_a, bound_by=self.me, no="SYN-SHADOW-RECON-2"
        )
        lost_shadow = make_hidden_order(
            game=self.game_a, card=other_card, order_no=self._no()
        )
        # `absent` 形态（我方**别的**打手接了）⇒ 不排除时会进 `to_withdraw`。
        self._taken(lost_shadow, self.other)
        self._link(self.binding, lost_shadow, in_hall=True)

        result = self._groups()

        self.assertEqual(
            [item.order.pk for item in result.to_launch], [normal.pk],
            "普通在厅单该抛（正对照）＋ 隐藏单**不该**抛",
        )
        self.assertEqual(
            [item.order.pk for item in result.to_withdraw], [],
            "隐藏单不该进退单那一档（它的生命周期归 hall_shadow 的钩子）",
        )

    def test_result_carries_the_injected_now(self):
        """注入的时钟进结果（调用方在同一时刻口径下落台账）。"""
        result = self._groups()
        self.assertEqual(result.at, self.NOW)


class OpenApiHallPushTests(_EngineTestCase):
    """⭐⭐ ②式的**期望形态里没有「已有链路 ⇒ 该在对方大厅」这一档**（2026-10-10，开闸前堵的环）。

    为什么：`to_launch` 的期望是「这张单该在对方**抢单大厅**里」，而②式**建单之后没有任何
    接口**能把一张**已经在对方系统里**的单推进他家大厅（`recUser` / `assignPrice` **只在建单
    那一刻**可传）⇒ 对**已经有链路**的单，这个期望在②式下**根本不可达**
    ⇒ 不该进差集（进退差集的后果：每 180 秒排一条注定被终态跳过的 `member.launch`）。

    ⚠️ **夹具**：②式那一行挂在**另一个人**身上、用**另一个下家地址** ——
    `JidanpuMemberBinding.user` 是 OneToOne（一个人只能一行），而同址重叠去重（A12）会把
    这张单判给**更早建的那一行**；不隔开的话，本类测到的就是「跳过」而不是「期望形态」了。
    """

    NOW = timezone.now()

    def setUp(self):
        super().setUp()
        self.openapi = _openapi_binding(self.other, base_url=BASE_URL_B)

    def _groups(self):
        return reconcile(self.openapi, now=self.NOW)

    def test_a_hall_order_with_a_link_off_hall_queues_nothing(self):
        """⭐⭐ **主判据（引擎那一半）**：待抢单（在范围内、没接手人）+ 一条**不在大厅**的链路
        ⇒ ②式下**一档都不排**（⛔ 不是排一条注定失败的 `member.launch`）。

        判别力（**能红**）：把 `reconcile` 里那条收口去掉（= 旧实现）⇒ `to_launch` 里又出现
        这张单 ⇒ 本用例红。
        """
        order = self._hall_order(self.game_a)
        self._link(self.openapi, order, in_hall=False)

        result = self._groups()

        self.assertEqual(result.to_launch, ())
        self.assertEqual(
            (result.to_record, result.to_unlist, result.to_withdraw), ((), (), ()),
        )
        #: ⚠️ 也**不是**「被跳过」那一档（同址重叠裁定）：那一档会带一条 `ReconcileSkip`
        #: ⇒ 本用例测的必须是**期望形态**那条路。
        self.assertEqual(result.skipped, ())

    def test_a_restored_order_queues_nothing_either(self):
        """⭐ 上一轮那个形态的②式对照：**退成过**的单被恢复（回池 ⇒ 期望大厅）⇒
        ②式下同样**排不出来**（推不进已有单的大厅）；⛔ 也**不退单**（那张单对方那边已经退了）。
        """
        order = self._hall_order(self.game_a)
        self._link(self.openapi, order, in_hall=False, withdrawn_at=self.NOW)

        result = self._groups()

        self.assertEqual((result.to_launch, result.to_withdraw), ((), ()))

    def test_a_hall_order_without_any_link_still_goes_to_launch(self):
        """⭐⭐ **不许误伤**：没有链路那一档（＝**该录单**）**照旧**排 `to_launch`
        —— ②式的 `recUser=999999999` 建单那一刻就自动进对方大厅 ⇒ 这个期望**可达**。

        判别力：把收口的条件写成「②式一律不排 `to_launch`」⇒ 本用例红。
        """
        order = self._hall_order(self.game_a)

        result = self._groups()

        self.assertEqual([item.order.pk for item in result.to_launch], [order.pk])

    def test_a_link_already_in_hall_needs_nothing_and_never_queues_an_unlist(self):
        """⛔ **收口的边界**：链路标着**还在大厅** ⇒ 目标态已达成、什么都不排。

        ⚠️ 这一条钉的是收口的**条件**（只在「不在大厅」时才收口）：把它写成无条件收口
        ⇒ 这张单会被判成 `order_only` ⇒ 排一条**撤下**（②式下那不是撤下、是**退单**，
        等于把对方在厅里的那张单毁掉）⇒ 本用例红。
        """
        order = self._hall_order(self.game_a)
        self._link(self.openapi, order, in_hall=True)

        result = self._groups()

        self.assertEqual(
            (result.to_launch, result.to_unlist, result.to_withdraw), ((), (), ()),
        )

    def test_the_password_line_still_queues_the_hall_push(self):
        """⭐ **①式一个字都不许变**：同样形态（待抢单 + 不在大厅的链路）⇒ ①式照旧排一条。

        ⚠️ 它真能把已有单**抛进**对方大厅（`throw_remote` 打真接口 + 回读 `isThrowOrder=="Y"`）
        ⇒ 那个期望在①式下**可达**，⛔ 不许被上面那条收口带走。
        判别力：把收口的条件写成「不看线」（两条线一起收口）⇒ 本用例红。
        """
        order = self._hall_order(self.game_a)
        self._link(self.binding, order, in_hall=False)

        result = reconcile(self.binding, now=self.NOW)

        self.assertEqual([item.order.pk for item in result.to_launch], [order.pk])

    def test_outbox_line_for_picks_each_line_by_mode(self):
        """⭐ 取用口：`outbox_line_for` 按 `mode` 取对应那一份（⛔ 不看 `client_id` 真值）。

        判别力：把它写成「恒回①式那一份」⇒ 本用例红（而那正是那个环的成因：
        ②式那一行会拿①式的**能力面**去算期望）。
        """
        from apps.business import jidanpu_member, jidanpu_member_openapi_outbox

        self.assertIs(
            jidanpu_member.outbox_line_for(self.binding),
            jidanpu_member.member_outbox_line(),
        )
        self.assertIs(
            jidanpu_member.outbox_line_for(self.openapi),
            jidanpu_member_openapi_outbox.member_openapi_outbox_line(),
        )
        #: ⚠️ 判据是 `mode`：切回①式之后 `client_id` **还在**（E-0021 家族）
        #: ⇒ 下面这一行只要还是②式的取值，就说明判据没走 `client_id` 真值那条错路。
        self.assertEqual(self.openapi.mode, "openapi")
        self.assertTrue(self.openapi.client_id)


# ── ③-乙 ⭐「同步大厅订单」开关：对账那一半（D-0299）─────────────────
#
# 口径：`hall_sync_enabled=False` ⇒「这张单该挂进对方抢单大厅」这个期望对这份绑定**不成立**
# ⇒ `to_launch` 一张不排、**也不进 skipped**（那是「归属别的绑定」的裁定，不是「他不参与大厅」）；
# ⛔ **不许**把期望收口成 absent（那会把**已挂对方大厅的存量单**排退单，D-0299 明文不撤回）；
# 其余档（to_record / to_withdraw / to_unlist）一个字不动（下面各一条钉住）。
# ⚠️ 与 `OpenApiHallPushTests` 是**两个独立的闸**：一个按 `mode`（②式推不进已有单的大厅）、
# 一个按用户开关（不想进大厅）——⛔ 互不替代。


class HallSyncOffReconcileTests(_EngineTestCase):
    NOW = timezone.now()

    def setUp(self):
        super().setUp()
        self.binding.hall_sync_enabled = False
        self.binding.save(update_fields=["hall_sync_enabled"])

    def _groups(self):
        return reconcile(self.binding, now=self.NOW)

    def test_hall_order_without_any_link_queues_nothing_and_no_skip(self):
        """待抢单（在范围内、无接手人）+ 没有链路 ⇒ **一档都不排、也不 skip**。

        判别力：`reconcile` 里那条 `hall_sync_enabled` 闸删掉（= 旧实现）⇒ `to_launch`
        里出现这张单，本用例红；闸写成「进 skipped」⇒ 第三条断言红。
        """
        order = self._hall_order(self.game_a)

        result = self._groups()

        self.assertEqual(result.to_launch, ())
        self.assertEqual((result.to_record, result.to_unlist, result.to_withdraw), ((), (), ()))
        self.assertEqual(result.skipped, ())

    def test_link_off_hall_is_neither_relaunched_nor_withdrawn(self):
        """⭐⭐ **存量不撤回**：链路标着「不在大厅」（早先抛出去过、后被接走/撤下）⇒
        不补抛（to_launch）、**更不退单**（to_withdraw）——退单就是「主动撤回存量」，
        D-0299 明文不做。判别力：把闸写成收口成 `absent` ⇒ `to_withdraw` 非空，红。
        """
        order = self._hall_order(self.game_a)
        self._link(self.binding, order, in_hall=False)

        result = self._groups()

        self.assertEqual(
            (result.to_launch, result.to_withdraw, result.to_record, result.to_unlist),
            ((), (), (), ()),
        )

    def test_link_already_in_hall_needs_nothing(self):
        """链路还在大厅 ⇒ 目标态已被达成（存量单原地不动），什么都不排。"""
        order = self._hall_order(self.game_a)
        self._link(self.binding, order, in_hall=True)

        result = self._groups()

        self.assertEqual(
            (result.to_launch, result.to_withdraw, result.to_record, result.to_unlist),
            ((), (), (), ()),
        )

    def test_assigned_to_him_still_goes_to_record(self):
        """**承诺钉子**：已指派给他、没有链路 ⇒ `to_record`（只录单）**照旧**——
        开关闸的是「抛大厅」，不是「录单」。判别力：闸门扩大到 to_record ⇒ 本用例红。
        """
        order = self._order(self.game_a)
        self._taken(order, self.me)

        result = self._groups()

        self.assertEqual(result.to_launch, ())
        self.assertEqual([item.order.pk for item in result.to_record], [order.pk])

    def test_taken_by_someone_else_with_a_link_still_goes_to_withdraw(self):
        """我方**别的**打手接了、本地有链路 ⇒ `to_withdraw`（退单）**照旧**——
        对方不该再留着这张单，与「不主动撤回还在做的大厅存量」是两件事。
        """
        order = self._hall_order(self.game_a)
        self._link(self.binding, order, in_hall=True)
        self._taken(order, self.other)

        result = self._groups()

        self.assertEqual(result.to_launch, ())
        self.assertEqual([item.order.pk for item in result.to_withdraw], [order.pk])

    def test_hall_on_still_launches(self):
        """**对照支**：默认（True）的绑定同样形态照排 `to_launch`——闸不是「一律不排」。

        判别力：把闸写成对所有绑定生效 ⇒ 本用例红。
        """
        self.binding.hall_sync_enabled = True
        self.binding.save(update_fields=["hall_sync_enabled"])
        order = self._hall_order(self.game_a)

        result = self._groups()

        self.assertEqual([item.order.pk for item in result.to_launch], [order.pk])


# ── ④ 重叠去重（spec §5.4 / A12）──────────────────────────────────────


class DedupeOwnerTests(_EngineTestCase):
    def test_earliest_binding_wins(self):
        """同地址、范围重叠 ⇒ 归属 = `id` 最小（最早建的那一行）。"""
        order = self._hall_order(self.game_a)
        earlier = self.binding
        later = _binding(self.other)

        self.assertEqual(dedupe_owner(order, [earlier, later]).pk, earlier.pk)
        self.assertLess(earlier.pk, later.pk)  # 构造物：先建的 pk 确实更小

    def test_earliest_wins_regardless_of_input_order(self):
        """候选序列的顺序不改变结论（⛔ 不是「取第一个命中的」）。"""
        order = self._hall_order(self.game_a)
        earlier = self.binding
        later = _binding(self.other)
        self.assertEqual(dedupe_owner(order, [later, earlier]).pk, earlier.pk)

    def test_assigned_order_belongs_to_the_assignee(self):
        """指派单不受重叠规则影响：归属就是他（spec §11 第 5 条）——哪怕他建得更晚。"""
        order = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(order, self.other)
        earlier = self.binding
        assignee = _binding(self.other)
        owner = dedupe_owner(order, [earlier, assignee])
        self.assertEqual(owner.pk, assignee.pk)
        self.assertGreater(assignee.pk, earlier.pk)  # 构造物：他确实更晚

    def test_nobody_carries_the_order_returns_none(self):
        """谁都不命中（别的打手接了）⇒ `None`（⛔ 不硬塞一个归属）。"""
        order = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(order, self.other)
        third = _user("member-c")
        _scope(third, self.game_a)
        self.assertIsNone(dedupe_owner(order, [self.binding, _binding(third)]))


class ReconcileOverlapTests(_EngineTestCase):
    def test_overlapped_order_launches_only_from_the_earliest_binding(self):
        """⭐ 同地址两个代练员范围重叠 ⇒ 同一条单**只推一次**，另一份**表达**「被跳过」。"""
        order = self._hall_order(self.game_a)
        earliest = self.binding
        later = _binding(self.other)

        first = reconcile(earliest, now=timezone.now())
        self.assertEqual([item.order.pk for item in first.to_launch], [order.pk])
        self.assertEqual(first.skipped, ())

        second = reconcile(later, now=timezone.now())
        self.assertEqual(
            (second.to_launch, second.to_withdraw, second.to_record), ((), (), ())
        )
        self.assertEqual([skip.order.pk for skip in second.skipped], [order.pk])
        self.assertEqual(second.skipped[0].owner.pk, earliest.pk)
        self.assertIn("跳过", second.skipped[0].reason)

    def test_disabled_binding_does_not_hold_the_ownership(self):
        """停用的同地址绑定不会推单 ⇒ ⛔ 不让它占着归属（否则这张单没人推）。

        spec §5.4 只说「最早绑定者推」，没说停用怎么办；这一条取「最早**且会跑**的那份」。
        若候选里带上停用的那份 ⇒ 它会以「更早」赢下归属、而它根本不会跑 ⇒ 本用例红。
        """
        self.binding.enabled = False
        self.binding.save(update_fields=["enabled"])
        order = self._hall_order(self.game_a)
        later_runner = _binding(self.other)  # 更晚建，但会真正跑对账
        self.assertLess(self.binding.pk, later_runner.pk)  # 构造物：停用的那份建得更早

        result = reconcile(later_runner, now=timezone.now())
        self.assertEqual([item.order.pk for item in result.to_launch], [order.pk])
        self.assertEqual(result.skipped, ())

    def test_assigned_order_is_not_skipped_by_an_overlapping_binding(self):
        """指派单走**他自己**那份绑定：同地址的另一份不得把它跳过（spec §11 第 5 条）。"""
        order = self._order(self.game_a, status=OrderStatus.IN_PROGRESS)
        self._taken(order, self.other)
        earlier = self.binding  # 更早建的同地址绑定（属于 self.me，也勾了这张单的游戏）
        assignee = _binding(self.other)  # 接手人自己的绑定，建得更晚
        self.assertLess(earlier.pk, assignee.pk)  # 构造物

        result = reconcile(assignee, now=timezone.now())
        self.assertEqual(result.skipped, ())
        self.assertEqual([item.order.pk for item in result.to_record], [order.pk])
