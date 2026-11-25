"""Task 8：P3a 的跨租户隔离回归（spec §4.4 / `AGENTS.md` §4）。

`AGENTS.md` §4：「任何新增查询都必须携带租户上下文，并补一条『以租户 A 的凭据
访问 B 的数据必须失败』的测试」。P3a 新增了 `Performance`、大厅查询与抢单的条件更新，
所以这条红线在**本文件**闭合（凭据层由 P5 收口，这里管的是数据层）。

⚠️ 本文件里的每条测试都必须能**单独运行**（P1 的教训：红线测试曾靠顺序假通过）。

⚠️ 断言纪律（照 `tests/test_tenant_isolation.py` 的既有形态）：**先在 B 里造一行并断言
它可见**，再断言 A 的行不可见——否则红线会依赖 `provision_tenant` 灌的基线数据来保证
B 非空，单独运行时可能假通过。**不要用 `count()`**：它的通过与否取决于两个租户各有
多少行，而不是「这一行可不可见」。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.claim import claim_order
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.core.scoping import scope_queryset
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 抢单那条边需要的节点。**授权不是本文件的主角**（隔离才是），所以这里给最小可用集合，
#: 而不是整份目录——后者会把一条与隔离无关的授权口径混进夹具。
TAKER_NODES = frozenset({"grab_hall.take_order"})


class PerformanceIsolationTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a, _ = provision_tenant(
            schema_name="p3a_a", name="P3A 租户 A", domain="p3a-a.test.local"
        )
        cls.tenant_b, _ = provision_tenant(
            schema_name="p3a_b", name="P3A 租户 B", domain="p3a-b.test.local"
        )

    def _make(self, no, price="88.00", game_name="原神"):
        """在当前 schema 里造一对「待抢单 + 打单工资绩效」。"""
        game = Game.objects.create(name=game_name)
        order = Order.objects.create(
            game=game, order_no=no, status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal(price),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal(price)
        )
        return order

    def test_performance_rows_do_not_cross_tenants(self):
        """⚠️ **判据不能用主键**（计划 Task 8 Step 1 的片段在这里是错的，实测红）。

        两个租户的 `id` 序列**各自从 1 开始**，所以 A 的 `order.id` 与 B 自己的
        `order.id` 常常是同一个数：`filter(order_id=a_order.id)` 在 B 里命中的是
        **B 自己的那一行**，于是「A 的行在 B 不可见」这条断言恒假（实测
        `AssertionError: True is not false`）。用**租户内唯一且两租户不同**的业务值
        （`order_no`）当判据才有判别力——这与 `test_tenant_isolation.py` 用
        `project_text` 做判据是同一个道理。
        """
        with tenant_context(self.tenant_a):
            self._make("20260920000000000001", game_name="仅 A 的游戏")

        with tenant_context(self.tenant_b):
            # 非空哨兵：先证明 B 里**能看到自己的行**，否定断言才有意义
            b_order = self._make("20260920000000000002", game_name="仅 B 的游戏")
            self.assertTrue(
                Performance.objects.filter(order=b_order).exists()
            )
            # 再断言 A 的行不可见——按 **A 的订单号**（两租户不同）判，不按主键
            self.assertFalse(
                Performance.objects.filter(
                    order__order_no="20260920000000000001"
                ).exists()
            )

        with tenant_context(self.tenant_a):
            self.assertTrue(
                Performance.objects.filter(
                    order__order_no="20260920000000000001"
                ).exists()
            )

    def test_same_order_no_can_live_in_two_tenants(self):
        """订单号跨租户只是概率唯一（P2 spec D10），租户内唯一由约束保证——
        所以两个租户各自用同一个订单号是**允许**的，且互不干扰。"""
        with tenant_context(self.tenant_a):
            self._make("20260920000000000009")
        with tenant_context(self.tenant_b):
            self._make("20260920000000000009")
            self.assertEqual(
                Performance.objects.filter(order__order_no="20260920000000000009").count(),
                1,
            )

    def test_claim_in_one_tenant_does_not_touch_the_other(self):
        """抢单也必须留在本租户 schema 内。

        ⚠️ 两个租户里的 id 很可能**相同**（每个 schema 各自从 1 开始），这恰好让本用例
        更有判别力：B 里抢 `id=1` 若漏了租户上下文，动到的就是 A 里那张 `id=1` 的单。

        ⚠️ `claim_order` 收的是 **ID**（不认身份、也不收 ORM 对象），它自己
        `Order.objects.get(pk=...)` 重新取行——取的是**当前连接的 schema**。
        本用例钉的就是这一点（决策记录：收 ID 是因为跨线程传对象不安全）。
        """
        with tenant_context(self.tenant_a):
            a_order = self._make("20260920000000000003")
            a_user = User.objects.create_user(username="a_taker")
            a_order_id, a_user_id = a_order.id, a_user.id

        with tenant_context(self.tenant_b):
            b_order = self._make("20260920000000000004")
            b_user = User.objects.create_user(username="b_taker")
            # 前置哨兵：B 里这张单真的在大厅（否则「A 没变」可能只是谁都没被抢）
            self.assertEqual(b_order.status, OrderStatus.WAITING_GRAB)
            claim_order(user_id=b_user.id, order_id=b_order.id, nodes=TAKER_NODES)
            b_order.refresh_from_db()
            self.assertEqual(b_order.status, OrderStatus.IN_PROGRESS)
            self.assertEqual(
                Performance.objects.get(order=b_order).user_id, b_user.id
            )

        with tenant_context(self.tenant_a):
            a_order.refresh_from_db()
            self.assertEqual(a_order.status, OrderStatus.WAITING_GRAB)   # 纹丝不动
            perf = Performance.objects.get(order_id=a_order_id)
            self.assertIsNone(perf.user_id)
            # `a_user_id` 只用来证明 A 里那个用户是真的存在（否则「没被写」可能只是
            # 因为 A 里根本没有这个用户——那是另一种失败，不该让本用例变绿）
            self.assertTrue(User.objects.filter(pk=a_user_id).exists())

    def test_scope_queryset_baseline_is_pass_through(self):
        """spec §4.4 的接线**不改变**任何行的可见性（P5 §7.2 的 pass-through 语义）。

        两个方向都断，缺一个就可能恒真：

        ① **不传** `scope_filter` ⇒ 行集合与「不接卡点」完全相同；
        ② **传**一个「排除全部」的过滤器 ⇒ 结果真的被清空——证明这个卡点确实会调用
           `scope_filter`（不是把参数吞掉、也不是永远返回原集）。

        📌 本条是为了防止有人在接线的同时**顺手发明**可见范围规则——那正是 E-0006 的坑
        （需求文档里"客服仅可见自己负责的订单"是被写反的那条）。
        """
        with tenant_context(self.tenant_a):
            self._make("20260920000000000006")
            user = User.objects.create_user(username="scope_probe")
            all_orders = Order.objects.all()
            self.assertTrue(all_orders.exists())        # 前置哨兵：空集会让 ① 恒真

            self.assertEqual(
                set(scope_queryset(user, all_orders).values_list("pk", flat=True)),
                set(all_orders.values_list("pk", flat=True)),
            )
            self.assertEqual(
                scope_queryset(
                    user, all_orders, scope_filter=lambda qs, u: qs.none()
                ).count(),
                0,
            )


class ScopingWiringTests(TenantTestCase):
    """spec §4.4 的接线必须**真的被用上**（Task 8 Step 0 的交付物）。

    ⚠️ **为什么需要这一条**（独立整体评审 I1）：上面那条测的是
    `scope_queryset()` **函数本身**，它不经过任何视图。实测把
    `order_views.py` 的 `get_queryset()` 与 `hall` 两处接线删掉后，
    `test_p3a_isolation + test_hall_api + test_order_api` **66 条全绿**——
    「接线」这一步当时**没有任何测试能变红**，spec §4.4 的红线只活在代码可见性里。

    ⚠️ **桩必须打在「返回值」上，不能只打在「被调用」上**：视图完全可以调用
    `scope_queryset(...)` 却把返回值丢掉（`scope_queryset(user, qs)` 单独一行是合法 Python），
    所以「断言它被调用」会漏掉那种写法。这里的桩把结果**清空**，再断言端点真的返回空集；
    同时用**不接桩的对照**证明「那张单本来是看得见的」——排除「空集来自别的条件」。
    """

    URL_LIST = "/api/business/orders/"
    URL_HALL = "/api/business/hall/"

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game, order_no="20260920000000000021",
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        # 双角色：`/orders/` 只要登录，大厅（Ruling 35）要代练员。
        self.client = make_logged_in_client(
            username="scoping-wired", groups=("客服", "代练员"),
            nodes=("order.list", "grab_hall.take_order"),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改——两条用例的「对照」正是「本来看得见」。
        grant_games(User.objects.get(username="scoping-wired"), self.game)

    def _nos(self, url):
        body = self.client.get(url).json()
        self.assertEqual(body["code"], 0)          # 前置哨兵：端点本身是成功的
        return [row["order_no"] for row in body["data"]["items"]]

    def test_order_list_uses_the_scoping_result(self):
        self.assertIn(self.order.order_no, self._nos(self.URL_LIST))    # 对照：本来看得见
        with mock.patch(
            "apps.business.api.order_views.scope_queryset",
            # （按游戏权限收窄）：桩的签名跟着**被桩的那条缝**更新——新的调用
            # 多传了 `scope_filter=`（`scope_queryset(user, qs, scope_filter=game_scope(...))`），
            # 旧的 `(user, qs)` 桩会 `TypeError`。**断言一字未改**：桩的返回值仍是
            # `qs.none()`，本用例要的仍是「端点返回空集」。
            side_effect=lambda user, qs, **kwargs: qs.none(),
        ) as seam:
            self.assertEqual(self._nos(self.URL_LIST), [])
        self.assertTrue(seam.called, "get_queryset() 没有经过 scope_queryset 卡点")

    def test_hall_uses_the_scoping_result(self):
        self.assertIn(self.order.order_no, self._nos(self.URL_HALL))    # 对照：本来看得见
        with mock.patch(
            "apps.business.api.order_views.scope_queryset",
            # （按游戏权限收窄）：桩的签名跟着**被桩的那条缝**更新——新的调用
            # 多传了 `scope_filter=`（`scope_queryset(user, qs, scope_filter=game_scope(...))`），
            # 旧的 `(user, qs)` 桩会 `TypeError`。**断言一字未改**：桩的返回值仍是
            # `qs.none()`，本用例要的仍是「端点返回空集」。
            side_effect=lambda user, qs, **kwargs: qs.none(),
        ) as seam:
            self.assertEqual(self._nos(self.URL_HALL), [])
        self.assertTrue(seam.called, "hall 没有经过 scope_queryset 卡点")
