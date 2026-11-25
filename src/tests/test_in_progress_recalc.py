"""D4a（spec §3.4 / §4.3 / §7.2 #17–#22）：订单状态**变成** `IN_PROGRESS` 时重算预计绩效。

判据一律钉**字面值**（E-0024 的教训：期望值不从被测模块 import 常量——自指断言在
常量被改坏时不会红）。⛔ 本文件的名字 / 单号 / 用户名全是**合成值**（E-0077）。

覆盖的入口（本线**枚举到**的范围，⛔ 不写「所有入口」这类绝对句）：领域层
`apply_transition`（`InProgressRecalcTests`）与通用端点 `POST /orders/<no>/transition/`
（`InProgressRecalcEndpointTests`）；`/review/` 驳回方向那一条在
`tests/test_review_api.py`。⚠️ 鸡蛋铺后台另有两条**直写**路径不经 `apply_transition`
（`jidanpu_hall.py`，spec §4.3 点名），本线**照各自既有「不算钱」口径不动它们**。

⚠️ **`is_settled` 那一支为什么有一条「直接调钩子」的用例**：D4b 的闸
（`order_status.settled_status_change_denial`）排在目标态钩子**之前**、且覆盖**一切**
目标态 ⇒ 经 `apply_transition` 永远先被那道闸拒掉，钩子里的 `is_settled` 支
**结构性不可达**。要证明它存在（而不是装饰），只能直接调它——见
`test_the_settled_guard_in_the_hook_skips_on_its_own` 的 docstring。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.commission import CommissionError
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Notification,
    NotificationKind,
    Order,
    OrderStatus,
    Performance,
    SettleStatus,
)
from apps.business.order_status import IllegalTransition, apply_transition
from apps.core.error_codes import ErrorCode


class InProgressRecalcTests(TenantTestCase):
    """领域层：`apply_transition(..., IN_PROGRESS)` 的副作用。"""

    NODES = {"order.change_status"}

    def setUp(self):
        self.game = Game.objects.create(name="D4a 重算测试游戏")
        self.taker = User.objects.create_user(username="测试打手乙")
        StaffProfile.objects.create(
            user=self.taker, commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )

    def _order(self, no, *, status=OrderStatus.PENDING_REVIEW, hall=Decimal("100.00"),
               taker_price=None, temp=Decimal("1.00"), bonus=Decimal("0.00")):
        order = Order.objects.create(game=self.game, order_no=no, status=status,
                                     total_price=Decimal("200.00"), hall_price=hall)
        perf = Performance.objects.create(
            order=order, user=self.taker, emp_type=EmpType.MAIN,
            temp_amount=temp, bonus_amount=bonus, taker_price=taker_price,
        )
        return order, perf

    def test_a_non_pool_state_back_to_in_progress_recomputes_temp_only(self):
        """`X → IN_PROGRESS`（X ∈ `PENDING_REVIEW` / `COMPLETED` / `ISSUE` / `PAUSED`）
        ⇒ `temp == 100.00 × 20% == 20.00`；`bonus` **不变**（7.77）。

        **杀法**：删掉 `register_target_hook(OrderStatus.IN_PROGRESS, _on_in_progress)`
        那一行 ⇒ `temp` 停在 1.00 ⇒ 本用例红。
        **杀法②（「bonus 也被改了」向）**：把钩子写成「算了 `temp` 再把 `bonus` 一起
        赋成同值」⇒ `bonus == 7.77` 那半边红（机制上它跑在状态已写成 `IN_PROGRESS`
        之后，`recompute_order_amounts` 的 `bonus` 分支本就不成立）。
        """
        for status, no in (
            (OrderStatus.PENDING_REVIEW, "20260928000000000304"),
            (OrderStatus.COMPLETED, "20260928000000000309"),
            (OrderStatus.ISSUE, "20260928000000000305"),
            (OrderStatus.PAUSED, "20260928000000000306"),
        ):
            with self.subTest(from_status=status):
                order, perf = self._order(no, status=status, temp=Decimal("1.00"),
                                          bonus=Decimal("7.77"))
                apply_transition(order, OrderStatus.IN_PROGRESS, nodes=self.NODES)
                perf.refresh_from_db()
                self.assertEqual(perf.temp_amount, Decimal("20.00"))
                self.assertEqual(perf.bonus_amount, Decimal("7.77"))   # ⛔ 不碰

    def test_a_taker_price_override_wins_over_the_order_price(self):
        """绩效行的覆盖价优先（`effective_taker_price` 的 `is not None` 口径）。

        **杀法**：钩子里改成直接读 `order.hall_price` ⇒ `11.00` 变成 `20.00` ⇒ 红。
        """
        order, perf = self._order("20260928000000000311", taker_price=Decimal("55.00"))
        apply_transition(order, OrderStatus.IN_PROGRESS, nodes=self.NODES)
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("11.00"))           # 55.00 × 20%

    def test_no_taker_is_skipped(self):
        """D4c：MAIN 行 `user` 为空 ⇒ **跳过不重算**（⛔ 不是「按 100% 落回全额」）。

        **杀法（「无接手人没跳过」向）**：去掉钩子里的 `user_id is None` 那一支 ⇒
        `recompute_temp_amount` 会把比例回落成 100% ⇒ `temp` 变成 `100.00` ⇒ 红。
        """
        order, perf = self._order("20260928000000000312", temp=Decimal("1.00"))
        perf.user = None
        perf.save(update_fields=["user"])
        apply_transition(order, OrderStatus.IN_PROGRESS, nodes=self.NODES)
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("1.00"))

    def test_a_settled_row_on_a_dirty_status_is_skipped(self):
        """存量脏行：状态**已经在** `IN_PROGRESS` 却带着结账痕迹。

        本用例经**真实入口**跑，钉的是**可观测**的那一半契约：D4b 的状态闸
        （`settled_status_change_denial`，Task 4）排在钩子之前 ⇒ 整笔被拒、
        `temp_amount` 一个字不改。

        ⚠️ 钩子自己那支 `is_settled` 跳过的判别力由下一条直接调用的用例承担
        —— 它在本入口上**结构性不可达**（见本文件模块 docstring）。
        """
        order, perf = self._order("20260928000000000313", status=OrderStatus.IN_PROGRESS,
                                  temp=Decimal("1.00"))
        perf.settle_status = SettleStatus.SETTLED
        perf.save(update_fields=["settle_status", "updated_at"])
        # 同态迁移不是当前的边（`TRANSITIONS` 除自环外都铺边）⇒ 先用 `PAUSED` 把订单挪开。
        order.status = OrderStatus.PAUSED
        order.save(update_fields=["status"])
        with self.assertRaises(IllegalTransition) as ctx:
            apply_transition(order, OrderStatus.IN_PROGRESS, nodes=self.NODES)
        self.assertIn("已结账", str(ctx.exception))
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAUSED)   # 状态没被改
        self.assertEqual(perf.temp_amount, Decimal("1.00"))

    def test_the_settled_guard_in_the_hook_skips_on_its_own(self):
        """钩子里 `is_settled ⇒ 跳过` 那支（双保险）**直接调**的判据。

        ⚠️ **为什么直接调**：本线唯一的生产调用者是 `apply_transition` 的目标态钩子出口，
        而 D4b 的闸排在它之前且覆盖一切目标态 ⇒ 经那条路永远先被 D4b 拒掉，
        这一支**结构性不可达**（spec §4.4.4 把它登记为「双保险」正是这个意思）。
        ⇒ 不给它一条直接调用的判据，它就是**装饰**（「去掉它，哪条用例会红」答不上来）。

        **杀法**：去掉钩子里的 `is_settled` 那一支 ⇒ 本用例红（`temp` 会被算成 `20.00`）。
        """
        from apps.business.performance import _on_in_progress

        order, perf = self._order("20260928000000000317", status=OrderStatus.IN_PROGRESS,
                                  temp=Decimal("1.00"))
        perf.settle_status = SettleStatus.SETTLED
        perf.save(update_fields=["settle_status", "updated_at"])
        returned = _on_in_progress(order, OrderStatus.PAUSED)
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("1.00"))
        self.assertEqual(returned.pk, perf.pk)      # 跳过时返回该行，⛔ 不是 None

    def test_an_order_without_a_main_row_is_skipped_not_a_crash(self):
        """守卫①（该单 MAIN 行取不到 ⇒ **跳过**）：**可达**场景——订单没有任何绩效行时
        `PAUSED → IN_PROGRESS` 必须照常成功，⛔ 不是 `AttributeError` ⇒ HTTP 500。

        去掉 `if taker is None: return None` 那两支，这条会从「跳过」变成 `AttributeError`（HTTP 500）。
        「恢复已暂停的单」不该因为缺行而 500。

        **杀法**：去掉钩子里 `if taker is None: return None` 那两支 ⇒ 下一句
        `taker.user_id` 对 `None` 抛 `AttributeError` ⇒ 本用例红（ERROR）。
        """
        order = Order.objects.create(
            game=self.game, order_no="20260928000000000319", status=OrderStatus.PAUSED,
            total_price=Decimal("200.00"), hall_price=Decimal("100.00"),
        )
        apply_transition(order, OrderStatus.IN_PROGRESS, nodes=self.NODES)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        # ⛔ 跳过 = 不补行、不写金额：本用例跑完仍没有 MAIN 行。
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).exists()
        )

    def test_the_zero_and_negative_bounds_are_literal(self):
        """⭐ E-0021：三条边界一律钉**字面值**，⛔ 不许用真值判据把 `0` 当成「没传」：
        ① `hall_price = 0.00`（无覆盖价）⇒ `0.00`；
        ② `taker_price = 0.00`（有覆盖价、且它就写 `0`）⇒ `0.00`（⛔ 不是回落到
           `hall_price = 100.00` 算出 `20.00`）；
        ③ `taker_price = -5.00` ⇒ `-5.00 × 20% == -1.00`（D-0015：负数合法）。

        **杀法**：把 `performance_amount.effective_taker_price` 的
        `if performance.taker_price is not None:` 写成真值判据 `if performance.taker_price:`
        ⇒ ② 红（`0` 被当成「没传」）；把它下面那句 `if hall_price is None:` 写成
        `if not hall_price:` ⇒ ① 红（`0` 被当成「没有价」）。
        """
        order, perf = self._order("20260928000000000314", hall=Decimal("0.00"))
        apply_transition(order, OrderStatus.IN_PROGRESS, nodes=self.NODES)
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("0.00"))
        order, perf = self._order("20260928000000000318", taker_price=Decimal("0.00"))
        apply_transition(order, OrderStatus.IN_PROGRESS, nodes=self.NODES)
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("0.00"))
        order, perf = self._order("20260928000000000315", taker_price=Decimal("-5.00"))
        apply_transition(order, OrderStatus.IN_PROGRESS, nodes=self.NODES)
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("-1.00"))

    def test_a_fixed_mode_raises_the_original_message_and_leaves_the_money_alone(self):
        """⭐ R7（领域层那一半）：重算抛错 ⇒ 钩子**让它在原地冒出来**、⛔ 不静默吞；
        这笔单的**钱一分没动**（钩子在写任何金额之前就抛了）。

        ⚠️ **「整笔回滚」不在这一层断言**：`apply_transition` 在跑钩子**之前**已经
        `save()` 了状态，回滚是**调用方**的契约（`transaction.atomic()` +
        `transaction.set_rollback(True)`）——见下面两条端点用例
        （`/transition/` 与 `tests/test_review_api.py` 的 `/review/` 驳回）钉的那一半。
        本层断言「状态**已写**」是**如实**记录这一分工，⛔ 不是把回滚判据放宽。

        **杀法（「抛错被吞」向）**：把钩子包成 `try/except CommissionError: pass` ⇒
        这里 `assertRaises` 收不到异常 ⇒ 本用例红；同时两条端点用例会因为
        `code == 0` 且状态被改到 `IN_PROGRESS` 一起红。
        """
        StaffProfile.objects.filter(user=self.taker).update(
            commission_mode=CommissionMode.FIXED, commission_value=Decimal("30.00"),
        )
        order, perf = self._order("20260928000000000316", status=OrderStatus.PAUSED,
                                  bonus=Decimal("7.77"))
        with self.assertRaises(CommissionError) as ctx:
            apply_transition(order, OrderStatus.IN_PROGRESS, nodes=self.NODES)
        self.assertIn("该单的接手人（用户名 测试打手乙）", ctx.exception.message)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)   # 状态已写（回滚是调用方的事）
        self.assertEqual(perf.temp_amount, Decimal("1.00"))       # ⛔ 钱没动
        self.assertEqual(perf.bonus_amount, Decimal("7.77"))
        self.assertFalse(OperationLog.objects.filter(order_no=order.order_no).exists())


class InProgressRecalcEndpointTests(TenantTestCase):
    """端点侧：`POST /orders/<no>/transition/` 上的「整笔回滚 + 原样弹错」与反向钉子。"""

    #: `/transition/` 的类级门（`OrderViewSet.ORDER_PERMISSIONS["transition"]`）。
    NODES = ("order.change_status",)

    #: ⭐ R7 / D5：`FIXED` 档的报错**原文**（`commission.py` 的 `_TAKER_MODE_HINT` 逐字）。
    #: ⛔ 期望值钉字面值，**不从被测模块 import**（E-0024）。
    FIXED_MSG = (
        "该单的接手人（用户名 测试打手丙）的提成模式是「固定金额」，"
        "本处需要「订单价百分比」；请到人员管理把 TA 的提成模式改成「订单价百分比」后再操作"
    )

    def setUp(self):
        self.game = Game.objects.create(name="D4a 端点数测试游戏")
        # ⚠️ 游戏必须在建 client **之前**建好：`make_logged_in_client` 默认把「当前租户的
        #    全部游戏」勾给账号（D-0136）⇒ 建晚了这张单过不了 `game_scope`。
        from tests.p5_auth_helpers import make_logged_in_client

        self.client = make_logged_in_client(
            username="测试客服丙", groups=("客服",), nodes=self.NODES
        )

    def _order(self, no, *, mode, value, status=OrderStatus.PAUSED,
               hall=Decimal("100.00"), temp=Decimal("1.00"), bonus=Decimal("7.77")):
        order = Order.objects.create(game=self.game, order_no=no, status=status,
                                     total_price=Decimal("200.00"), hall_price=hall)
        taker = User.objects.create_user(username="测试打手丙")
        StaffProfile.objects.create(
            user=taker, commission_mode=mode, commission_value=value,
        )
        perf = Performance.objects.create(
            order=order, user=taker, emp_type=EmpType.MAIN,
            temp_amount=temp, bonus_amount=bonus,
        )
        return order, perf

    def _post(self, order_no):
        return self.client.post(
            f"/api/business/orders/{order_no}/transition/",
            {"target": OrderStatus.IN_PROGRESS}, content_type="application/json",
        ).json()

    def test_the_endpoint_rolls_back_and_returns_the_original_message(self):
        """⭐ R7：`/transition/` → `IN_PROGRESS` 撞 `CommissionError` ⇒ 业务码 +
        `msg` **等于** D5 那句原文 + 状态列没变 + 金额两列没变 + 无留痕。

        **杀法（「抛错被吞」向，端点侧）**：把钩子包成 `try/except CommissionError: pass`
        ⇒ `code == 0` 且订单被改到 `IN_PROGRESS` ⇒ 本用例红。
        """
        order, perf = self._order("20260928000000000330",
                                  mode=CommissionMode.FIXED, value=Decimal("30.00"))
        body = self._post(order.order_no)
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertEqual(body["msg"], self.FIXED_MSG, body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAUSED)
        self.assertEqual(perf.temp_amount, Decimal("1.00"))
        self.assertEqual(perf.bonus_amount, Decimal("7.77"))
        self.assertFalse(OperationLog.objects.filter(order_no=order.order_no).exists())

    def test_a_normal_mode_still_succeeds(self):
        """⭐ 反向钉子（防「一刀切全拒」那种恒真实现）：正常百分比模式 ⇒ `code == 0`、
        `temp` 按字面值、`bonus` 不变。

        **杀法**：把钩子写成无条件 `raise`（或把模式判据写反）⇒ 本用例红。
        """
        order, perf = self._order("20260928000000000331",
                                  mode=CommissionMode.ORDER_PERCENT,
                                  value=Decimal("20.00"))
        body = self._post(order.order_no)
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)   # 这次真的改了
        self.assertEqual(perf.temp_amount, Decimal("20.00"))       # 100.00 × 20%
        self.assertEqual(perf.bonus_amount, Decimal("7.77"))        # ⛔ 不碰

    def test_an_uncomputable_base_price_rolls_back_and_returns_the_original_message(self):
        """⭐ R7（取不出基价那一档，端点侧）：`taker_price` 与 `hall_price` **都为空**
        ⇒ `effective_taker_price` 抛 `AmountError` ⇒ 业务码 + `msg` **等于**原文 +
        状态列没变 + 金额两列没变 + 无留痕（整笔回滚）。

        两条金额都算不出来时，端点必须回业务错误并整笔回滚，不能 500。

        **杀法（「抛错被吞」向）**：把钩子包成 `try/except AmountError: pass`
        ⇒ `code == 0` 且订单被改到 `IN_PROGRESS` ⇒ 本用例红；
        **杀法②（「没回滚」向）**：把 `transition_order` 的
        `transaction.set_rollback(True)` 去掉 ⇒ 状态列留 `IN_PROGRESS` ⇒ 红。
        """
        order, perf = self._order("20260928000000000332",
                                  mode=CommissionMode.ORDER_PERCENT,
                                  value=Decimal("20.00"), hall=None)
        body = self._post(order.order_no)
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        # ⛔ 期望值钉字面值，**不从被测模块 import**（E-0024）。
        self.assertEqual(body["msg"], "该单没有接单价，请联系客服", body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAUSED)
        self.assertEqual(perf.temp_amount, Decimal("1.00"))
        self.assertEqual(perf.bonus_amount, Decimal("7.77"))
        self.assertFalse(OperationLog.objects.filter(order_no=order.order_no).exists())


class AssignSkipsTheAmountHookTests(TenantTestCase):
    """`POST /orders/<no>/assign/`：钱钩子被**显式**跳过，通知钩子与尾副作用照跑。

    依据 R10 / R11 与 `E-0163`（新钩子把 `/assign/` 打成 HTTP 500 的回归）。
    本类覆盖 A / B / C / E / F 五档；**D 档**（`/transition/` 的默认值没被改动 ⇒ 钱钩子照跑、
    算不出就拒 + 整笔回滚）由本文件既有的
    `InProgressRecalcEndpointTests.test_the_endpoint_rolls_back_and_returns_the_original_message`
    承担——它断的正是「经 `/transition/` 算不出 ⇒ 业务码 + 原文 + 状态/金额不动」，
    ⛔ 不为凑数重造一条。

    夹具全是**合成值**（E-0077）；金额与业务码一律钉**字面值**（E-0024，⛔ 不从被测模块
    import 常量）。
    """

    #: `/assign/` 的类级节点门（`OrderViewSet.ORDER_PERMISSIONS["assign"]` = any）。
    NODES = ("order.assign_taker",)

    #: ⭐ R7 / D5：`FIXED` 档的报错**原文**（`commission.py` 的 `_TAKER_MODE_HINT` 逐字）。
    #: ⛔ 期望值钉字面值，**不从被测模块 import**（E-0024）。
    FIXED_MSG = (
        "该单的接手人（用户名 测试旧打手戊）的提成模式是「固定金额」，"
        "本处需要「订单价百分比」；请到人员管理把 TA 的提成模式改成「订单价百分比」后再操作"
    )

    def setUp(self):
        self.game = Game.objects.create(name="D4a 改派跳过钱钩子测试游戏")
        # ⚠️ 游戏必须在建 client **之前**建好：`make_logged_in_client` 默认把「当前租户的
        #    全部游戏」勾给账号（D-0136）⇒ 建晚了这张单过不了 `game_scope`。
        from tests.p5_auth_helpers import make_logged_in_client

        self.client = make_logged_in_client(
            username="测试客服戊", groups=("客服",), nodes=self.NODES
        )

    def _taker(self, username, *, mode, value):
        user = User.objects.create_user(username=username)
        profile = StaffProfile.objects.create(
            user=user, commission_mode=mode, commission_value=value,
        )
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=无游戏=拒）
        #    ⇒ 本夹具造的打手一律持本类的游戏（「无该游戏 ⇒ 拒」的专钉在
        #    tests/test_order_assign_game_gate.py）。
        from tests.p5_auth_helpers import grant_games

        grant_games(user, self.game)
        return user

    def _order(self, no, *, from_status, hall, old_taker, temp=Decimal("1.00"),
               bonus=Decimal("7.77"), created_by=""):
        order = Order.objects.create(
            game=self.game, order_no=no, status=from_status,
            total_price=Decimal("200.00"), hall_price=hall, created_by=created_by,
        )
        perf = Performance.objects.create(
            order=order, user=old_taker, emp_type=EmpType.MAIN,
            temp_amount=temp, bonus_amount=bonus,
        )
        return order, perf

    def _assign(self, order_no, taker):
        return self.client.post(
            f"/api/business/orders/{order_no}/assign/",
            {"user_id": taker.id}, content_type="application/json",
        ).json()

    # ── A 档 ────────────────────────────────────────────────────────────────
    def test_reassigning_a_paused_order_survives_an_unusable_outgoing_taker(self):
        """⭐ `E-0163` 的回归钉子：旧接单人提成模式是「固定金额」也不能挡住改派。

        订单 `PAUSED`、发单价 `100.00`；MAIN 行旧接单人 = 固定金额（钱钩子对它算不出来）；
        指派给一个「订单价百分比 20%」的新人。

        **修后实测**：HTTP 200 / `code=0`、状态 `IN_PROGRESS`、MAIN 行 `user` = 新人、
        `temp_amount == 20.00`（= 100.00 × 20%，**不是**旧人的、**不是**旧值 1.00）。

        **杀法（M1）**：去掉 `assign_order` 那次 `apply_transition` 的
        `skip_amount_recalc=True` ⇒ 钱钩子按**旧**接单人（固定金额）抛 `CommissionError`，
        而那一支只 `except IllegalTransition` ⇒ 异常冒出 ⇒ 本用例红（ERROR）。
        """
        old = self._taker("测试旧打手戊", mode=CommissionMode.FIXED, value=Decimal("30.00"))
        new = self._taker(
            "测试新打手戊", mode=CommissionMode.ORDER_PERCENT, value=Decimal("20.00")
        )
        order, perf = self._order(
            "20260928000000000401", from_status=OrderStatus.PAUSED,
            hall=Decimal("100.00"), old_taker=old,
        )
        body = self._assign(order.order_no, new)
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(perf.user_id, new.id)
        self.assertEqual(perf.temp_amount, Decimal("20.00"))

    def test_assign_recalculates_temp_even_though_the_amount_hook_is_skipped(self):
        """A 档第二条：`temp_amount` 是**重算过来的**，⛔ 不是把旧值留在那里。

        起点先把该行的 `temp_amount` 写成一个明显错误的合成值 `9.99` ⇒ 改派后必须等于
        正确算式值 `20.00`（D4a 的业务结果在 `/assign/` 上仍成立，只是由 `set_taker` 提供）。
        """
        old = self._taker("测试旧打手己", mode=CommissionMode.FIXED, value=Decimal("30.00"))
        new = self._taker(
            "测试新打手己", mode=CommissionMode.ORDER_PERCENT, value=Decimal("20.00")
        )
        order, perf = self._order(
            "20260928000000000402", from_status=OrderStatus.PAUSED,
            hall=Decimal("100.00"), old_taker=old, temp=Decimal("9.99"),
        )
        body = self._assign(order.order_no, new)
        self.assertEqual(body["code"], 0, body)
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("20.00"))

    # ── B 档 ────────────────────────────────────────────────────────────────
    def test_assign_with_an_uncomputable_base_price_still_rolls_back(self):
        """B 档（R7 未被放松）：**新**接单人那一步取不出基价 ⇒ 业务码 + 原文 + 整笔回滚。

        `hall_price=None` 且该行没有 `taker_price` ⇒ `set_taker` 里的重算抛
        `AmountError`（`该单没有接单价，请联系客服`，值钉字面值）⇒ `assign_order` 既有的
        `except (CommissionError, _AmountError)` 那一支 `transaction.set_rollback(True)`。
        ⇒ 状态列仍 `PAUSED`、两金额列一个字不改。

        **本档钉的是**：跳过钱钩子**不等于**放松 R7（真算不出来的那一步仍由 `set_taker` 拒）。
        """
        old = self._taker("测试旧打手庚", mode=CommissionMode.FIXED, value=Decimal("30.00"))
        new = self._taker(
            "测试新打手庚", mode=CommissionMode.ORDER_PERCENT, value=Decimal("20.00")
        )
        order, perf = self._order(
            "20260928000000000403", from_status=OrderStatus.PAUSED,
            hall=None, old_taker=old,
        )
        body = self._assign(order.order_no, new)
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertEqual(body["msg"], "该单没有接单价，请联系客服", body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAUSED)
        self.assertEqual(perf.temp_amount, Decimal("1.00"))
        self.assertEqual(perf.bonus_amount, Decimal("7.77"))

    # ── C 档 ────────────────────────────────────────────────────────────────
    def test_assigning_to_the_same_unusable_taker_is_still_rejected(self):
        """C 档（T6 之前的既有口径，修法**不得**把它放宽成静默成功）：

        MAIN 行的接单人**就是要指派的那个人自己**、而该人模式是固定金额 ⇒
        `code=1000` + 文案指**这个人**、订单不动（整笔回滚）。
        """
        same = self._taker("测试旧打手戊", mode=CommissionMode.FIXED, value=Decimal("30.00"))
        order, perf = self._order(
            "20260928000000000404", from_status=OrderStatus.PAUSED,
            hall=Decimal("100.00"), old_taker=same,
        )
        body = self._assign(order.order_no, same)
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertEqual(body["msg"], self.FIXED_MSG, body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAUSED)
        self.assertEqual(perf.user_id, same.id)
        self.assertEqual(perf.temp_amount, Decimal("1.00"))
        self.assertEqual(perf.bonus_amount, Decimal("7.77"))

    # ── E 档（本轮修法的判别力钉子）────────────────────────────────────────
    def test_the_notification_hook_still_runs_when_the_amount_hook_is_skipped(self):
        """⭐ E 档：**通知钩子照跑** —— 只跳钱钩子、不整段跳过钩子。

        构造：订单 `WAITING_GRAB`（直接 ORM 造：D2 闸挡的是**迁入**池内两态，
        不管存量数据）、MAIN 行 `user` = 旧接单人（模式 **FIXED**）、
        `order.created_by` = 一个活跃用户的 `username`；`POST /assign/` 改派给一个正常模式
        的新人。⇒ `notifications._on_in_progress` 在钱钩子**之后**注册，它看到
        `from_status == WAITING_GRAB` 且库里当时仍有归属人 ⇒ 给抛单人写**一条**
        `Notification(kind=GRABBED)`。

        **杀法（M3）**：把 `_run_target_hooks` 的判据改成「有开关就**整段**不跑」
        （忽略 `recomputes_amount` 标记）⇒ 通知钩子被跳过 ⇒ 本用例红（通知行数 0）。

        ⚠️ 断言打在**数据库行**上（E-0009：⛔ 不许断言 `logger`）。
        """
        thrower = User.objects.create_user(username="测试抛单戊")
        old = self._taker("测试旧打手辛", mode=CommissionMode.FIXED, value=Decimal("30.00"))
        new = self._taker(
            "测试新打手辛", mode=CommissionMode.ORDER_PERCENT, value=Decimal("20.00")
        )
        order, perf = self._order(
            "20260928000000000405", from_status=OrderStatus.WAITING_GRAB,
            hall=Decimal("100.00"), old_taker=old, created_by=thrower.username,
        )
        body = self._assign(order.order_no, new)
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(perf.user_id, new.id)
        self.assertEqual(
            Notification.objects.filter(
                user=thrower, kind=NotificationKind.GRABBED, order=order
            ).count(),
            1,
        )

    # ── F 档（`apply_transition` **尾**上那两条副作用的仓内判据）──────────────
    def test_the_status_sync_tail_still_runs_when_the_amount_hook_is_skipped(self):
        """⭐ F 档：**尾副作用照跑** —— `skip_amount_recalc` 只跳钱钩子，⛔ 不是关掉整段、
        也不是把异常吞掉。

        配方 = A 档同款（订单 `PAUSED`、MAIN 行旧接单人提成模式 **FIXED** ⇒ 钱钩子本来会抛
        `CommissionError`），另按 `tests/test_jidanpu_member_triggers.py` 的绑定夹具造
        **一条生效的绑定 + 该单的一条链路** ⇒ `apply_transition` 尾上的
        `jidanpu_member.queue_status_out` 会给那条链路排**恰好一行** `member.status_out`。

        ⚠️ **本用例钉的是哪一半**：`_run_target_hooks` 里那两类钩子（钱 / 通知）跑完之后，
        `apply_transition` 尾上还有两句副作用（`queue_status_out` 与
        `peer_dispatch.notify_order_status_changed`）——它们**不在**目标态钩子出口里。
        **杀法（「吞异常」向）**：把「按旧接单人重算金额」那个钩子的异常挡在
        `_run_target_hooks` 那一句上（吞掉 / 或让异常从 `apply_transition` 直接冒出）
        ⇒ 控制流根本走不到尾上那两句 ⇒ 本用例红（台账行数 0）。
        ⚠️ 「把 `_run_target_hooks` 改成有开关就**整段不跑**」那种变异**不会**打红本用例
        （尾上那两句在它之后、与钩子开关无关）——那一半由本类 E 档的通知行数钉住；
        **两条合起来**才钉住「只跳钱钩子」这件事（实测读数见 Task 10 报告）。

        ⚠️ 断言打在**数据库行**上（E-0009：⛔ 不断言 `logger`）；夹具全是**合成值**（E-0077）。
        """
        old = self._taker("测试旧打手壬", mode=CommissionMode.FIXED, value=Decimal("30.00"))
        new = self._taker(
            "测试新打手壬", mode=CommissionMode.ORDER_PERCENT, value=Decimal("20.00")
        )
        order, _perf = self._order(
            "20260928000000000406", from_status=OrderStatus.PAUSED,
            hall=Decimal("100.00"), old_taker=old,
        )
        binding = JidanpuMemberBinding.objects.create(
            user=old,
            jidanpu_base_url="https://jidanpu-tail.test.invalid",
            account="acc-测试旧打手壬",
            password="pw-canary-tail",
            source_name="团团通系统",
            enabled=True,
        )
        JidanpuMemberOrderLink.objects.create(
            binding=binding, order=order,
            jidanpu_order_id="JDP-00000000000000000406", in_hall=False,
        )
        response = self.client.post(
            f"/api/business/orders/{order.order_no}/assign/",
            {"user_id": new.id}, content_type="application/json",
        )
        body = response.json()
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        rows = JidanpuSyncEvent.objects.filter(
            kind="member.status_out", target_ref=order.order_no,
        )
        self.assertEqual(rows.count(), 1, body)
        self.assertEqual(rows.get().payload["member_binding_id"], binding.pk)
