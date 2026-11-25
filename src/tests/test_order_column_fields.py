"""Task 1：「完单时间」`Order.completed_at` 与「发单人」`Order.hall_publisher` 的写入 / 清理路径。

对应 spec §4（数据模型）与 §6（写入点与清理路径）：

- `completed_at`：进「已完成」写值；**改回其它状态清空**；只 PATCH 别的字段**不动它**。
- `hall_publisher`：抛单写请求用户；撤回**保留**；再抛**覆盖**；指派（含改派）
  **也写**（D-0369，2026-11-03 业务方口径：谁把单指派出去，发单人就是谁，
  「保留最后一次」跨动作类型成立）；`/claim` **不动它**（抢单不算发单动作）；
  通用 `/transition/` **只有**目标态是 `WAITING_GRAB` 时才写（F5，2026-09-23
  收口：两条进大厅的路走同一漏斗），其余目标态不动它
  （清理路径按业务方裁定 8 = 「无，保留最后一次」）。

## 为什么断言一律打在**数据库行**上（本文件的核心纪律）

`apply_transition` 与 `throw_to_hall` 都用 `save(update_fields=[...])`：**没列进
`update_fields` 的新列会被静默丢弃**——而视图手上那份内存对象**仍然是对的**
（值只落在对象上、没落库），响应序列化器读的也是那份内存对象。
所以每个用例都用 `Order.objects.get(pk=...)` **重新取行**再断言；
把 `completed_at` / `hall_publisher` 从对应的 `update_fields` 里删掉，这些用例会变红
（本任务报告里有一条把 `completed_at` 从 `update_fields` 删掉的变异验证）。

## 其它两条

- **走真 HTTP 端点**（`make_logged_in_client` 真实登录，spec D11 的 fail-closed 默认类）：
  状态与抛单的产品出口就是这些视图，直连 ORM 会绕开视图里的保存方式，测不到本任务要钉的东西。
- 期望值写**字面值**（E-0024），不引用被测代码导出的常量。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 客服在本文走到的节点门（`OrderViewSet.ORDER_PERMISSIONS`； 起 fail-closed）。
#: `groups` 只建 Group、**不放行** ⇒ 每个真实登录的夹具都要显式声明它要过的门。
CS_NODES = (
    "order.add_edit",                  # PATCH
    "order.change_status",             # `/transition/`（目标非「已完成」）
    "order.change_status_completed",   # `/transition/` 目标「已完成」（节点 17）
    "order.set_offer_price",           # `/hall-price/`（改发单价）
    "order.send_to_grab_hall",         # `/throw-to-hall/`（2026-10-26 起**独占闸**）
    "order.submit_payout_price",
    "order.withdraw_from_grab_hall",   # `/withdraw/`
    "order.assign_taker",              # `/assign/`
)


def _no(n):
    """20 位订单号（`OrderViewSet` 的 `lookup_value_regex = r"\\d{20}"`）。"""
    return f"20260920{n:012d}"


class _ColumnFieldTestCase(TenantTestCase):
    """本文件的共用夹具。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")

    def _make_order(self, n, status=OrderStatus.UNASSIGNED):
        """建一对「订单 + MAIN 绩效」。

        绩效行不是可选的：D-0032 之后「迁入池内两态」的回退钩子与 `/claim` 的条件更新
        都打在**那条** MAIN 行上（`claim.py` 的 `emp_type = '打单工资'` 过滤）。
        ⚠️ `project_text` 必须有值：`partial_update` 会把「本次没传的项目文本」合并进来
        再走 `validate_required`，空串会让 PATCH 以业务码 1000 被拒（与本任务无关的噪音）。
        """
        order = Order.objects.create(
            game=self.game,
            order_no=_no(n),
            status=status,
            hall_price=Decimal("88.00"),
            project_text="满星",
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        return order

    def _cs(self, username):
        """一个真实登录的客服，返回 `(client, user)`。

        `/transition/`、抛单、撤回、指派都要客服身份（`roles_of` + 逐 action 的节点门）。
        """
        client = make_logged_in_client(
            username=username, groups=("客服",), nodes=CS_NODES
        )
        user = User.objects.get(username=username)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 本文件的每个客服都要经 `get_object()` 读/写 `self.game` 上那一行
        # （详情 / 改单 / 流转 / 抛单 / 撤回 / 指派都走同一条被 `game_scope` 收窄的
        # queryset）⇒ 缺 `StaffProfile` 会让它们拿 1002 / 1000。期望值一个字没改。
        grant_games(user, self.game)
        return client, user

    def _row(self, order):
        """从数据库**重新取行**——本文件唯一的断言对象（E-0009）。"""
        return Order.objects.get(pk=order.pk)

    def _transition(self, client, order, target):
        return client.post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": target},
            content_type="application/json",
        ).json()

    def _throw(self, client, order, payload=None):
        return client.post(
            f"/api/business/orders/{order.order_no}/throw-to-hall/",
            payload or {},
            content_type="application/json",
        ).json()

    def _withdraw(self, client, order):
        return client.post(
            f"/api/business/orders/{order.order_no}/withdraw/",
            {},
            content_type="application/json",
        ).json()


class CompletedAtWritePathTests(_ColumnFieldTestCase):
    """`completed_at` 的写入点（进「已完成」）与清理路径（离开「已完成」）。"""

    def test_a_fresh_order_has_no_completed_at(self):
        """历史行 / 新行天然为空（spec §4：两列都允许为空）。"""
        row = self._row(self._make_order(1))
        self.assertIsNone(row.completed_at)

    def test_entering_completed_writes_completed_at_to_the_database_row(self):
        """进「已完成」⇒ 库里的行有时间戳。

        ⚠️ **判别力**：把 `completed_at` 从 `apply_transition` 的 `update_fields` 里删掉，
        库里的行仍是 `None`（内存对象上有值）⇒ 本用例红。这正是本项目最忌的失败形态。
        """
        cs, _cs_user = self._cs("cs-complete")
        # ⚠️ spec §2.3：入「已完成」的边只剩
        #    「待验收 → 已完成」⇒ 完成那几条用例显式把来源态放在「待验收」。
        order = self._make_order(2, status=OrderStatus.PENDING_REVIEW)
        before = timezone.now()
        self.assertEqual(self._transition(cs, order, OrderStatus.COMPLETED)["code"], 0)
        row = self._row(order)
        self.assertEqual(row.status, OrderStatus.COMPLETED)
        self.assertIsNotNone(row.completed_at)
        self.assertGreaterEqual(row.completed_at, before)

    def test_leaving_completed_clears_completed_at_in_the_database_row(self):
        """从「已完成」改回其它状态 ⇒ 库里的行变回 `None`（问题单 / 已暂停两档）。"""
        cs, _cs_user = self._cs("cs-reopen")
        for n, target in ((10, OrderStatus.ISSUE), (11, OrderStatus.PAUSED)):
            with self.subTest(target=target):
                # F3：完成的合法来源态是「待验收」（见上一条）
                order = self._make_order(n, status=OrderStatus.PENDING_REVIEW)
                self.assertEqual(
                    self._transition(cs, order, OrderStatus.COMPLETED)["code"], 0
                )
                # 非空哨兵：不先证明「真的写上过」，下面的 assertIsNone 可能恒真
                self.assertIsNotNone(self._row(order).completed_at)
                self.assertEqual(self._transition(cs, order, target)["code"], 0)
                row = self._row(order)
                self.assertEqual(row.status, target)
                self.assertIsNone(row.completed_at)

    def test_patching_another_field_leaves_completed_at_untouched(self):
        """PATCH 别的字段（备注）⇒ 完成时间**不变**（既不清空也不刷新）。"""
        cs, _cs_user = self._cs("cs-patch")
        order = self._make_order(20, status=OrderStatus.PENDING_REVIEW)
        self.assertEqual(self._transition(cs, order, OrderStatus.COMPLETED)["code"], 0)
        stamp = self._row(order).completed_at
        self.assertIsNotNone(stamp)                      # 哨兵：先真的写上了
        response = cs.patch(
            f"/api/business/orders/{order.order_no}/",
            {"remark": "只改备注"},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0)
        self.assertEqual(self._row(order).completed_at, stamp)


class HallPublisherWritePathTests(_ColumnFieldTestCase):
    """`hall_publisher` 的写入点（抛大厅 + 指派，D-0369）与「保留最后一次」的口径。"""

    def test_a_fresh_order_has_no_hall_publisher(self):
        row = self._row(self._make_order(30))
        self.assertIsNone(row.hall_publisher_id)

    def test_throw_to_hall_writes_the_publisher_to_the_database_row(self):
        """不带价抛单 ⇒ 库里那一行的发单人 = 请求用户。

        ⚠️ 这一档**必须**有：不带价时 `throw_to_hall` 原本跳过它那次
        `save(update_fields=[...])`，发单人会静默丢掉。
        """
        cs, cs_user = self._cs("cs-throw")
        order = self._make_order(31)
        self.assertEqual(self._throw(cs, order)["code"], 0)
        row = self._row(order)
        self.assertEqual(row.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(row.hall_publisher_id, cs_user.id)
        self.assertEqual(row.hall_publisher.username, "cs-throw")

    def test_throw_to_hall_with_a_price_also_writes_the_publisher(self):
        """带价抛单（另一条保存分支）同样写发单人，且发单价照旧落库。"""
        cs, _cs_user = self._cs("cs-throw-price")
        order = self._make_order(32)
        self.assertEqual(self._throw(cs, order, {"price": "66.00"})["code"], 0)
        row = self._row(order)
        self.assertEqual(row.hall_price, Decimal("66.00"))
        self.assertEqual(row.hall_publisher.username, "cs-throw-price")

    def test_withdraw_keeps_the_publisher(self):
        """撤回**保留**最后一次的发单人（清理路径 = 无，业务方裁定 8）。"""
        cs, cs_user = self._cs("cs-withdraw")
        order = self._make_order(33)
        self.assertEqual(self._throw(cs, order)["code"], 0)
        self.assertEqual(self._row(order).hall_publisher_id, cs_user.id)   # 哨兵
        self.assertEqual(self._withdraw(cs, order)["code"], 0)
        row = self._row(order)
        self.assertEqual(row.status, OrderStatus.UNASSIGNED)
        self.assertEqual(row.hall_publisher_id, cs_user.id)
        self.assertEqual(row.hall_publisher.username, "cs-withdraw")

    def test_rethrowing_overwrites_the_publisher(self):
        """撤回后再抛 ⇒ 覆盖成**新的**那个人（保留的是最后一次，不是第一次）。"""
        first, _first_user = self._cs("cs-first")
        order = self._make_order(34)
        self.assertEqual(self._throw(first, order)["code"], 0)
        self.assertEqual(self._row(order).hall_publisher.username, "cs-first")  # 哨兵
        self.assertEqual(self._withdraw(first, order)["code"], 0)
        second, _second_user = self._cs("cs-second")
        self.assertEqual(self._throw(second, order)["code"], 0)
        self.assertEqual(self._row(order).hall_publisher.username, "cs-second")

    def test_assign_overwrites_the_publisher_with_the_operator(self):
        """指派也写发单人（D-0369，2026-11-03 业务方口径）：谁指派，发单人就是谁。

        钉的是「保留最后一次」**跨动作类型**成立：抛大厅先写 A（走漏斗），
        撤回不动它，随后**指派**把它覆盖成操作人 B（走 `assign_order` 的直写点）。
        ⚠️ 判别力：把 `order_writes.assign_order` 里那三句直写删掉 ⇒ 发单人仍是
        抛大厅的人 ⇒ 本用例红。B 与 A 用**不同账号**（同一个人覆盖看不出「换人」）。
        ⚠️ 目标打手要 `grant_games`（D-0252 游戏范围闸，无档案 = 拒）。
        """
        thrower, _thrower_user = self._cs("cs-throw-then-assign")
        order = self._make_order(40)
        self.assertEqual(self._throw(thrower, order)["code"], 0)
        self.assertEqual(self._withdraw(thrower, order)["code"], 0)
        self.assertEqual(
            self._row(order).hall_publisher.username, "cs-throw-then-assign"
        )  # 哨兵：抛大厅真的写过
        taker = User.objects.create_user(username="taker-assign-pub")
        # ⭐ D-0252（2026-10-18）：指派闸判「目标打手游戏范围」⇒ 目标也要授权。
        grant_games(taker, self.game)
        assigner, assigner_user = self._cs("cs-assign-wins")
        body = assigner.post(
            f"/api/business/orders/{order.order_no}/assign/",
            {"user_id": taker.id},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        row = self._row(order)
        self.assertEqual(row.status, OrderStatus.IN_PROGRESS)     # 哨兵：指派真的发生了
        self.assertEqual(row.hall_publisher_id, assigner_user.id)
        self.assertEqual(row.hall_publisher.username, "cs-assign-wins")

    def test_reassign_on_an_in_progress_order_overwrites_the_publisher(self):
        """改派（单已 `IN_PROGRESS`，迁移被 R-3 跳过）同样写发单人（D-0369）。

        这条专门钉 **R-3 跳过迁移那一档的直写支**：改派支没有 `apply_transition`
        可走 ⇒ 发单人的覆盖只能来自 `assign_order` 里的直写点。
        ⛔ 漏了它，直写支对「改派」就没有判别力（上面那条只覆盖迁移支）。
        """
        first, _first_user = self._cs("cs-first-assign")
        order = self._make_order(42)
        taker_b = User.objects.create_user(username="taker-reassign-b")
        taker_c = User.objects.create_user(username="taker-reassign-c")
        grant_games(taker_b, self.game)
        grant_games(taker_c, self.game)
        # 首次指派（迁移支）：B 接手 ⇒ 发单人 = 首次指派人。
        body = first.post(
            f"/api/business/orders/{order.order_no}/assign/",
            {"user_id": taker_b.id},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        row = self._row(order)
        self.assertEqual(row.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(row.hall_publisher.username, "cs-first-assign")   # 哨兵
        # 改派（R-3 跳过迁移那一档）：C 接手 ⇒ 发单人覆盖成改派人。
        second, second_user = self._cs("cs-reassign-wins")
        body = second.post(
            f"/api/business/orders/{order.order_no}/assign/",
            {"user_id": taker_c.id},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        row = self._row(order)
        self.assertEqual(row.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(row.hall_publisher_id, second_user.id)
        self.assertEqual(row.hall_publisher.username, "cs-reassign-wins")

    def test_reassigning_by_the_same_operator_keeps_the_publisher(self):
        """同一个人重复指派 ⇒ 发单人不变（幂等：覆盖成同一个人 = 没变化）。"""
        assigner, assigner_user = self._cs("cs-same-operator")
        order = self._make_order(43)
        taker = User.objects.create_user(username="taker-same-operator")
        grant_games(taker, self.game)
        for round_no in (1, 2):
            body = assigner.post(
                f"/api/business/orders/{order.order_no}/assign/",
                {"user_id": taker.id},
                content_type="application/json",
            ).json()
            self.assertEqual(body["code"], 0, body)
            row = self._row(order)
            self.assertEqual(row.status, OrderStatus.IN_PROGRESS)   # 哨兵
            self.assertEqual(row.hall_publisher_id, assigner_user.id)

    def test_transition_does_not_touch_the_publisher(self):
        """通用 `/transition/` 改到**非 `WAITING_GRAB`** 的目标态时不动发单人（负对照）。

        ⚠️ F5）起这条**只覆盖「除 `WAITING_GRAB` 外的目标态」**：业务方口径是
        「发单人 = 把订单抛到大厅的人」⇒ 通用 `/transition/` 进 `WAITING_GRAB` 时**是**写入点
        （正向用例见 `test_generic_transition_into_the_grab_hall_writes_the_publisher`）。
        原来那句「通用 `/transition/` 不动发单人」在 F5 之后就是假的，已按实现改准。
        """
        cs, _cs_user = self._cs("cs-transition")
        order = self._make_order(41)
        self.assertEqual(self._throw(cs, order)["code"], 0)
        self.assertEqual(self._row(order).hall_publisher.username, "cs-transition")  # 哨兵
        self.assertEqual(
            self._transition(cs, order, OrderStatus.UNASSIGNED)["code"], 0
        )
        row = self._row(order)
        self.assertEqual(row.status, OrderStatus.UNASSIGNED)
        self.assertEqual(row.hall_publisher.username, "cs-transition")

    def test_generic_transition_into_the_grab_hall_writes_the_publisher(self):
        """通用 `/transition/` 把订单改到 `WAITING_GRAB` ⇒ 写发单人（F5，2026-09-23 收口）。

        两个来源态都试（`UNASSIGNED` 与 `ISSUE`）：写不写只由**目标态**决定，与从哪来无关。

        ⚠️ **判别力**：把 `order_writes.transition_order` 传给 `apply_transition` 的
        `hall_publisher=ctx.user if str(target) == OrderStatus.WAITING_GRAB else None`
        去掉 ⇒ 库里的行仍是 `NULL` ⇒ 本用例红。
        （A3 起这条漏斗在领域核心里；搬迁前它在 `order_views.transition` 的动作体内。）
        ⚠️ **非空哨兵**：先断 `status` 真的变成 `WAITING_GRAB`——否则「这条边根本没走通」与
        「走了但没写发单人」在断言上不可区分。
        ⚠️ 断言打在**数据库行**上（`_row`）：内存对象上有值不算数（本文件的核心纪律）。
        """
        cs, cs_user = self._cs("cs-transition-grab")
        for n, source in ((70, OrderStatus.UNASSIGNED), (71, OrderStatus.ISSUE)):
            with self.subTest(source=source):
                order = self._make_order(n, status=source)
                self.assertEqual(
                    self._transition(cs, order, OrderStatus.WAITING_GRAB)["code"], 0
                )
                row = self._row(order)
                self.assertEqual(row.status, OrderStatus.WAITING_GRAB)   # 哨兵
                self.assertEqual(row.hall_publisher_id, cs_user.id)
                self.assertEqual(row.hall_publisher.username, "cs-transition-grab")

    def test_claim_does_not_touch_the_publisher(self):
        """`/claim` 不动发单人（抢单只改绩效行的归属与订单状态）。"""
        cs, _cs_user = self._cs("cs-claim")
        order = self._make_order(50)
        self.assertEqual(self._throw(cs, order)["code"], 0)
        self.assertEqual(self._row(order).hall_publisher.username, "cs-claim")  # 哨兵
        taker = make_logged_in_client(
            username="taker-claim",
            groups=("代练员",),
            nodes=("grab_hall.take_order",),
        )
        # （按游戏权限收窄）：**夹具补齐**。`/claim/` 也经 `get_object()`
        # （同一条被 `game_scope` 收窄的 queryset）⇒ 抢单的账号同样要持有这单的游戏。
        grant_games(User.objects.get(username="taker-claim"), self.game)
        body = taker.post(
            f"/api/business/hall/{order.order_no}/claim/",
            {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        row = self._row(order)
        self.assertEqual(row.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(row.hall_publisher.username, "cs-claim")


class PublishedAtWritePathTests(_ColumnFieldTestCase):
    """`Order.published_at`（报酬页「发单时间」那一档）= **第一次被发出去给打手**。

    业务方拍板选 A：两个来源**取先发生的那个** —— ①抛到内部抢单大厅；
    ②第一次接手（`/claim` 与 `/assign` 都算接手）。

    ## 本类逐条钉的四条口径（每条都有会红的用例，不靠「跑完不报错」）

    1. 两个来源**各自都能写**（抛大厅、抢单、指派 —— 三条路各一条），且**三条都是同一条口径**；
    2. **只写第一次**：撤回重抛 / 二次接手都**不覆盖**（它是「最早什么时候发出去的」）；
    3. **与同行 `claimed_at` 同一时刻**（接手那两条路：写的是**同一个** `now` 对象，
       微秒级也要相等 —— 各取一次 `timezone.now()` 会差几微秒，对账时看着像两件事）；
    4. **不擦**：`apply_transition`（它随后就跑）**不能**用调用方手上那份陈旧 `order` 里的
       `published_at=None` 把刚落库的值改成 NULL（`.update()` 与 `save(update_fields=…)` 的
       交互，是本批实测出来的坑，见 `order_status.apply_transition` 那段 ⛔ 注释）。

    ⚠️ 断言一律打在**数据库行**上（本文件的核心纪律）：内存对象上有值不算数。
    """

    def _taker(self, username):
        client = make_logged_in_client(
            username=username, groups=("代练员",), nodes=("grab_hall.take_order",)
        )
        grant_games(User.objects.get(username=username), self.game)
        return client

    def test_a_fresh_order_has_no_published_at(self):
        row = self._row(self._make_order(60))
        self.assertIsNone(row.published_at)

    def test_throw_to_hall_writes_published_at_to_the_database_row(self):
        """抛大厅后从数据库读到发单时间；首次写入保护见 MarkFunctionsTests。"""
        cs, _cs_user = self._cs("cs-pub-throw")
        order = self._make_order(61)
        before = timezone.now()
        self.assertEqual(self._throw(cs, order)["code"], 0)
        after = timezone.now()
        row = self._row(order)
        self.assertEqual(row.status, OrderStatus.WAITING_GRAB)      # 哨兵（这条边真的走了）
        self.assertIsNotNone(row.published_at)
        self.assertGreaterEqual(row.published_at, before)
        self.assertLessEqual(row.published_at, after)

    def test_claim_writes_published_at_equal_to_claimed_at(self):
        """②抢单（接手）⇒ 写发单时间，且与**同一行**的 `claimed_at` **逐微秒相等**。

        ⚠️ 这一条同时是「`apply_transition` 没把刚写的值擦掉」的判据：抢单那条路是
        `claim.py` 先 `.update()` 写库、随后 `apply_transition` 才改状态；后者手上那份
        `order` 是**函数开头读的**（`published_at` 还是 `None`）⇒ 一旦它无条件把这列列进
        `update_fields`，这里读到的就是 `None`，本用例当场红。

        ⚠️ 相等判据取**微秒级**（`claimed_at` / `published_at` 都是 `DateTimeField`，
        取出的是 `datetime`）——「同一时刻」这件事在实现里就是**同一个 `now` 对象**。
        """
        cs, _cs_user = self._cs("cs-pub-claim")
        order = self._make_order(62)
        self.assertEqual(self._throw(cs, order)["code"], 0)
        self.assertIsNotNone(self._row(order).published_at)          # 哨兵：抛单已写过
        # 把这张单**退回大厅的原始状态**，并清掉发单时间 ⇒ 让「抢单」成为**唯一**可能写出
        # 这一列的动作（否则本用例在「抢单那一支被删掉」时照样绿 —— 值还是抛单那次留下的）。
        # ⚠️ 走 `.update()` 而不是 `/transition/`：`/claim/` 的来源态守卫要求
        #    `status == WAITING_GRAB`（转到 `UNASSIGNED` 再抢会被判「不在抢单大厅」，码 1002）。
        Order.objects.filter(pk=order.pk).update(
            status=OrderStatus.WAITING_GRAB, published_at=None
        )
        self.assertIsNone(self._row(order).published_at)             # 阴性对照
        taker = self._taker("taker-pub-claim")
        body = taker.post(
            f"/api/business/hall/{order.order_no}/claim/",
            {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        row = self._row(order)
        self.assertEqual(row.status, OrderStatus.IN_PROGRESS)        # 哨兵
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertIsNotNone(perf.claimed_at)                        # 哨兵：这一列真被写了
        self.assertEqual(row.published_at, perf.claimed_at)

    def test_assign_writes_published_at_equal_to_claimed_at(self):
        """②指派（另一条接手路径，`performance.set_taker`）⇒ 同样写、同样与 `claimed_at` 同刻。"""
        cs, _cs_user = self._cs("cs-pub-assign")
        taker = User.objects.create_user(username="taker-pub-assign")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权。
        grant_games(taker, self.game)
        order = self._make_order(63)
        self.assertIsNone(self._row(order).published_at)             # 阴性对照：还没发出去
        body = cs.post(
            f"/api/business/orders/{order.order_no}/assign/",
            {"user_id": taker.id},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        row = self._row(order)
        self.assertEqual(row.status, OrderStatus.IN_PROGRESS)        # 哨兵
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertIsNotNone(perf.claimed_at)                        # 哨兵
        self.assertIsNotNone(row.published_at)
        self.assertEqual(row.published_at, perf.claimed_at)

    def test_a_second_throw_does_not_overwrite(self):
        """**只写第一次**：撤回后重抛 ⇒ 值**不变**（钉的是**值**，⛔ 不是「非空」）。

        ⚠️ 与 `hall_publisher` 的「保留最后一次」**有意相反**：这一列的语义是
        「这张单**最早**什么时候发出去的」。
        """
        cs, _cs_user = self._cs("cs-pub-twice")
        order = self._make_order(64)
        self.assertEqual(self._throw(cs, order)["code"], 0)
        first = self._row(order).published_at
        self.assertIsNotNone(first)                                   # 哨兵
        self.assertEqual(self._withdraw(cs, order)["code"], 0)
        # ⚠️ **撤回也不清**（它是筛选维度，清掉历史就筛不出来）——先钉这一条。
        self.assertEqual(self._row(order).published_at, first)
        self.assertEqual(self._throw(cs, order)["code"], 0)
        self.assertEqual(self._row(order).published_at, first)

    def test_published_at_survives_leaving_completed(self):
        """**不清空**（与 `completed_at` 离开已完成后会清**有意不同**）。

        路径：抛单 → 抢单 → 交单（进待验收）→ 完成 → 改回待验收。
        ⚠️ 必须逐跳走（`IN_PROGRESS → COMPLETED` **不是合法边**：D-2026-09-25 裁定
        「任何订单不能直接改已完成，必须从『待验收』改已完成」）。
        改回之后 `published_at` 与 `submitted_at` 都必须**原样还在**，而 `completed_at` 清空。
        """
        cs, _cs_user = self._cs("cs-pub-sticky")
        order = self._make_order(65)
        self.assertEqual(self._throw(cs, order)["code"], 0)
        published = self._row(order).published_at
        self.assertIsNotNone(published)                               # 哨兵
        taker = self._taker("taker-pub-sticky")
        self.assertEqual(
            taker.post(
                f"/api/business/hall/{order.order_no}/claim/",
                {}, content_type="application/json",
            ).json()["code"],
            0,
        )
        self.assertEqual(
            self._transition(cs, order, OrderStatus.PENDING_REVIEW)["code"], 0
        )
        submitted = self._row(order).submitted_at
        self.assertIsNotNone(submitted)                               # 哨兵
        self.assertEqual(
            self._transition(cs, order, OrderStatus.COMPLETED)["code"], 0
        )
        self.assertIsNotNone(self._row(order).completed_at)           # 哨兵：真的完成过
        self.assertEqual(
            self._transition(cs, order, OrderStatus.PENDING_REVIEW)["code"], 0
        )
        row = self._row(order)
        self.assertIsNone(row.completed_at)                           # 既有口径：这一列会清
        self.assertEqual(row.published_at, published)                 # ⭐ 这两列不清
        self.assertEqual(row.submitted_at, submitted)

    def test_claim_published_at_is_not_erased_by_the_later_transition(self):
        """第 4 条口径的**独立**判据：`apply_transition` 跑完之后，库里那一列还是抢单写的值。

        （与 `test_claim_writes_published_at_equal_to_claimed_at` 同源，但这里**只**看
        「有没有被擦成 NULL」——那条 500 行注释里点名的失败形态。）
        """
        cs, _cs_user = self._cs("cs-pub-erase")
        order = self._make_order(66)
        self.assertEqual(self._throw(cs, order)["code"], 0)
        Order.objects.filter(pk=order.pk).update(
            status=OrderStatus.WAITING_GRAB, published_at=None
        )
        taker = self._taker("taker-pub-erase")
        self.assertEqual(
            taker.post(
                f"/api/business/hall/{order.order_no}/claim/",
                {}, content_type="application/json",
            ).json()["code"],
            0,
        )
        self.assertIsNotNone(self._row(order).published_at)


class SubmittedAtWritePathTests(_ColumnFieldTestCase):
    """`Order.submitted_at`（报酬页「交单时间」那一档）= **第一次**进「待验收」那一刻。

    写入点**一处**：`order_status.apply_transition` 里 `target == PENDING_REVIEW` 那一支
    ——它是「进状态」的唯一收口（`/submit/`、通用 `/transition/`、批量迁移都经它）。
    ⛔ 三条视图里各写一遍是**不要**的形状（E-0021）。

    ⚠️ 断言一律打在**数据库行**上。
    """

    def _in_progress_with_taker(self, n, username):
        """建一张「代练中 + 有接单人」的单（`/submit/` 要求来源态与接单人身份两条都满足）。"""
        order = self._make_order(n, status=OrderStatus.IN_PROGRESS)
        taker = make_logged_in_client(
            username=username,
            groups=("代练员",),
            nodes=("order.submit_work",),
        )
        user = User.objects.get(username=username)
        grant_games(user, self.game)
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN).update(user=user)
        return order, taker

    def test_a_fresh_order_has_no_submitted_at(self):
        row = self._row(self._make_order(80, status=OrderStatus.IN_PROGRESS))
        self.assertIsNone(row.submitted_at)

    def test_submit_writes_submitted_at_to_the_database_row(self):
        """`/submit/` ⇒ 库里有值，且就是那一刻（区间断言，⛔ 不是只断非空）。"""
        order, taker = self._in_progress_with_taker(81, "taker-submit")
        before = timezone.now()
        body = taker.post(
            f"/api/business/orders/{order.order_no}/submit/",
            {},
            content_type="application/json",
        ).json()
        after = timezone.now()
        self.assertEqual(body["code"], 0)
        row = self._row(order)
        self.assertEqual(row.status, OrderStatus.PENDING_REVIEW)      # 哨兵
        self.assertIsNotNone(row.submitted_at)
        self.assertGreaterEqual(row.submitted_at, before)
        self.assertLessEqual(row.submitted_at, after)

    def test_generic_transition_into_pending_review_also_writes_it(self):
        """**唯一收口**的判据：通用 `/transition/` 进「待验收」同样写（不是只有 `/submit/` 写）。

        判别力：若有人把这一列写在 `/submit/` 视图里（而不是 `apply_transition` 里），
        本用例会红 —— 那正是设计 §2.2 点名不要的形状。
        """
        cs, _cs_user = self._cs("cs-submit-transition")
        order = self._make_order(82, status=OrderStatus.IN_PROGRESS)
        self.assertIsNone(self._row(order).submitted_at)              # 阴性对照
        self.assertEqual(
            self._transition(cs, order, OrderStatus.PENDING_REVIEW)["code"], 0
        )
        row = self._row(order)
        self.assertEqual(row.status, OrderStatus.PENDING_REVIEW)      # 哨兵
        self.assertIsNotNone(row.submitted_at)

    def test_a_second_submission_does_not_overwrite(self):
        """**只写第一次**：改回代练中再交一次 ⇒ 值**不变**（钉值，⛔ 不是「非空」）。"""
        cs, _cs_user = self._cs("cs-submit-twice")
        order = self._make_order(83, status=OrderStatus.IN_PROGRESS)
        self.assertEqual(
            self._transition(cs, order, OrderStatus.PENDING_REVIEW)["code"], 0
        )
        first = self._row(order).submitted_at
        self.assertIsNotNone(first)                                   # 哨兵
        self.assertEqual(
            self._transition(cs, order, OrderStatus.IN_PROGRESS)["code"], 0
        )
        self.assertEqual(self._row(order).submitted_at, first)        # 离开待验收不清
        self.assertEqual(
            self._transition(cs, order, OrderStatus.PENDING_REVIEW)["code"], 0
        )
        self.assertEqual(self._row(order).submitted_at, first)        # 二次交单不覆盖


class ServerWrittenColumnsAreClosedTests(_ColumnFieldTestCase):
    """两列都**不收**通用写入：spec §6 的写入点表只有 `apply_transition` 与 `throw_to_hall`。

    ⚠️ 这一组**不在**任务 brief 的 Step 1 清单里，是自检时**实测**发现的洞：通用 PATCH
    （只持 `order.add_edit` 的客服）送 `completed_at` / `hall_publisher` ⇒ 业务码 0、
    库里的行真的被改写 ⇒ 通用写入成了 spec §6 之外的**第二个写入点**。按既有先例
    （`tests/test_order_api.py::test_patch_cannot_change_any_money_field`）收口并钉住。

    ⚠️ **两条用例都必须带「其它字段仍可写」的阳性对照**（不是「录单仍能建单」那种更弱的对照）：
    防的是「顺手用 read_only 把整条写路径堵死」——那会让 PATCH / 录单整体失效而断言照样绿。
    PATCH 那条的对照是同一个请求里的 `remark`（证明**这一条 PATCH 真的执行了、且非目标字段
    照常落库**）；录单那条的对照是 `project_text`（证明单真的建成了）。
    """

    def test_patch_cannot_forge_the_completion_time_or_the_publisher(self):
        """PATCH 送这两列 ⇒ 被忽略；同一次 PATCH 里的 `remark` 照常写进去。

        判据打在**数据库行**上：`completed_at` / `hall_publisher_id` 仍是空。
        """
        cs, _cs_user = self._cs("cs-forge")
        other = User.objects.create_user(username="patch-forge-target")
        order = self._make_order(60)
        response = cs.patch(
            f"/api/business/orders/{order.order_no}/",
            {
                "completed_at": "2020-01-01T00:00:00Z",
                "hall_publisher": other.id,
                "split_kind": "PEER",
                "split_id": "forged-group",
                "remark": "阳性对照",
            },
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0)
        row = self._row(order)
        self.assertIsNone(row.completed_at)
        self.assertIsNone(row.hall_publisher_id)
        self.assertEqual(row.split_kind, "")
        self.assertEqual(row.split_id, "")
        # 阳性对照：`remark` 是**同一次 PATCH** 里的普通可写字段 ⇒ 它落库了才说明这次 PATCH
        # 真的执行过（而不是整包被拒 / PATCH 被整体堵死）
        self.assertEqual(row.remark, "阳性对照")

    def test_intake_cannot_set_the_completion_time_or_the_publisher(self):
        """录单（`POST /orders/`）同样不收这两列——否则建单那一步就能伪造发单人。"""
        cs, _cs_user = self._cs("cs-intake")
        other = User.objects.create_user(username="intake-forge-target")
        body = cs.post(
            "/api/business/orders/",
            {
                "game": self.game.id,
                "project_text": "满星",
                "completed_at": "2020-01-01T00:00:00Z",
                "hall_publisher": other.id,
                "split_kind": "PEER",
                "split_id": "forged-group",
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        row = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertIsNone(row.completed_at)
        self.assertIsNone(row.hall_publisher_id)
        self.assertEqual(row.split_kind, "")
        self.assertEqual(row.split_id, "")
        self.assertEqual(row.project_text, "满星")   # 阳性对照：这单真的建成了


class ServerWrittenTimeColumnsAreClosedTests(_ColumnFieldTestCase):
    """⭐ 批次 5 · 线 E 修复轮 3（2026-10-15）：**发单 / 交单两列也不收通用写入**。

    ## 为什么补这一组（全量门禁抓到的真缺陷，⛔ 不是网该更新）

    两列先落地时**没有**进 `SERVER_WRITTEN_ORDER_FIELDS` ⇒ 两个写序列化器都是 `exclude = []`
    ⇒ 这两列成了**可写字段**：
    ① **内网**：只持 `order.add_edit` 的客服一个 PATCH 就能把它们改成任意值——而
       `order_time_fields._mark` 的「只写第一次」（`WHERE <列> IS NULL`）**管不到 PATCH**
       （那条 `WHERE` 是那条路自己加的），于是不变式被绕过；
    ② **对外**：`apps/openapi/api/order_write_views.py` 的改单端点**共用**
       `OrderUpdateSerializer` ⇒ **外部系统可以伪造我方订单的这两个事件时刻**。
       这一面由 `tests/test_openapi_order_update.py` 那张「最终对外字段表」当场报出来
       （`AssertionError: Items in the first set but not the second: 'published_at' 'submitted_at'`）。

    ## 与既有 `ServerWrittenColumnsAreClosedTests` 的关系

    同一族（「值由服务端动作推出」⇒ 请求体不是权威来源），判据形态照抄它那两条：
    **打在数据库行上** + 每条都带**阳性对照**证明「请求真的被处理了 / 不是谁都不写」。

    ⚠️ 第三面（对外改单端点）在本文件里⛔ 不重复造夹具（那要 provision 第二个租户），
    它落在 `tests/test_openapi_order_update.py::ReadOnlyFieldTests`——
    `_patch` 那一族夹具本来就在那里。
    """

    def test_intake_cannot_set_the_two_time_columns(self):
        """① 录单（`POST /orders/`）带这两列 ⇒ 落库的是**事件值（这里还没发生 ⇒ NULL）**。

        ⚠️ 阳性对照 = `project_text`（证明这单真的建成了，⛔ 不是整包被拒）。
        ⚠️ 判别力：把这两列从 `SERVER_WRITTEN_ORDER_FIELDS` 拿掉 ⇒ 库里会是请求体给的那个值 ⇒ 红。
        """
        cs, _cs_user = self._cs("cs-time-intake")
        body = cs.post(
            "/api/business/orders/",
            {
                "game": self.game.id,
                "project_text": "满星",
                "published_at": "2020-01-01T00:00:00Z",
                "submitted_at": "2020-01-02T00:00:00Z",
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        row = Order.objects.get(order_no=body["data"]["order_no"])
        # 这张单**还没**被发出去、也**还没**交单 ⇒ 事件值就是 NULL（⛔ 不是请求体那两个时刻）
        self.assertIsNone(row.published_at)
        self.assertIsNone(row.submitted_at)
        self.assertEqual(row.project_text, "满星")     # 阳性对照：单真的建成了

    def test_patch_cannot_forge_the_two_time_columns(self):
        """② 通用 PATCH（`order.add_edit`）送这两列 ⇒ 被忽略；同一次 PATCH 的 `remark` 照常落库。

        判据打在**数据库行**上。
        ⚠️ 阳性对照是**同一个请求**里的 `remark`（证明这一条 PATCH 真的执行了、且非目标字段照常写），
        与既有那两条同款。
        """
        cs, _cs_user = self._cs("cs-time-patch")
        order = self._make_order(67)
        response = cs.patch(
            f"/api/business/orders/{order.order_no}/",
            {
                "published_at": "2020-01-01T00:00:00Z",
                "submitted_at": "2020-01-02T00:00:00Z",
                "remark": "阳性对照",
            },
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0)
        row = self._row(order)
        self.assertIsNone(row.published_at)
        self.assertIsNone(row.submitted_at)
        self.assertEqual(row.remark, "阳性对照")       # 阳性对照

    def test_the_server_actions_still_write_them(self):
        """④ **阳性对照**：走 `/claim` 与 `/submit/` ⇒ 两列**正常落库**（⛔ 不是「谁都不写」）。

        ⚠️ 这一条是前三条的**反面**：没有它，「把两列做成永远只读的空列」也能让那三条绿
        ——而那正是批次 4 那个缺陷的形状（列在、没人写）。
        """
        cs, _cs_user = self._cs("cs-time-positive")
        order = self._make_order(68)
        self.assertEqual(self._throw(cs, order)["code"], 0)
        taker = make_logged_in_client(
            username="taker-time-positive",
            groups=("代练员",),
            nodes=("grab_hall.take_order", "order.submit_work"),
        )
        grant_games(User.objects.get(username="taker-time-positive"), self.game)
        self.assertEqual(
            taker.post(
                f"/api/business/hall/{order.order_no}/claim/",
                {}, content_type="application/json",
            ).json()["code"],
            0,
        )
        self.assertIsNotNone(self._row(order).published_at)          # 接手 ⇒ 发单时间
        self.assertEqual(
            taker.post(
                f"/api/business/orders/{order.order_no}/submit/",
                {}, content_type="application/json",
            ).json()["code"],
            0,
        )
        row = self._row(order)
        self.assertEqual(row.status, OrderStatus.PENDING_REVIEW)     # 哨兵
        self.assertIsNotNone(row.submitted_at)                       # 交单 ⇒ 交单时间


class HallCardColumnIsServerWrittenTests(_ColumnFieldTestCase):
    """⭐ 2026-10-26（业务方拍板）：`Order.hall_card`（镜像卡派生的隐藏单）**不收通用写入**。

    两个写序列化器都是 `exclude = []` ⇒ 不把这一列列进 `SERVER_WRITTEN_ORDER_FIELDS` 的话：
    ① 录单（`POST /orders/`）带 `hall_card` ⇒ 新单出生就是一张隐藏单（在列表 / 统计里消失）；
    ② 通用 `PATCH` 带 `hall_card` ⇒ 一张**正常单**可以挂到某张镜卡上、从列表里消失。

    判据打在**数据库行**上，每条都带阳性对照（那一次请求真的被处理了）。
    """

    def setUp(self):
        super().setUp()
        from apps.business.models import PeerHallMirror, PeerUpstreamBinding
        from apps.business.peer_sync import hash_token, new_token
        from django.contrib.auth.models import User as _User

        operator = _User.objects.create_user(username="cs-hallcard-op", password="pw-syn")
        binding = PeerUpstreamBinding.objects.create(
            peer_name="隐藏单夹具上家",
            peer_base_url="https://syn-hallcard-up.test.invalid",
            peer_account="syn-hallcard-up",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://syn-hallcard-local.test.invalid",
            bound_by=operator,
            hall_sync_enabled=True,
        )
        self.mirror = PeerHallMirror.objects.create(
            peer_upstream_binding=binding,
            peer_base_url=binding.peer_base_url,
            peer_order_no="SYN-HALL-CARD-1",
            status=PeerHallMirror.Status.LISTED,
            game=self.game,
            remote_hall_price=Decimal("50.00"),
            first_seen_at=timezone.now(),
        )

    def test_intake_ignores_a_forged_hall_card(self):
        cs, _user = self._cs("cs-hallcard-intake")
        body = cs.post(
            "/api/business/orders/",
            {
                "game": self.game.id,
                "project_text": "满星",
                "hall_card": self.mirror.pk,
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        row = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertIsNone(row.hall_card_id)
        self.assertEqual(row.project_text, "满星")     # 阳性对照：单真的建成了

    def test_patch_ignores_a_forged_hall_card(self):
        cs, _user = self._cs("cs-hallcard-patch")
        order = self._make_order(910)
        body = cs.patch(
            f"/api/business/orders/{order.order_no}/",
            {"hall_card": self.mirror.pk, "remark": "照常改"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        row = self._row(order)
        self.assertIsNone(row.hall_card_id)
        self.assertEqual(row.remark, "照常改")         # 阳性对照：这次 PATCH 真的落库了
