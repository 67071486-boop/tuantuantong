"""抢单的限制与正常路径（并发另见 `test_claim_concurrency.py`）。

⚠️ **四支限制共用同一个业务码** `GRAB_LIMIT_EXCEEDED`（spec §5 的码表），
所以「断言 `code == 1013`」**区分不出是哪一支开的火**。本文件因此让每条限制各有
一条用例，且每条用例的夹具都保证**另外三支不可能开火**，再断 `msg` 说明是哪一支——
否则就是「用同一个码断言四件不同的事」，等于没断言。

价格区间与持单上限的**取值来源**是按人读取（`grab_limits.limits_for_order`），
所以这类用例的夹具给 `self.taker` 补
`StaffProfile` 的按人值、期望值写**该人的字面值**（不引用被测常量）。「缺档案 ⇒ 回落全局 `10`」
这一支的**集成**回归留在 `test_active_limit_counts_only_main_rows`（不建档案）——按人值不替代回落值。

⚠️ 夹具：池内订单必须已经有那条 MAIN 绩效行。`/claim` 的条件更新打在那一行上，
缺行会把 `rowcount=0` 翻成「该订单已被别人接走」。进池钩子缺行会补一条，不抛。
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile, TakerWallet, WithdrawOrder
from apps.audit.models import OperationLog
from apps.business.claim import ClaimError, claim_order
from apps.business.models import (
    DailiantongPush,
    EmpType,
    Game,
    Order,
    OrderStatus,
    OrderType,
    Performance,
)
from apps.business.order_status import IllegalTransition, apply_transition
from apps.core.error_codes import ErrorCode
from tests.order_rules_helpers import release_taker
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 本文件走到的订单端点门（`OrderViewSet.ORDER_PERMISSIONS`）。
#: `groups` 只建 Group、**不放行** ⇒ 每个真实登录的夹具都要显式声明它要过的门。
CLAIM_NODES = (
    "grab_hall.take_order",   # 抢单 / 大厅
    "order.add_edit",         # 录单
    "order.list",             # 列表
    "order.view_detail",      # 详情
)

#: DRF 对**没有自定义 `message` 的权限类**给的默认 403 文案（本项目 zh-hans 下的实测值；
#: 同 `test_rbac_endpoint_enforcement` 里的同名常量）。用来把「节点门拒绝」与
#: 「CSRF 失败」等其它 403 来源区分开。⚠️ 期望值写字面量（E-0024）。
NODE_DENIED_MSG = "您没有执行该操作的权限"


def _no(n):
    """20 位订单号（P2 起订单的对外标识是 `order_no`，`lookup_value_regex = r"\\d{20}"`）。"""
    return f"20260920{n:012d}"


class ClaimLogicTests(TenantTestCase):
    """直接测纯逻辑——不经 HTTP。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.taker = User.objects.create_user(username="taker1")

    def _profile(self, **fields):
        """给 `self.taker` 建一份档案——抢单限制的**按人取值来源**。

        ⚠️ 只在这个用例真的要用按人值时调用：**缺档案**那一支（回落全局常量）由
        `test_active_limit_counts_only_main_rows` 单独覆盖，两者不能互相替代。
        """
        return StaffProfile.objects.create(user=self.taker, **fields)

    def _order(self, no, status=OrderStatus.WAITING_GRAB, price="88.00", order_type=""):
        """建一对「订单 + MAIN 绩效」。`user` 为空 = 还没人接。

        `price=None` ⇒ `hall_price` 为 **NULL**：这在数据层是合法的
        （`models.py` 的 `hall_price` 是 `null=True`，`/throw` 只在传了价时才写它），
        所以「一条没有接单价的大厅单」是真实存在的状态（Ruling 49 的夹具）。

        `order_type` 默认空串（模型字段没有 `default`）⇒ 按 C4 走**代练**（`dl`）上限；
        需要陪玩（`pw`）时显式传 `OrderType.COMPANION`。
        """
        hall_price = None if price is None else Decimal(price)
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            hall_price=hall_price,
            order_type=order_type,
        )
        Performance.objects.create(
            order=order,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("88.00") if price is None else Decimal(price),
        )
        return order

    def _main(self, order):
        return Performance.objects.get(order=order, emp_type=EmpType.MAIN)

    def _hold(self, no, *, user=None, emp_type=EmpType.MAIN):
        """直接摆出「某人手上持有的一条绩效行」——**不经 `claim_order`**。

        理由见 `test_active_order_limit_is_enforced` 的 docstring：上限用例的夹具要摆到
        **达到上限**为止，连调 `claim_order` 会被**上限那一支**拦下（夹具造不完），
        而它与「上限真的开火了」共用同一个码 `1013` ⇒ 断言上不可区分。
        """
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order,
            user=user or self.taker,
            emp_type=emp_type,
            temp_amount=Decimal("88.00"),
        )
        return order

    def _fixture_rows(self, *, user=None):
        """数夹具摆出来的绩效行数——它是「期望放行」那两条上限用例的**判别力落点**。

        ⚠️ 断言的是**值**（行数），不是「有行存在」：夹具若静默没造出行，
        `test_a_persons_zero_limit_means_no_cap`（持有 11 单仍放行）与
        `test_a_companion_order_reads_the_pw_limit`（1 单 + `pw` 上限 5 放行）
        都会**在 0 持有上恒绿** ⇒ 测不到它们声称要测的东西（判别力判据 3）。
        """
        return Performance.objects.filter(user=user or self.taker).count()

    # ── 正常路径 ──────────────────────────────────────────────
    def test_claim_writes_user_and_status(self):
        order = self._order(_no(1))
        claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._main(order).user_id, self.taker.id)

    def test_claim_writes_the_claim_time_on_the_same_row(self):
        """接单统计列的**前半条不变量**：`user_id` 被写 ⇒ `claimed_at` 被写。

        ⚠️ 判别力：把条件更新里的 `claimed_at=…` 删掉 ⇒ `claimed_at` 是 `None` ⇒ 本用例红。
        断言打**值**（`is not None` + 不早于本用例开跑的时刻），不是「这个字段存在」。
        """
        order = self._order(_no(740))
        before = timezone.now()

        claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})

        perf = self._main(order)
        self.assertEqual(perf.user_id, self.taker.id)          # 哨兵：人真的写上了
        self.assertIsNotNone(perf.claimed_at)
        self.assertGreaterEqual(perf.claimed_at, before)

    def test_a_pool_entry_clears_the_claim_time_with_the_taker(self):
        """**后半条不变量**：`user_id` 被清 ⇒ `claimed_at` 被清（**同一次**回退）。

        走**产品路径**：抢单 → 迁回池内。回退由 `_on_pool_entry` → `reset_taker_performance`
        负责，而 `/withdraw`、`/clear-taker`、`/transition/` 与「退单回池」**全部**汇到这一个落点
        （`grep -rn "user_id=None|user=None" apps/business` 只命中注释，没有第二处清人）。
        ⚠️ **前置哨兵**：先证明 `claimed_at` 真的被写上过——缺了它，`assertIsNone` 对
        「从头到尾都是 `None`」与「被清回 `None`」不加区分。
        """
        order = self._order(_no(741))
        claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})
        self.assertIsNotNone(self._main(order).claimed_at)      # 哨兵：先真的写上了
        # ⚠️ `claim_order` **自己重新取一次行**（它收 ID）⇒ 内存里这份还是 `WAITING_GRAB`；
        #    不刷新就迁状态会被 `apply_transition` 的复读比对判成陈旧（IllegalTransition）。
        order.refresh_from_db()

        # ⭐ D2（spec §4.1.2 例外①）：此刻 MAIN 行的 `user` 非空 ⇒ 回池必须走
        #    「**显式清除接单人**」这个产品动作（= `/clear-taker/` 那条路），
        #    （直接 `apply_transition(…, UNASSIGNED)` 会被新闸拒。）
        #    ⛔ 断言一个字没放宽：本用例要钉的仍是「清人 ⇒ 清 `claimed_at`」。
        release_taker(order, nodes={"order.change_status"})

        perf = self._main(order)
        self.assertIsNone(perf.user_id)
        self.assertIsNone(perf.claimed_at)

    def test_second_claim_is_rejected_with_its_own_code(self):
        """⚠️ 「已被别人接走」必须是**独立业务码**，不能与"参数错"共用——
        否则前端无法区分「手慢了」和「我点错了」。
        """
        order = self._order(_no(2))
        other = User.objects.create_user(username="taker2")
        claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})

        with self.assertRaises(ClaimError) as ctx:
            claim_order(user_id=other.id, order_id=order.id, nodes={"grab_hall.take_order"})

        self.assertEqual(ctx.exception.code, ErrorCode.ORDER_ALREADY_TAKEN)
        self.assertEqual(self._main(order).user_id, self.taker.id)   # 没被改
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)      # 状态也没被改回去

    def test_claim_only_touches_the_main_row(self):
        """客服的「销售提成」行不得被抢（spec §7.1 的 `emp_type` 限定）。"""
        order = self._order(_no(3))
        kefu = User.objects.create_user(username="kefu")
        Performance.objects.create(
            order=order, user=kefu, emp_type=EmpType.SALES
        )
        claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})
        sales = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(sales.user_id, kefu.id)        # 客服那行纹丝不动
        self.assertEqual(self._main(order).user_id, self.taker.id)

    def test_claim_ignores_a_sales_row_that_has_no_user_yet(self):
        """`emp_type` 限定是**胜负判据的一部分**，不是装饰。

        ⚠️ 上一条用例**证明不了**这一点：那条 SALES 行的 `user` 已经有值，
        所以 WHERE 里就算没有 `emp_type`，它也匹配不上 `user_id IS NULL`。
        这里把 SALES 行也做成**没人接**的（约束①只约束 `(order, user)` 唯一、
        约束②只约束 MAIN，所以「一条还没归属人的销售提成行」是合法状态）——
        此时若 WHERE 少了 `emp_type`，一条 UPDATE 会**同时命中两行**
        （`rowcount == 2`）⇒ 本用例变红。
        """
        order = self._order(_no(4))
        Performance.objects.create(order=order, emp_type=EmpType.SALES)
        claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.SALES).user_id
        )
        self.assertEqual(self._main(order).user_id, self.taker.id)

    # ── 角色：用**调用方的**角色，不替调用方断言 ────────────────
    def test_claim_order_requires_a_nodes_argument(self):
        """① `nodes` 是**必填 keyword-only**（与 `apply_transition` 同形）：
        漏传 = `TypeError` —— fail-closed 在**签名层**，不会静默替调用方断言身份。

        判别力：把签名改回 `claim_order(*, user_id, order_id)` ⇒ 下面这次调用不再抛
        `TypeError`（它会一路抢成功）⇒ 本用例红。
        """
        order = self._order(_no(20))
        with self.assertRaises(TypeError):
            claim_order(user_id=self.taker.id, order_id=order.id)   # 故意漏传 nodes（签名层 fail-closed）

    def test_a_caller_without_the_taker_node_is_rejected_by_the_state_machine(self):
        """① 传入的节点集不含该边**任何**节点 ⇒ 状态机**真的**拒绝（`IllegalTransition`）。

        这是纵深层的钉子：授权入口是端点的类级节点门，但 `claim_order` 也把调用方节点
        交给状态机再判一次；测试里「只有 105 的节点集」就是「门被绕过 / 新调用点漏闸」的形状。
        ⚠️ 靶子选择：那条边的节点集按 **target** 定后是 `{16, 18, 1162, 17, 50}`
        ——`order.change_status_completed`（17）已是**边内**节点、构造不出拒绝，
        所以传 `order.submit_work`（105，属 `PENDING_REVIEW` 那一组）这个**本边不含**的节点。
        判别力：把 `apply_transition(..., nodes=nodes)` 写回常量 `{grab_hall.take_order}`
        ⇒ 状态机的节点维度对本路径恒真 ⇒ 下面 `assertRaises(IllegalTransition)` 变红。
        """
        order = self._order(_no(21))
        with self.assertRaises(IllegalTransition):
            claim_order(
                user_id=self.taker.id,
                order_id=order.id,
                nodes={"order.submit_work"},
            )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        # 钉住 `claim.py` 的那条绝对句：状态机判定发生在**绩效条件更新
        # **之后** ⇒ 直接调用者拿到的是「异常 + 那半截已写入的行」，端点必须自己回滚。
        # 判别力：谁把节点判据提前到写入之前（那其实是更好的修法），这一行就变红。
        self.assertEqual(self._main(order).user_id, self.taker.id)

    # ── 限制一：接单价区间（按人取值）────────────────────────────
    def test_price_out_of_range_is_rejected(self):
        """接单价落在**这个人**配置的区间之外 ⇒ 拒（spec §7.2；P3c Task 2 起按人取值）。

        判别力来自**价位落在全局区间之内**：`50.00` 不低于全局下界 `0`、也不高于全局上界
        `99999` ⇒ 旧实现（读全局常量）会**放行**。
        期望值与夹具值（`50.00` / `100`）都写字面量，不引用被测常量。

        本用例的 `held == 0` ⇒ 另外两支不可能开火，
        所以「是哪一支」是可判别的。
        """
        self._profile(grab_price_min=Decimal("100"))
        order = self._order(_no(5), price="50.00")
        with self.assertRaises(ClaimError) as ctx:
            claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})
        self.assertEqual(ctx.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("接单价必须在", str(ctx.exception))     # 同码四支 ⇒ 必须能看出是哪一支
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)   # 没抢到
        self.assertIsNone(self._main(order).user_id)               # 绩效行也没写人

    def test_price_boundaries_are_inclusive(self):
        """区间两端**含**：该人配的上界 `100` 与**回落来的**下界 `0` 都能抢。

        `0.00` 那一条**不是**在证明「判据用了 `is not None` 而不是真值」——
        本夹具的下界是**回落来的** `0`（这个人没配下限），所以 `0` 在两种写法下都放行，
        这个用例证不出两者的差异。它钉的是另一件事：**0 是合法的接单价**
        （D-0015 之下金额可以为负，`0` 不是「空值」），免得后来者顺手加一条
        「接单价必须 > 0」——那是 AGENTS.md §5 里 E-0021 的同族形状。
        （⚠️ 反过来：把下限配成 `100` 的人手里，`price = 0` 必须被拦下 —— 真值写法在
        那个夹具上**是**可分辨的，见 `claim.py` 的注释与 `test_price_out_of_range_is_rejected`。）
        ⚠️ 判别力在最后一条：`100.01` 落在全局区间之内、只超出**这个人的**上界 `100`
        ⇒ 旧实现会放行。
        """
        self._profile(grab_price_max=Decimal("100"))
        at_min = self._order(_no(6), price="0.00")
        claim_order(user_id=self.taker.id, order_id=at_min.id, nodes={"grab_hall.take_order"})
        at_max = self._order(_no(7), price="100.00")
        claim_order(user_id=self.taker.id, order_id=at_max.id, nodes={"grab_hall.take_order"})
        self.assertEqual(self._main(at_min).user_id, self.taker.id)
        self.assertEqual(self._main(at_max).user_id, self.taker.id)

        above = self._order(_no(8), price="100.01")
        with self.assertRaises(ClaimError) as ctx:
            claim_order(user_id=self.taker.id, order_id=above.id, nodes={"grab_hall.take_order"})
        self.assertEqual(ctx.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("接单价必须在", str(ctx.exception))
        self.assertIsNone(self._main(above).user_id)

    def test_a_persons_price_max_blocks_a_higher_order(self):
        """某人 `grab_price_max = Decimal("50")`、单的接单价 `60.00` ⇒ 被拒。

        ⚠️ `60.00` 落在**全局**区间 `[0, 99999]` 之内 ⇒ 旧实现放行；
        夹具与期望值都写字面量，不引用 `GLOBAL_PRICE_*`。
        """
        self._profile(grab_price_max=Decimal("50"))
        order = self._order(_no(722), price="60.00")
        with self.assertRaises(ClaimError) as ctx:
            claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})
        self.assertEqual(ctx.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("接单价必须在", str(ctx.exception))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main(order).user_id)

    def test_a_hall_order_without_a_price_cannot_be_claimed(self):
        """③ `hall_price IS NULL` ⇒ **不可抢**（Ruling 49）：代练员不该接一个不知道多少钱的活。

        `hall_price` 是 `null=True`，而抛单不传价时它保持 NULL ⇒ 大厅里真的会有无价单。
        修之前价格支路写成 `if price is not None and (...)`，NULL 时**整支跳过**，
        这张单反而**不受任何价格判据**被抢走（静默）。
        判别力：把 NULL 分支改回「跳过」⇒ 下面 `assertRaises(ClaimError)` 变红（抢成功）。
        ⚠️ 这是**业务限制**，不是给金额列加 `≥ 0` 约束（D-0015 只管列约束）。
        """
        order = self._order(_no(30), price=None)
        self.assertIsNone(order.hall_price)          # 哨兵：夹具真的是「没有接单价」
        with self.assertRaises(ClaimError) as ctx:
            claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})
        self.assertEqual(ctx.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("没有接单价", str(ctx.exception))   # 同码四支 ⇒ 必须能看出是哪一支
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main(order).user_id)

    def test_null_price_rejection_does_not_read_the_staff_profile(self):
        """📌 顺序承诺的机械钉子（`claim.py:55-58` docstring + `:80` 调用点注释）：
        四支里只有 NULL 接单价那一支在按人读取**之前**判（因此不查库），所以按人限制的
        读取排在它之后。
        判别力：把 `limits = limits_for_order(...)` 挪回函数第一行 ⇒ 桩先抛 AssertionError
        ⇒ `assertRaises(ClaimError)` 失败 ⇒ 本用例红。
        ⚠️ 桩打在**被查找的那个模块**（`apps.business.claim`，AGENTS.md §5 / E-0029）。
        ⚠️ 边界：本用例只钉「`claim.py` 没调用 `limits_for_order`」这一件事，**不**覆盖
        「把档案读取**内联**进 `claim.py`」的改法：若内联后仍保留这个名字（或换个入口读），
        那次读取**不会被本用例发现（静默绿）**；若名字被删/改名，`mock.patch` 会因找不到属性
        而红，而**那条红不代表顺序被破坏**（顺序可能仍是对的）。内联违反的是 `claim.py:9-10`
        「本模块不自己读 `StaffProfile`」，要守它得用查询数口径的钉子（本用例未加）。
        """
        order = self._order(_no(31), price=None)
        self.assertIsNone(order.hall_price)          # 哨兵：夹具真的是「没有接单价」
        with mock.patch(
            "apps.business.claim.limits_for_order",
            side_effect=AssertionError("NULL 接单价的单不该为一次档案查询买单"),
        ) as read_limits:
            with self.assertRaises(ClaimError) as ctx:
                claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})
        self.assertIn("没有接单价", str(ctx.exception))  # 同码四支 ⇒ 必须能看出是哪一支
        self.assertEqual(read_limits.call_count, 0)       # 值断言：档案读取一次都没发生
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)  # 拒绝发生在条件更新之前
        self.assertIsNone(self._main(order).user_id)

    # ── 限制二：接单上限（按人取值）────────────────────────────
    def test_active_order_limit_is_enforced(self):
        """接单上限取自**这个人**的档案：同时持有的未完成订单数不得超过它
        （spec §7.2 / §11 #2；P3c Task 2 起按人读取）。

        ⚠️ 夹具**不**连调上限次数 `claim_order` 来铺这些持有行：上限那一支会先开火
        （第 4 次就被拦）⇒ 夹具造不完，而且「夹具没铺好」与「上限真的拦下了」
        共用同一个码 `1013` ⇒ **在断言上不可区分**，用例照样绿。
        所以持有行由 `_hold` 直接摆出，再用 `_fixture_rows` 的计数哨兵证明**它们真的存在**。

        判别力：这个人配的是 `3`，夹具也停在第 3 单 ⇒ 旧实现读全局 `10` 时这次会**放行**；
        「缺档案 ⇒ 回落全局 `10`」那一支由下一条用例覆盖。
        """
        self._profile(grab_limit_dl=3)
        for i in range(3):
            self._hold(_no(100 + i))
        # 哨兵：夹具真的摆了 3 行——它同时证明这 3 行**存在**
        # （返回 0 行说明夹具没造出来，那下面的断言就不是在测上限）
        self.assertEqual(self._fixture_rows(), 3, "夹具没造出预期数量的持有行")

        extra = self._order(_no(999))
        with self.assertRaises(ClaimError) as ctx:
            claim_order(user_id=self.taker.id, order_id=extra.id, nodes={"grab_hall.take_order"})

        self.assertEqual(ctx.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("未完成的订单", str(ctx.exception))     # 是哪一支开的火
        extra.refresh_from_db()
        self.assertEqual(extra.status, OrderStatus.WAITING_GRAB)   # 没抢到
        self.assertIsNone(self._main(extra).user_id)               # 绩效行也没写人

    def test_a_persons_limit_of_one_blocks_the_second_order(self):
        """某人 `grab_limit_dl = 1`、手上已有 1 单 ⇒ 抢第二单被拒。

        ⚠️ 夹具与期望值全是字面量（档案 `1`、夹具 1 行、期望「被拒」）：旧实现读全局 `10`
        时这次会**放行**，所以判别力由夹具本身承担。
        上限那一支的 `msg` 必须能独立说明是哪一支（同码四支）。
        """
        self._profile(grab_limit_dl=1)
        self._hold(_no(720))
        self.assertEqual(self._fixture_rows(), 1, "夹具没造出预期数量的持有行")

        order = self._order(_no(721))
        with self.assertRaises(ClaimError) as ctx:
            claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})

        self.assertEqual(ctx.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("未完成的订单", str(ctx.exception))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main(order).user_id)

    def test_forbid_self_service_blocks_the_claim_with_its_own_message(self):
        """「禁自助接单」开着 ⇒ 抢单被拒，且 msg 能把它与另外三支区分开（同码四支）。

        判别力：把 `claim.py` 那一支删掉 ⇒ 本用例**必须红**
        （其余夹具刻意放到「价格与上限都不拦」：`grab_limit_dl=0`、接单价在区间内）。
        """
        self._profile(grab_forbid_self_service=True, grab_limit_dl=0)
        order = self._order(_no(730))

        with self.assertRaises(ClaimError) as ctx:
            claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})

        self.assertEqual(ctx.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("禁止自助接单", str(ctx.exception))       # 是哪一支开的火
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)   # 没抢到
        self.assertIsNone(self._main(order).user_id)               # 绩效行也没写人

    def test_forbid_self_service_off_does_not_block_the_claim(self):
        """同一个夹具、只把这一个布尔翻成 `False` ⇒ 必须放行（证明红的不是别的支）。"""
        self._profile(grab_forbid_self_service=False, grab_limit_dl=0)
        order = self._order(_no(731))

        claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})

        self.assertEqual(self._main(order).user_id, self.taker.id)

    def test_a_persons_zero_limit_means_no_cap(self):
        """`grab_limit_dl = 0` ⇒ **不限制**（`max_active is None`）⇒ 上限那一支**整支跳过**。

        夹具刻意持有 `11` 条（> 全局 `10`）：旧实现读全局常量 ⇒ 第 12 单会被拦 ⇒ 红；
        新实现读这个人的 `0` ⇒ 不拦。夹具值与期望都是字面量，不引用 `GLOBAL_*`。

        判别力：把 `if limits.max_active is not None:` 去掉、直接
        `held >= limits.max_active` ⇒ 拿 `int` 与 `None` 比大小 ⇒ `TypeError` ⇒ 本用例红。
        ⚠️ 这条**同时**钉住「`0` = 不限制」与「`None` = 未配（缺档案 ⇒ `10`）」是两种不同语义
        （E-0021 的边界）：未配那一支由 `test_active_limit_counts_only_main_rows` 覆盖，
        两边的夹具（11 行 vs 10 行）与期望（放行 vs 拦下）都不同向。
        """
        self._profile(grab_limit_dl=0)
        for i in range(11):
            self._hold(_no(700 + i))
        self.assertEqual(self._fixture_rows(), 11, "夹具没造出预期数量的持有行")

        order = self._order(_no(711))
        claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})
        self.assertEqual(self._main(order).user_id, self.taker.id)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    def test_a_companion_order_reads_the_pw_limit(self):
        """陪玩单走 `grab_limit_pw`（不是 `grab_limit_dl`）——上限来源是 `limits_for_order`。

        夹具：`grab_limit_dl = 1`（已超）、`grab_limit_pw = 5`，手上 1 单，
        订单 `order_type = COMPANION` ⇒ 生效上限是 `5` ⇒ 放行。
        判别力：把 `limits_for_order(user_id, order)` 换成 `limits_of(user_id)`（固定取 `dl`）
        ⇒ 上限变成 `1` ⇒ 被拦 ⇒ 本用例红。
        """
        self._profile(grab_limit_dl=1, grab_limit_pw=5)
        self._hold(_no(723))
        self.assertEqual(self._fixture_rows(), 1, "夹具没造出预期数量的持有行")

        order = self._order(_no(724), order_type=OrderType.COMPANION)
        claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})
        self.assertEqual(self._main(order).user_id, self.taker.id)

    def test_active_limit_counts_only_main_rows(self):
        """接单上限只数**打单工资**行——客服的销售提成不占代练员的额度（spec §7.2）。

        ⚠️ 本用例**不建档案**：它覆盖「缺档案 ⇒ 回落全局 `10`」那一支的**集成**行为
        （读取层那一侧的值由 `tests/test_grab_limits.py` 的用例覆盖）。期望值写**字面量**
        `10`，**不引用** `GLOBAL_MAX_ACTIVE`——拿常量断按同一常量造的夹具是自指断言
        （常量改坏时照样绿，E-0009 / E-0024）。

        判别力：把 `check_grab_limits` 里 held 计数的 `emp_type=MAIN` 去掉 ⇒
        下面那条 SALES 行也被算进去（9 + 1 = 10 ≥ 上限）⇒ 本用例变红。
        """
        for i in range(9):
            self._hold(_no(200 + i))
        self._hold(_no(299), emp_type=EmpType.SALES)
        self.assertEqual(self._fixture_rows(), 10)   # 哨兵：10 行都在

        order = self._order(_no(998))
        claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})
        self.assertEqual(self._main(order).user_id, self.taker.id)

        # 反向对照（**能失败**的那种；替换了原先那条恒真的断言——若计数错把 SALES 算进去，
        # 上面的 `claim_order` 会先抛错，那条断言永远不会是判别的那一条）：
        # 抢到之后代练员手上正好 10 条 MAIN 持有行（9 条夹具 + 刚抢的这条）
        # ⇒ 再抢必须**被上限**拦下（这单的接单价既非 NULL、也在区间内 ⇒ 可开火的只剩上限这一支）。
        # 判别力：把上限判据 `held >= 10` 改成 `>` ⇒ 这一次会被放行 ⇒ 本用例红。
        # 它同时把「只数 MAIN」钉在**边界**上：9 MAIN + 1 SALES 放行（上面），10 MAIN 拦下（这里）。
        at_cap = self._order(_no(997))
        with self.assertRaises(ClaimError) as ctx:
            claim_order(user_id=self.taker.id, order_id=at_cap.id, nodes={"grab_hall.take_order"})
        self.assertEqual(ctx.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("未完成的订单", str(ctx.exception))
        self.assertIsNone(self._main(at_cap).user_id)

    # ── 没有「频率」这一支（业务方裁定去掉）──────────
    def test_six_claims_in_a_row_all_succeed(self):
        """连续抢 **6 张不同**的单 ⇒ 全部成功——钉住「没有频率限制」这条业务规则。

        ⚠️ 夹具必须让**持有上限不构成干扰**：给这个人配 `grab_limit_dl = 0`
        （= **不限制**，读取层把 `0` 翻成 `None`）⇒ 6 张单里没有任何一张会被上限拦下，
        本用例的成败于是只取决于「抢单还有没有别的节流」。
        判别力：把频率判据加回 `claim.py` ⇒ 第 6 张被 `1013` 拦 ⇒ 本用例红
        （原限制是「60 秒内最多 5 单」，第 6 张正是它会拦下的那一张）。
        ⚠️ 6 与期望值都写**字面量**，不引用被测常量（E-0024：自指断言在被测常量被改坏时不会红）。
        """
        self._profile(grab_limit_dl=0)
        orders = [self._order(_no(300 + i)) for i in range(6)]
        for order in orders:
            claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})
        # 值断言：6 张单**各自**都写上了接单人、状态都进了「代练中」——
        # 「全部成功」不能只靠「没抛异常」推出来（判别力判据 3）。
        for order in orders:
            self.assertEqual(self._main(order).user_id, self.taker.id)
            order.refresh_from_db()
            self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    def test_claim_refreshes_updated_at(self):
        """抢单的条件更新必须**显式刷新** `Performance.updated_at`。

        ⚠️ 断的是**值**（真的往后走了），不是「这个字段存在」：`auto_now=True` 只在
        `save()` 上生效，`QuerySet.update()` **不**会替调用方带上它（读码：
        `django/db/models/sql/compiler.py` 的 `SQLUpdateCompiler.as_sql` 只写调用方
        显式给出的字段）。漏了它，这一行的时间戳会**停在创建时刻**——而「这行确实
        被改过」是本仓其它绩效写点留下的那条记录（见 `claim_order` 的 docstring）。
        ⚠️ **夹具为什么必须打桩**：抢单这条路径上，这一行的
        `updated_at` 有**两个**写入点——`claim.py` 的条件更新，以及紧随其后的
        `recompute_temp_amount`（它内部 `performance.save(update_fields=[…, "updated_at"])`）。
        不把后者打桩成 no-op，本条用例断的其实是**后者**：实测把 `claim.py` 的
        `updated_at=timezone.now()` 删掉，本文件仍 `Ran 29 / OK`（零判别力）。
        ⇒ 桩打在**被查找的那个模块**（`apps.business.claim`，`AGENTS.md` §5 / E-0029），
        打桩后这次调用里还写这一行 `updated_at` 的只剩条件更新。
        判别力：把 `updated_at=timezone.now()` 从 `claim.py` 的条件更新里去掉
        ⇒ 本用例**红**；还原 ⇒ 绿。
        """
        order = self._order(_no(400))
        perf = self._main(order)
        stale = timezone.now() - timedelta(days=1)
        Performance.objects.filter(pk=perf.pk).update(updated_at=stale)
        perf.refresh_from_db()
        self.assertEqual(perf.updated_at, stale)      # 前置哨兵：真的推旧了

        # 桩掉「条件更新之后」那处重算（它也会刷新同一行的时间戳）——
        # 否则本条用例断的是那处 `save()`，不是条件更新。
        with mock.patch("apps.business.claim.recompute_temp_amount"):
            claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})

        perf.refresh_from_db()
        self.assertGreater(perf.updated_at, stale)

    # ── 不在大厅的单 ─────────────────────────────────────────
    def test_claim_on_a_non_hall_order_is_rejected(self):
        """不在大厅的单（`UNASSIGNED`、还没人接）不是「被人抢走」，必须走**另一个**码。"""
        order = self._order(_no(500), status=OrderStatus.UNASSIGNED)
        with self.assertRaises(ClaimError) as ctx:
            claim_order(user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"})
        self.assertEqual(ctx.exception.code, ErrorCode.BUSINESS)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(self._main(order).user_id)


class ClaimApiTests(TenantTestCase):
    """抢单的 HTTP 出口。路径取自 **spec §5 的端点表 + Ruling 33**：
    `POST /api/business/hall/{order_no}/claim/`。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no=_no(1),
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        self.url = f"/api/business/hall/{self.order.order_no}/claim/"

    def _login(self, *groups, username="u1", nodes=CLAIM_NODES, games=True):
        """真实登录（fail-closed，spec D11）并返回该用户。

        ⚠️ **不能用 `force_login`**：它产出的会话里**没有 `identity_plane`**，
        会被守卫中间件与 `TenantBoundAuthentication` 按 fail-closed 不变式拒掉——
        结果是**每个业务请求都 401**（完整说明见 `tests/p5_auth_helpers.py`）。

        （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        `/claim` 经 `get_object()`（被 `game_scope` 收窄的 queryset）取单，所以
        「要读/写这张单」的账号必须显式持有 `self.game`；缺档案 ⇒ 空集 ⇒ 1002。
        ⚠️ `games=False` 只给**断言 403 的那一条**
        （`test_a_user_without_the_take_order_node_cannot_claim` 的客服）：
        ⛔ 不给「被断言没有权限」的账号补权限。期望值一个字没改。
        `nodes` 可覆盖：单轴化后**节点**才是判据，拒绝侧要显式不给（spec §4.4）。
        """
        self.client = make_logged_in_client(
            username=username, groups=groups, nodes=nodes
        )
        user = User.objects.get(username=username)
        if games:
            grant_games(user, self.game)
        return user

    def _post(self):
        return self.client.post(self.url, {}, content_type="application/json")

    def _main(self):
        return Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)

    def test_taker_can_claim(self):
        taker = self._login("代练员")
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._main().user_id, taker.id)

    def test_claim_revives_a_soft_deleted_main_row_instead_of_leaving_it_deleted(self):
        """R-7（spec §4.5）：MAIN 行被垃圾桶软删后再抢单 ⇒ **同一步复活它**。

        软删态用 **ORM 直接造**（与 `test_order_assign_ext` 复活用例同法），不等删除端点。
        垃圾桶删接单人那条 MAIN 行会**清空它的 `user`** ⇒ 那条行满足 `/claim` 条件更新的
        谓词（`user_id__isnull=True`），接手人于是会被写进一条**已经软删**的行，留下
        「行还是已删、却带着一个接单人」这个本批新造出来的形状——**它只被按绩效行
        `deleted_at` 过滤的读点排除，不是「读路径全都看不见」**。
        ⇒ 复活必须与 `user`/`claimed_at` 落在**同一次** `UPDATE` 上。
        判别力：实现里少写 `deleted_at=None` ⇒ 它仍停在软删时刻 ⇒ 本用例红。
        """
        perf = self._main()
        original_id = perf.id
        perf.deleted_at = timezone.now()
        perf.deleted_by = "测试删除人甲"
        perf.save(update_fields=["deleted_at", "deleted_by"])
        # 前置哨兵：库里**只有**这一条 MAIN 行，且它确实是软删态
        self.assertEqual(
            Performance.objects.filter(order=self.order, emp_type=EmpType.MAIN).count(), 1
        )
        self.assertIsNotNone(Performance.objects.get(pk=original_id).deleted_at)

        taker = self._login("代练员")
        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.assertEqual(
            Performance.objects.filter(order=self.order, emp_type=EmpType.MAIN).count(), 1
        )
        perf.refresh_from_db()
        self.assertEqual(perf.id, original_id)      # 同一行，不是新建
        self.assertIsNone(perf.deleted_at)
        self.assertEqual(perf.deleted_by, "")
        self.assertEqual(perf.user_id, taker.id)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    def test_claim_leaves_the_delete_columns_of_a_live_main_row_untouched(self):
        """反向对照（R-7 判据 2）：**没被软删**的 MAIN 行抢单后，两列仍是「未删」的初值。

        防的是「无条件清 `deleted_at`」被读成别的意思：复活只对软删行有可观测差异，
        活跃行上 `deleted_at is None` / `deleted_by == ""` 必须**保持**（不是被写成别的值）。
        ⚠️ 本用例在实现前后**都**应绿——它是控制组，不承担 R-7 的红。
        """
        perf = self._main()
        self.assertIsNone(perf.deleted_at)      # 前置哨兵：夹具真的是活跃行
        self.assertEqual(perf.deleted_by, "")

        taker = self._login("代练员")
        self.assertEqual(self._post().json()["code"], 0)

        perf.refresh_from_db()
        self.assertIsNone(perf.deleted_at)
        self.assertEqual(perf.deleted_by, "")
        self.assertEqual(perf.user_id, taker.id)

    def test_claim_writes_an_audit_row(self):
        """spec §6 硬规则 2：迁移必须留痕，且留痕是**数据库行**（不是 `logger.info()`）。

        ⚠️ 断的是**值**（接单人 / 渠道 / 接手价 / 操作人），不是「有一行」（判别力判据 3）。
        ⚠️ `order.claim` 归 **10 订单相关**（照抄对标的不自洽边界，spec A2/§11-1）。
        """
        taker = self._login("代练员")
        self.assertEqual(self._post().json()["code"], 0)
        row = OperationLog.objects.get(order_no=self.order.order_no)
        self.assertEqual(row.action, "order.claim")
        self.assertEqual(row.log_type, 10)
        self.assertEqual(row.target_name, taker.username)          # {接单人} 进名字列
        self.assertEqual(row.reason, "网页端大厅")
        self.assertEqual(row.to_value, "88.00")                     # 接手价
        self.assertEqual(row.actor, taker.username)

    def test_a_user_without_the_take_order_node_cannot_claim(self):
        """没有 `grab_hall.take_order` 的账号不得抢单 ⇒ **HTTP 403**（类级节点门）。

        判据是**节点口径**（职位名不是判据）：只给查看节点、不给抢单节点 ⇒ 类级门 403；
        订单与绩效一行都不动。
        已认证但无节点属**传输/认证层**（P5 §4.2），所以是 403 而不是业务码。
        📌 按游戏权限收窄）：本条的客服**不补**游戏权限（`games=False`）——
        它被断言的是「缺抢单节点 ⇒ 403」，补权限会让这枚钉子多一个无关的自由度。
        """
        self._login("客服", nodes=("order.view_detail",), games=False)
        response = self._post()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)
        # ⚠️ 必须断**msg**——403 有多个来源
        # （CSRF 失败 / 权限类 / 节点门），只断 code 区分不出来源。判据取节点门的默认文案，
        # 并显式排除 CSRF（与 `tests/test_p5_rbac.py` 的 403 可分辨口径同一形状）。
        msg = response.json()["msg"]
        self.assertIn(NODE_DENIED_MSG, msg)
        self.assertNotIn("CSRF", msg)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main().user_id)      # 绩效行**没被写上人**

    def test_claiming_a_non_hall_order_is_rejected(self):
        self.order.status = OrderStatus.UNASSIGNED
        self.order.save(update_fields=["status"])
        self._login("代练员")
        body = self._post().json()
        self.assertEqual(body["code"], ErrorCode.BUSINESS)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(self._main().user_id)

    def test_forbid_self_service_is_rejected_at_the_http_layer_too(self):
        """端到端：`/claim` 返回业务码 `1013`，msg 含「禁止自助接单」。
        落库判据：订单仍在池内、绩效行没有归属人（不看信封的 `data`，看**业务码**）。"""
        taker = self._login("代练员")
        # 夹具刻意放到「价格与上限都不拦」：`grab_limit_dl=0` ⇒ 不限制；接单价 88.00 在全局区间内。
        # （按游戏权限收窄）：`_login` 里的 `grant_games` 已经为这个账号建了
        # **同一条**档案（它必须有档案才看得见这单，否则拿到的是 1002 而不是这里要测的
        # 1013），所以这里改成**就地更新**那两列，不是再 `create` 一条（唯一约束会撞）。
        # 期望值一个字没改。
        StaffProfile.objects.filter(user=taker).update(
            grab_forbid_self_service=True, grab_limit_dl=0
        )

        response = self._post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("禁止自助接单", body["msg"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main().user_id)

    def test_claiming_an_already_taken_order_returns_its_own_code(self):
        """「已被接走」在**顺序**请求下也必须确定性地区分出来（`ClaimLogicTests` 用例 2 的 HTTP 版）。

        ⚠️ 这条是**判据的钉子**：来源态一旦只回 `BUSINESS`，双击/双角色的用户看到的
        就是「不在抢单大厅」而不是「手慢了」——前端无法区分「我点错了」与
        「别人抢先了」，而 spec §5 专门为后者留了 `ORDER_ALREADY_TAKEN`。
        """
        other = User.objects.create_user(username="taker-other")
        Performance.objects.filter(order=self.order).update(user=other)
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)
        self._login("代练员")

        body = self._post().json()

        self.assertEqual(body["code"], ErrorCode.ORDER_ALREADY_TAKEN)
        self.assertEqual(self._main().user_id, other.id)     # 归属人没被改

    def test_illegal_transition_returns_a_business_code_and_commits_nothing(self):
        """② 状态机拒绝时**不得提交那半截写入**（修法由 Ruling 48 定，
        可达性机制以 **Ruling 50 的更正**为准：今天可达的形状是「调用方节点集不含
        `grab_hall.take_order`」，不是「并发冲突」）。

        构造：打桩让 `claim_order` 里**第二笔**写入（`apply_transition`）抛
        `IllegalTransition`，桩在抛错前从**数据库**读回状态当哨兵——证明第一笔
        （绩效条件更新）**真的执行过**（判别力判据 2：桩打在第二笔 + 证明第一笔发生过）。
        ⚠️ 这条支路防的是「端点节点门被绕过 / 新调用点传了不含 `grab_hall.take_order`
        的节点集」——① 之后状态机**真的**会拒绝，于是这条 `except` 不再是装饰。
        判别力：去掉 `transaction.set_rollback(True)` ⇒ `return` 落在 `atomic()` 里
        ⇒ **提交**那半截写入 ⇒ 下面 `assertIsNone(self._main().user_id)` 变红，
        留下「绩效行有接单人、订单还停在待抢单」的脏行（spec §7.4 说任何数据库约束
        都表达不了、任何别的测试都抓不到的那种）。
        """
        taker = self._login("代练员")
        seen = {}

        def _boom(_order, _target, **_transition_options):
            seen["order_status"] = Order.objects.get(pk=self.order.pk).status
            seen["perf_user_id"] = Performance.objects.get(
                order=self.order, emp_type=EmpType.MAIN
            ).user_id
            raise IllegalTransition("非法状态迁移：UNASSIGNED → IN_PROGRESS")

        with mock.patch("apps.business.claim.apply_transition", side_effect=_boom):
            response = self._post()

        # 哨兵：第一笔写入**真的落过库**（否则「没留脏行」对「根本没走到那一步」同样成立）
        self.assertEqual(seen["perf_user_id"], taker.id)
        self.assertEqual(seen["order_status"], OrderStatus.WAITING_GRAB)
        # 预期失败 ⇒ 200 + 业务码，不是 500、也不是静默成功
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], ErrorCode.BUSINESS)
        self.assertIn("非法状态迁移", body["msg"])
        # 回滚：两笔都不留痕
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main().user_id)
        # **这里刻意没有新表断言**：`record`
        #    排在 `except` **之后**，而 `except` 里是早退 `return`，所以「没有留痕行」在
        #    **任何单一**变异下都恒真——只去掉 `transaction.set_rollback(True)` 时仍然早退；
        #    只去掉那个 `return`（让它往下走）时那一笔写入会被 `set_rollback` 的 savepoint
        #    回滚掉。能失败的断言才承担判别力，恒真的「形状完整」断言比不写更糟。

    def test_claim_is_not_exposed_on_the_order_router(self):
        """**Ruling 33**：claim 只挂在 spec §5 的 `/api/business/hall/...` 上。

        不做成 `@action` 正是为了**不**额外生成 `orders/{order_no}/claim/`——
        同一件事两个入口，其中一个还是被 spec 否决的写法。
        """
        self._login("代练员")
        response = self.client.post(
            f"/api/business/orders/{self.order.order_no}/claim/",
            {},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_claim_requires_authentication(self):
        """未认证 → 401（fail-closed 默认权限类，spec D11）。"""
        anonymous = Client(SERVER_NAME="tenant.test.com")
        response = anonymous.post(self.url, {}, content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)

    def test_malformed_query_params_on_claim_do_not_500(self):
        """⚠️ `/claim` **不在** `test_no_route_500` 的覆盖集合里（独立评审 Minor 6）。

        原因：`test_no_route_500.py` 会跳过带 `<str:order_no>` 这类**转换器**的路由
        （它按声明收集路由，带参数的路由无法用一个固定 URL 探测）。而 `/claim` 的
        `get_object()` 会走到 `get_queryset()` → `_search_params()`，也就是**同样吃查询参数**：
        `?game=abc` 这类畸形值必须被**同一个校验口**拦成 **200 + code 1000**
        （E-0010：请求来源的值进类型化查询前一律过序列化器），**不是 500、也不是 400**。

        判别力：把 `_search_params` 那层校验去掉（让 `?game=abc` 直接进 `filter()`）
        → `ValueError` 不在异常处理器的业务分支里 → **HTML 500**，本用例变红。
        """
        self._login("代练员")
        response = self.client.post(
            f"{self.url}?game=abc&price_max=xyz", {}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)          # 传输层成功
        self.assertEqual(response.json()["code"], ErrorCode.VALIDATION)   # 业务码 1000
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)     # 什么都没发生


class ClaimAtomicityTests(TenantTestCase):
    """中途失败**不留脏行**（spec §7.4 的 `/hall/{order_no}/claim` 行 / §10 #10）。

    做法与 `test_dispatch_api.py::DispatchAtomicityTests` 一致：桩打在**最后一笔**
    （`record`）上，并让桩在抛错前记下它看到的**数据库**状态。
    没哨兵时，「第二笔没留下脏行」对「请求根本没走到第一笔写入」同样成立
    （判别力判据 2：桩必须在第二笔上，且要证明第一笔真的发生过）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no=_no(1),
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        self.perf = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        self.taker = make_logged_in_client(
            username="atomic-taker", groups=("代练员",),
            nodes=("grab_hall.take_order",),
        )
        self.taker_user = User.objects.get(username="atomic-taker")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 本条要证明「两笔写入都回滚」，前提是请求真的走到了写入——而 `/claim` 经
        # `get_object()`（被 `game_scope` 收窄的 queryset）取单 ⇒ 抢单账号必须显式
        # 持有这单的游戏；缺档案 ⇒ 空集 ⇒ 提前 1002，回滚根本无从发生。
        # 期望值一个字没改。
        grant_games(self.taker_user, self.game)

    def test_claim_rolls_back_when_the_audit_write_fails(self):
        """`/claim` 的 §7.4 行 = ① 绩效 `user`（条件更新）② `Order.status`。两笔都要回滚。"""
        url = f"/api/business/hall/{self.order.order_no}/claim/"
        seen = {}

        def _boom(**kwargs):
            seen["order_status"] = Order.objects.get(pk=self.order.pk).status
            seen["perf_user_id"] = Performance.objects.get(pk=self.perf.pk).user_id
            raise RuntimeError("boom")

        with mock.patch(
            "apps.business.api.order_views.record", side_effect=_boom
        ):
            with self.assertRaises(RuntimeError):
                self.taker.post(url, {}, content_type="application/json")

        # 哨兵：两笔写入在抛错前**都已经落到库里**（同一个事务里读得到自己未提交的写入）
        self.assertEqual(seen["order_status"], OrderStatus.IN_PROGRESS)
        self.assertEqual(seen["perf_user_id"], self.taker_user.id)
        # 回滚：两笔都不留痕
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self.perf.user_id)


# ── （Z2）：统一锁序「先锁 Order、再锁钱包」──────────────
class LockOrderTests(TenantTestCase):
    """**抓 SQL 钉锁序**（主判据）。

    为什么不用「双线程跑一次看它死不死」当判据：死锁不保证复现（调度、行数、机器负载都能
    让它溜过去），绿了的并发用例等于没测。这里直接看 `claim_order` 这次调用发出的
    `SELECT … FOR UPDATE` **顺序**：Order 行的锁必须早于钱包表的锁。

    ⚠️ 两条用例各自覆盖一条**会碰钱包**的边：
    ① 抢单（`cancel_release_on_claim` 无条件拿钱包行锁）；
    ② 退款钩子（`WAITING_GRAB → REFUNDED`，`apply_transition` 先锁 Order 行，随后
       `on_order_refunded` 的第 1.55 步中断提现 ⇒ 拿钱包行锁）。
    两条都**不是**「先锁钱包再锁 Order」——那正是 Z2 要消掉的 AB-BA 形状。
    """

    def setUp(self):
        self.game = Game.objects.create(name="锁序游戏")
        self.taker = User.objects.create_user(username="lock-order-taker")

    def _claimable_order(self, no):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        return order

    def _assert_order_lock_precedes_wallet_lock(self, captured):
        order_table = Order._meta.db_table
        wallet_table = TakerWallet._meta.db_table
        locking = [
            (index, query["sql"])
            for index, query in enumerate(captured.captured_queries)
            if "FOR UPDATE" in query["sql"].upper()
        ]
        order_locks = [
            index for index, sql in locking if f'"{order_table}"' in sql
        ]
        wallet_locks = [
            index for index, sql in locking if f'"{wallet_table}"' in sql
        ]
        self.assertTrue(
            order_locks,
            "本次调用没有锁订单行（Z2 的 Order 行锁被删了？）：%r" % (locking,),
        )
        self.assertTrue(
            wallet_locks,
            "本次调用没有碰钱包行锁 —— 用例没走到要测的那条边：%r" % (locking,),
        )
        self.assertLess(
            order_locks[0],
            wallet_locks[0],
            "锁序反了：钱包行锁（query #%d）早于订单行锁（query #%d）—— Z2 的 AB-BA "
            "形状回来了：%r"
            % (wallet_locks[0], order_locks[0], locking),
        )

    def test_claim_locks_the_order_row_before_the_wallet(self):
        order = self._claimable_order("20261025000000009201")
        with transaction.atomic():
            with CaptureQueriesContext(connection) as captured:
                claim_order(
                    user_id=self.taker.id,
                    order_id=order.id,
                    nodes={"grab_hall.take_order"},
                    # 押金闸不在本用例的射程内（它会在钱包锁之前多一层 ensure）⇒ 显式关，
                    # 让「首次碰钱包」就是 `cancel_release_on_claim` 那一句。
                    enforce_deposit=False,
                )
        self._assert_order_lock_precedes_wallet_lock(captured)
        # 哨兵：抢单真的成功了（锁序断言不是在一笔失败的调用上做的）。
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    def test_the_refund_hook_locks_the_order_before_the_wallet(self):
        # ⚠️ 夹具刻意让「待抢单」的单上仍有一条**带人**的 MAIN 行：D-0032 的正常形态是池内
        #    无接单人，而本用例要打的是退款钩子的「有接单人 ⇒ 中断他的提现」那一支
        #    （第 1.55 步）——那是本边**唯一**会拿钱包锁的地方。脏夹具在这里是**目的**，
        #    不是疏忽：钩子按 `Performance.user` 判，不按订单状态判。
        order = Order.objects.create(
            game=self.game,
            order_no="20261025000000009202",
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
            total_price=Decimal("130.00"),
        )
        Performance.objects.create(
            order=order,
            user=self.taker,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("17.60"),
            bonus_amount=Decimal("17.60"),
        )
        WithdrawOrder.objects.create(
            user=self.taker,
            amount=Decimal("10.00"),
            alipay_account="synthetic@example.test",
            alipay_name="合成",
            state=WithdrawOrder.STATE_APPROVED,
        )
        with transaction.atomic():
            with CaptureQueriesContext(connection) as captured:
                apply_transition(
                    order, OrderStatus.REFUNDED,
                    nodes={"order.change_status"}, actor="锁序用例",
                )
        self._assert_order_lock_precedes_wallet_lock(captured)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)


class DailiantongPriorityTests(TenantTestCase):
    """⭐ **代练通优先**（业务方口径：「代练通先被接 ⇒ 内部大厅抢单直接拒绝」）。

    - 远端**已经有人接手**（`remote_status` 不在 {10, 11}，或撤销码表明接手过）⇒ 拒；
    - 远端**只是被挂上平台、还没人接**（`Status=11`）⇒ 内部照旧能抢（随后同步把
      代练通订单撤掉，判别力由 `test_dailiantong_sync` 的并发组承担）；
    - 还没回读过（`remote_status is None`）⇒ 不算已接手（⛔ 不能因为「不知道」就拒）。

    断言打在**数据库终态**上：被拒时 MAIN 行的 `user` 仍为空、订单仍在待抢单。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.taker = User.objects.create_user(username="dlt-priority-taker")

    def _order(self, no: str, status=OrderStatus.WAITING_GRAB, price="88.00"):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            hall_price=Decimal(price),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal(price)
        )
        return order

    def _push(self, order, *, status: int | None, cancel_status: int = 10):
        return DailiantongPush.objects.create(
            order=order,
            platform=10,
            state=DailiantongPush.State.SENT,
            serial_no="19000000000000000299",
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=order.order_no,
            remote_status=status,
            remote_cancel_status=cancel_status,
            remote_taker_name="平台合成打手",
            attempts=1,
        )

    def _claim(self, order):
        return claim_order(
            user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"}
        )

    def test_a_platform_accepted_order_cannot_be_claimed_internally(self):
        order = self._order(_no(901))
        self._push(order, status=12)

        with self.assertRaises(ClaimError) as ctx:
            self._claim(order)

        self.assertEqual(ctx.exception.code, ErrorCode.ORDER_ALREADY_TAKEN)
        self.assertIn("代练通", str(ctx.exception))
        self.assertIn("平台合成打手", str(ctx.exception))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )

    def test_the_cancel_status_alone_proves_the_order_was_accepted(self):
        order = self._order(_no(902))
        # 协商撤销中（11）：远端 `Status` 暂时还显示 11，但撤销流程本身就意味着有人接过。
        self._push(order, status=11, cancel_status=11)

        with self.assertRaises(ClaimError) as ctx:
            self._claim(order)

        self.assertEqual(ctx.exception.code, ErrorCode.ORDER_ALREADY_TAKEN)

    def test_an_unaccepted_platform_order_can_still_be_claimed_internally(self):
        """`Status=11`（只挂上平台、还没人接）⇒ 内部先接是允许的（A4 第 2 条的前提）。"""
        order = self._order(_no(903))
        self._push(order, status=11)

        self._claim(order)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            self.taker.id,
        )

    def test_a_push_that_was_never_read_back_does_not_block_the_claim(self):
        order = self._order(_no(904))
        self._push(order, status=None)

        self._claim(order)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    def test_a_failed_push_does_not_block_the_claim(self):
        order = self._order(_no(905))
        push = self._push(order, status=12)
        push.state = DailiantongPush.State.FAILED
        push.save(update_fields=["state"])

        self._claim(order)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    def test_an_off_shelf_platform_order_is_not_accepted_and_can_be_claimed(self):
        """⭐ `Status=18` **已下架** = 没有在卖、没有打手 ⇒ 不算已接手。

        旧实现把它当「已接手」，内部大厅抢单被误挡（业务方会看到「已被代练通接单」，
        其实只是被下架了）。判据钉在两处：`remote_is_accepted()` 本体 + 抢单闸的返回值，
        再用一次真实 `claim_order` 断言终态（MAIN 行归内部打手、订单进「代练中」）。
        """
        from apps.business.dailiantong_push import (
            claim_blocked_by_platform_denial,
            remote_is_accepted,
        )

        order = self._order(_no(906))
        push = self._push(order, status=18)
        self.assertFalse(
            remote_is_accepted(push), "18 已下架不算已接手（与 10 / 11 同一族）"
        )
        self.assertIsNone(claim_blocked_by_platform_denial(order))

        self._claim(order)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            self.taker.id,
        )

    def test_a_cancel_status_still_proves_acceptance_when_status_reads_off_shelf(self):
        """⭐ `Status=18` 只否掉「**Status** 证明已接手」这一支；`CancelStatus` 仍是证据。

        撤销流程已经动起来（协商中 / 已处理 / 客服介入…）本身就说明**确实有人接过**，
        即使此刻 `Status` 读出来是 18，也必须继续按「已接手」挡内部抢单。
        """
        order = self._order(_no(907))
        self._push(order, status=18, cancel_status=11)

        with self.assertRaises(ClaimError) as ctx:
            self._claim(order)

        self.assertEqual(ctx.exception.code, ErrorCode.ORDER_ALREADY_TAKEN)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
