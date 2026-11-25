"""批量端点的隔离网（spec §7-T2）：权限门 403 + 跨租户一行不写 + 游戏可见范围。

⚠️ 跨租户的否定断言**不许用主键指认另一租户的行**（两个 schema 的主键序列各自从 1 开始
——`AGENTS.md` §5-G）⇒ 判据一律用**订单号**。
⚠️ 夹具一律合成值；断言一律打在**数据库行**上（E-0009：不打 logger、不打内存对象）。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.models import Game, Order
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, make_logged_in_client
from tests.test_order_batch_base import BATCH, OrderBatchTestBase

#: 跨租户隔离用租户 B 的域名 / schema（`.test.local` 在 `DJANGO_ALLOWED_HOSTS` 白名单里）。
DOMAIN_B = "obatch-b.test.local"
SCHEMA_B = "obatch_b"

#: 判据用的订单号：**租户内唯一、且两租户取值不同**（⛔ 不用主键）。
NO_A = "20260926000000009001"
NO_B = "20260926000000009002"
#: 一个 20 位但谁都没有的单号（形状合法、内容不存在）。
NO_MISSING = "1" * 20


class NoNodeTests(OrderBatchTestBase):
    """批量端点缺权限时返回 403，且不改订单或日志。

    ⚠️ 无节点账号**必须显式 opt-out**（`nodes=()`）：`make_logged_in_client` 的
    `nodes` 默认就是空集（fail-closed），而本基线那个账号一次挂齐了 9 个端点用到的节点
    ⇒ ⛔ 不许在本基线上放宽，另建一个账号。
    ⚠️ 无节点账号**照样授游戏**（下面那行 `grant_games`）：这样若真出现 403，它只可能来自
    **节点门**；否则这条用例可能在测「游戏范围」而不是「权限」（那是两回事）。
    """

    def _client_without_nodes(self):
        # ⚠️ 组名 ≤5 字：`Role.name` 的 `max_length=5`（见 `p5_auth_helpers._grant_nodes`）。
        #    本用例 `nodes=()` ⇒ 不建 `Role`，所以 6 字今天不会炸——但那是一个**潜伏**的
        #    `DataError`（将来谁给这个账号挂一个节点就炸），不值得为字面好看留着。
        return make_logged_in_client(
            username="obatch-nobody", groups=("无权限组",), nodes=()
        )

    def test_every_batch_endpoint_is_closed_without_its_node(self):
        """逐条检查端点返回 403，且订单列和操作日志不变。

        ⚠️ 403 是**权限层**的出口（`ORDER_PERMISSIONS` + `has_node`）：若某条端点的
        `ORDER_PERMISSIONS` 漏了，这里会以 200 出现——那正是本条要抓的。
        """
        cases = [
            (f"{BATCH}/price/", {"total_price": "1.00"}),
            (f"{BATCH}/hall-price/", {"price": "1.00"}),
            (f"{BATCH}/payment-status/", {"payment_status": "SETTLED"}),
            (f"{BATCH}/types/", {"order_type": "BOOST"}),
            (f"{BATCH}/assign/", {"user_id": 1}),
            (f"{BATCH}/transition/", {"target": "IN_PROGRESS"}),
            (f"{BATCH}/refunds/", {"refund_total": "1.00", "reason": "测试原因",
                                   "request_ids": ["测试键-1"]}),
            (f"{BATCH}/delete/", {"isDelEmp": "N"}),
            (f"{BATCH}/delete-v2/", {"isDelEmp": "N"}),
            (f"{BATCH}/recompute-commission/", {}),
        ]
        self.assertEqual(len(cases), 10, "批量端点数量变了，请核对 ORDER_PERMISSIONS")
        order = self._order(total_price=Decimal("100.00"))
        client = self._client_without_nodes()
        grant_games(User.objects.get(username="obatch-nobody"), order.game)
        self.assertEqual(OperationLog.objects.count(), 0)     # 夹具基线：登录不留痕
        for path, extra in cases:
            payload = {"order_nos": [order.order_no], "dry_run": False, **extra}
            resp = client.post(path, payload, content_type="application/json")
            with self.subTest(path=path):
                self.assertEqual(resp.status_code, 403, (path, resp.status_code, resp.content[:200]))
        # ── 零写入：断言打在**数据库行**上（E-0009）──
        order.refresh_from_db()
        self.assertEqual(order.total_price, Decimal("100.00"))
        self.assertEqual(order.updated_by, "")
        self.assertIsNone(order.deleted_at)
        self.assertEqual(OperationLog.objects.count(), 0)


class CrossTenantTests(OrderBatchTestBase):
    """B 租户的凭据拿 A 租户的单号 ⇒ 那些单号全在 `ineligible`、**一行不写**。

    ## 夹具出处（既有写法，不是本文件发明的）

    照 `tests/test_order_update_api.py::OrderUpdateIsolationTests` 与
    `tests/test_rbac_endpoint_enforcement.py::CrossTenantOrderEndpointIsolationTests`：
    `setUpClass` 用 `provision_tenant` 建第二个租户；两侧各造一笔**单号不同**的单；
    B 侧的请求与 ORM 读一律走 `_b()`（`tenant_context` + `try/finally` 把连接钉回 A）。

    ⚠️ **为什么必须 `try/finally` 钉回 A**：`django_tenants` 的 `TenantMainMiddleware`
    只在请求**进来时** `connection.set_tenant(...)`，**不会**在响应结束时还原
    （`tests/test_account_isolation.py` 的模块说明有完整实测记录）⇒ 不还原的话，
    后面每一句 ORM 都会跑在 B 的 schema 上，断言看起来"更严"其实测错了对象。

    ## ⭐ 本条判据的**证明力边界**（如实写，⛔ 别读成「跨租户已被完全证明」）

    租户之间的不可见性由 **schema** 保证（B 的 `search_path` 里根本没有 A 的
    `business_order` 行），**不是**由 `order_batch.resolve_orders` 的
    `scope_queryset(...)` 保证 ⇒ 把那一句注掉，**本条仍然绿**（Task 11 变异实测的结论，**内联在此**；
    原始输出当时只落在**不进 git** 的 `.superpowers/**` 里、后来者读不到 ⇒ ⛔ 不再引用那个路径）。
    对那句 `scope_queryset` 有判别力的是同文件的 `GameScopeTests`（**同一个变异下它红**）。

    ⇒ 本条负责的是另外两件事，两件都不是那句 `scope_queryset` 能提供的：
    ① **跨租户的请求形状一行都写不进去**（A 的行、B 的行，两侧都在自己的 schema 里读）；
    ② 否定断言的**前提可指认**——两侧夹具自证「两笔单互不可见」，并配**正向对照**
    （同一个 B 的 client 拿 B 自己的单号做同一个动作**必须生效**），
    否则「一行不写」可能由「B 根本用不了这个端点」凑出来。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="批量端点跨租户隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        super().setUp()
        # ⚠️ 第一行（在 super 之后）：把连接显式钉回租户 A（挡住上一条用例的残留，
        #    保证用例可单独运行）——`TenantTestCase.setUpClass` 已 `set_tenant`，
        #    但跨域名请求会把它改掉。
        connection.set_tenant(self.tenant)
        order_a = Order.objects.create(
            game=self.game, order_no=NO_A, project_text="仅 A 可见",
            total_price=Decimal("100.00"),
        )
        self.order_a = order_a
        try:
            with tenant_context(self.tenant_b):
                # B 侧：同样形状，但**单号不同**（判据靠单号指认，不靠主键）。
                self.client_b = make_logged_in_client(
                    domain=DOMAIN_B, username="obatch-b", groups=("批量测试组",),
                    nodes=(
                        "order.list", "order.submit_payout_price", "order.edit_payout_price",
                        "order.change_payment_status", "order.add_edit", "order.submit_edit",
                        "order.set_offer_price", "order.change_status",
                        # ⭐ 计划 ④：与 A 侧同一份批量节点集（待抢单目标要 36）。
                        "order.send_to_grab_hall",
                        "order.change_status_completed", "order.assign_taker",
                        "order.clear_taker", "order.delete", "order.recycle",
                        "order.online_refund", "finance.change_settlement_status",
                    ),
                )
                game_b = Game.objects.create(name="测试游戏乙（B 租户）")
                grant_games(User.objects.get(username="obatch-b"), game_b)
                self.order_b = Order.objects.create(
                    game=game_b, order_no=NO_B, project_text="仅 B 可见",
                    total_price=Decimal("200.00"),
                )
        finally:
            connection.set_tenant(self.tenant)

        # ── 夹具自证（**不是**用例，是承重点）：两侧各有一笔**不同号**的单，且互不可见 ──
        self.assertTrue(Order.objects.filter(order_no=NO_A).exists())
        assert self._b(lambda: Order.objects.filter(order_no=NO_B).exists()), \
            "B 的标记单没落到 B 的 schema 上"
        assert not self._b(lambda: Order.objects.filter(order_no=NO_A).exists()), \
            "A 的单在 B 的 schema 里也存在——隔离前提坏了"
        assert not Order.objects.filter(order_no=NO_B).exists(), \
            "B 的单在 A 的 schema 里也存在——隔离前提坏了"

    def _b(self, function):
        """在 B 的 schema 上下文里执行，结束后把连接钉回租户 A。"""
        try:
            with tenant_context(self.tenant_b):
                return function()
        finally:
            connection.set_tenant(self.tenant)

    def _post_b(self, path, payload):
        return self._b(
            lambda: self.client_b.post(
                path, payload, content_type="application/json"
            ).json()
        )

    def test_v2_delete_refuses_foreign_order_and_leaves_both_orders_intact(self):
        payload = {"order_nos": [NO_B, NO_A], "dry_run": False, "reason": "清理测试订单"}
        applied = self._post_b(f"{BATCH}/delete-v2/", payload)
        self.assertEqual(applied["code"], 1002)
        self.assertEqual(applied["data"]["conflict"]["order_no"], NO_A)
        self.assertTrue(Order.objects.filter(order_no=NO_A).exists())
        self.assertTrue(self._b(lambda: Order.objects.filter(order_no=NO_B).exists()))
        self.assertEqual(self._b(lambda: OperationLog.objects.count()), 0)

        control = self._post_b(f"{BATCH}/delete-v2/", {**payload, "order_nos": [NO_B]})
        self.assertEqual(control["data"]["applied"], [NO_B])
        self.assertFalse(self._b(lambda: Order.objects.filter(order_no=NO_B).exists()))
        self.assertTrue(Order.objects.filter(order_no=NO_A).exists())

    def test_other_tenant_order_numbers_are_ineligible_and_write_nothing(self):
        # ── ① 校验态（`dry_run=True`）：A 的单号（B 的 schema 里没有它）
        #    与一个纯不存在的号 ⇒ 两笔都进 `ineligible`，**一笔都不合格**。
        body = self._post_b(f"{BATCH}/price/", {
            "order_nos": [NO_A, NO_MISSING], "dry_run": True, "total_price": "1.00",
        })
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            [row["order_no"] for row in body["data"]["ineligible"]],
            [NO_A, NO_MISSING],
            "跨租户的单号必须逐笔进 ineligible（顺序 = 请求顺序）",
        )
        self.assertEqual(body["data"]["eligible"], [])
        self.assertEqual(body["data"]["applied"], [])

        # ── ② 提交态（`dry_run=False`）：§D4 的整批回滚 + 1002（⛔ 不是静默跳过那一笔）。
        #    码写**字面值**（E-0024：断言钉值，不钉常量）。
        applied = self._post_b(f"{BATCH}/price/", {
            "order_nos": [NO_A], "dry_run": False, "total_price": "1.00",
        })
        self.assertEqual(applied["code"], 1002, applied)
        self.assertEqual(applied["data"]["applied"], [])
        self.assertEqual(applied["data"]["conflict"]["order_no"], NO_A)

        # ── ③ 一行不写：两侧的行都在**自己的 schema** 里读 ──
        self.order_a.refresh_from_db()
        self.assertEqual(self.order_a.total_price, Decimal("100.00"))
        self.assertEqual(self.order_a.updated_by, "")
        self.assertIsNone(self.order_a.deleted_at)
        b_row = self._b(lambda: Order.objects.get(order_no=NO_B))
        self.assertEqual(b_row.total_price, Decimal("200.00"))
        self.assertEqual(b_row.updated_by, "")
        self.assertIsNone(b_row.deleted_at)
        # 两侧都零留痕（逐单一行是 spec §D7）。
        self.assertEqual(OperationLog.objects.count(), 0)
        self.assertEqual(self._b(lambda: OperationLog.objects.count()), 0)

        # ── ④ 正向对照：**同一个** B 的 client 拿 B 自己的单号做**同一个**动作必须生效 ──
        control = self._post_b(f"{BATCH}/price/", {
            "order_nos": [NO_B], "dry_run": False, "total_price": "123.00",
        })
        self.assertEqual(control["code"], 0, control)
        self.assertEqual(control["data"]["applied"], [NO_B])
        b_after = self._b(lambda: Order.objects.get(order_no=NO_B))
        self.assertEqual(b_after.total_price, Decimal("123.00"))
        # 而且 A 的那一行**仍然**没动（对照不得顺手证明「谁都能改」）。
        self.order_a.refresh_from_db()
        self.assertEqual(self.order_a.total_price, Decimal("100.00"))


class GameScopeTests(OrderBatchTestBase):
    """不在自己游戏权限内的单 ⇒ 进 `ineligible`（与 `get_object()` 的既有语义一致）。

    ⚠️ **判据必须分两个形态**（与 spec §7-T1 的执行期更正同形）：
    `dry_run=True` 有**逐笔**判定 ⇒ 合格的合格、不合格的进 `ineligible`；
    `dry_run=False` 时「取不到行」按 §D4 是**整批拒**（1002 + `applied` 为空），
    ⛔ 响应里**没有** `ineligible` 键——照 `dry_run` 那半的形状读 `data.ineligible` 会 KeyError。

    ⚠️ 两个形态各配**正反对照**（同一请求里既有在范围、又有不在范围的单）：
    只有「不在范围 ⇒ 不合格」而没有「在范围 ⇒ 合格」的话，
    「这个动作对谁都不生效」也能让本条绿。

    ⭐ **本条是同文件里唯一对 `resolve_orders` 的 `scope_queryset(...)` 有判别力的用例**：
    把那一句注掉，「不在范围」那一笔在本 schema 里**查得到**（游戏范围是本条唯一的闸）
    ⇒ 它会变成 `eligible` ⇒ 本条红（变异原始输出见 Task 11 报告）。
    """

    def test_out_of_scope_game_order_is_ineligible(self):
        other_game = Game.objects.create(name="测试游戏乙")
        theirs = self._order(game=other_game, total_price=Decimal("100.00"))
        mine = self._order(total_price=Decimal("300.00"))

        # ① 校验态：逐笔判定 ⇒ 在范围的那笔合格、不在范围的进 ineligible。
        body = self._post(f"{BATCH}/price/", {
            "order_nos": [theirs.order_no, mine.order_no], "dry_run": True,
            "total_price": "1.00",
        })
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["eligible"], [mine.order_no])
        self.assertEqual(
            [row["order_no"] for row in body["data"]["ineligible"]], [theirs.order_no]
        )
        self.assertEqual(body["data"]["applied"], [])

        # ② 提交态：只送不在范围的那一笔 ⇒ §D4 整批拒（1002），**一行不写**。
        applied = self._post(f"{BATCH}/price/", {
            "order_nos": [theirs.order_no], "dry_run": False, "total_price": "1.00",
        })
        self.assertEqual(applied["code"], 1002, applied)
        self.assertEqual(applied["data"]["applied"], [])
        theirs.refresh_from_db()
        self.assertEqual(theirs.total_price, Decimal("100.00"))
        self.assertEqual(theirs.updated_by, "")
        mine.refresh_from_db()
        self.assertEqual(mine.total_price, Decimal("300.00"))
        self.assertEqual(OperationLog.objects.count(), 0)

        # ③ 正向对照：在范围的那一笔（同一个端点、同一份 payload 形状）必须真的改得动。
        control = self._post(f"{BATCH}/price/", {
            "order_nos": [mine.order_no], "dry_run": False, "total_price": "1.00",
        })
        self.assertEqual(control["code"], 0, control)
        self.assertEqual(control["data"]["applied"], [mine.order_no])
        mine.refresh_from_db()
        self.assertEqual(mine.total_price, Decimal("1.00"))
