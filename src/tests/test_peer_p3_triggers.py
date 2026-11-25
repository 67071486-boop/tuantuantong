"""上下游 P3 ：**触发挂钩点**的端点级判据（spec §7.1 / §16.2）。

    本文件回答的不是「入队内核写得对不对」（那在 `tests/test_peer_p3_enqueue.py` 里钉过），
    而是**每条产品写入路径到底有没有把那一行接上**——挂钩漏挂 / 挂错分支在这类改动里是
    **静默**的（内核单元测试全绿）。

    ## 覆盖的两类挂钩

    ① **状态漏斗**（唯一入口 `order_status.apply_transition`）：
    下行 `ISSUE` ⇒ 一行下行 `order.update`；`COMPLETED` 不在下行接受集 ⇒ 一行都不写；
    下家本地 `PENDING_REVIEW` / `COMPLETED` ⇒ 一行上行 `order.update`（映射成上家的待验收）；
    `REFUNDED` 不在上行触发集 ⇒ 一行都不写。
    ② **发单价助手**（`peer_dispatch.notify_order_price_changed`）的四条产品写入路径
    （`/hall-price/`、`/throw-to-hall/` 带价、`/assign/` 带价、`/adjust-amount/` 落在
    `Order.hall_price` 那一支）——全部打**真 HTTP**、断**数据库行**。

    ## 状态机的两处硬边（夹具必须自己处理）

    `apply_transition` 只认 `order_status.TRANSITIONS` 里存在的边，且入 `COMPLETED` **只有**
    `PENDING_REVIEW → COMPLETED` 一条（`_COMPLETED_SOURCE`）。所以本文件的 `_transition`
    在目标为 `COMPLETED` 且当前不是待验收时**先驱动一次到 `PENDING_REVIEW` 的中间迁移**，
    ⛔ 不改任何用例的目标态、也不放宽断言。

    ## ⚠️ 一条如实登记的落差：`/throw-to-hall/` 带价那一支

    回池钩子（`performance._on_pool_entry`，注册在 `WAITING_GRAB`）在 `apply_transition`
    **之内**就把 MAIN 绩效行的 `user` 清成 `None`（`reset_taker_performance`），而
    `/throw-to-hall/` 的价格写入排在它**之后** ⇒ 价格助手到这里**没有可投递的人**，
    一条 `order.update` 都写不出来（下游收到的是那条 `order.withdraw`）。
    这不是「挂钩没挂」：打桩用例证明该分支**确实调到了**助手；数据库行判据如实例证「无人可投」。
    ⛔ 两条判据**都在**，别只留一条。

    ⚠️ 夹具值**全部是合成值**（`syn-*` / `UP-*` / `*.test.invalid` / `测试*`），⛔ 不含真实租户数据。
"""

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_sync import hash_token, new_token
from tests.p5_auth_helpers import grant_games, make_logged_in_client

ORDERS = "/api/business/orders"
PERFORMANCES = "/api/business/performances"

#: 本文件走到的**端点门**（`OrderViewSet.ORDER_PERMISSIONS` +
#: `PerformanceViewSet.PERFORMANCE_PERMISSIONS`）。`groups` 只建 Group、**不放行**，
#: 所以夹具要显式声明它要过的门：
#: - `order.change_status` / `order.change_status_completed` ⇒ `/transition/`（按目标态分）
#: - `order.set_offer_price` ⇒ `/hall-price/`
#: - `order.submit_payout_price` ⇒ 三条带价路径的价格闸 + `/adjust-amount/`
#: - `order.assign_taker` / `order.send_to_grab_hall` ⇒ `/assign/` / `/throw-to-hall/` 的类级门
#: - `order.list` ⇒ `get_object()` 的取数层
NODES = (
    "order.change_status",
    "order.change_status_completed",
    "order.set_offer_price",
    "order.submit_payout_price",
    "order.assign_taker",
    "order.send_to_grab_hall",
    "order.list",
)


def _no(n):
    """20 位订单号（`OrderViewSet.lookup_value_regex = r"\\d{20}"`：URL 里的单号必须 20 位）。"""
    return f"2026092700000000{n:04d}"


class TestPeerBase(TenantTestCase):
    """共用夹具：一款游戏、两套绑定、三张单。

    - `self.bound_taker` + `self.downstream_binding`：一个挂着**生效下游绑定**的账号；
    - `self.order`：MAIN 绩效行归 `bound_taker`（⇒ 状态 / 发单价变化写得出一行**下行**事件），
    初始状态 `UNASSIGNED`；
    - `self.plain_order`：MAIN 行有人、但那个人**没有**绑定（⇒ 一行都不写）；
    - `self.local_order` + `self.local_link`：挂着一条**启用且未软删**的上游绑定
    （⇒ 状态变化写得出一行**上行**事件），MAIN 行没有归属人。

    ⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里，普通 `TestCase`
    的 search_path 里没有 `business_*` 表。
    ⚠️ `PeerDownstreamBinding` 的 `account` / `peer_base_url` / `token_in_hash` / `token_out`
    与 `PeerUpstreamBinding` 的 `peer_name` / `peer_base_url` / `peer_account` /
    `token_in_hash` / `token_out` / `local_base_url` / `bound_by` **全部非空**
    ⇒ 少给一个就 `IntegrityError`，夹具按字段形状给全。
    ⚠️ 登录客户端**每个测试只建一次**：`make_logged_in_client` 每次都 `create_user`，
    同一测试里建第二次会撞用户名唯一约束。挂在 `self.api` 上（⛔ 不覆盖框架的 `self.client`）。
"""

    def setUp(self):
        self.game = Game.objects.create(name="上下游P3挂钩夹具游戏")

        self.bound_taker = self._taker("syn-p3-trigger-bound")
        self.downstream_binding = self._bind(self.bound_taker)

        self.upstream_operator = get_user_model().objects.create_user(
            username="syn-p3-trigger-operator", password="pw-syn-p3"
        )
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家甲",
            peer_base_url="https://peer-up.test.invalid",
            peer_account="syn-up-account",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-local.test.invalid",
            bound_by=self.upstream_operator,
        )

        self.order = self._order(_no(1), taker=self.bound_taker)
        self.plain_order = self._order(_no(2), taker=self._taker("syn-p3-trigger-plain"))
        self.local_order = self._order(_no(3))
        self.local_link = PeerOrderLink.objects.create(
            order=self.local_order,
            upstream_binding=self.upstream_binding,
            peer_order_no="UP-SYN-P3-1",
        )

        self.api = make_logged_in_client(
            username="syn-p3-trigger-cs", groups=("客服",), nodes=NODES
        )

    # ── 账号与绑定 ────────────────────────────────────────────────
    def _taker(self, username):
        """一个能被指派的账号（带提成档案，缺档案会回落全额）。"""
        user = get_user_model().objects.create_user(
            username=username, password="pw-syn-p3"
        )
        StaffProfile.objects.create(
            user=user,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=无游戏=拒）
        # ⇒ 能被 /assign/ 指派的夹具账号要持本类的游戏（专钉：tests/test_order_assign_game_gate.py）。
        grant_games(user, self.game)
        return user

    def _bind(self, account, *, base_url="https://peer-down.test.invalid"):
        """给账号建一条**我方为上家**的生效绑定。"""
        return PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url=base_url,
            # 对手方给我的令牌只存哈希；我发给下家的是明文——按字段口径给。
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )

    # ── 订单 ─────────────────────────────────────────────────────
    def _order(self, no, *, status=OrderStatus.UNASSIGNED, taker=None):
        """订单 + 它的 MAIN 绩效行（价格给**非默认值**，取错列时断言才分辨得出来）。"""
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            total_price=Decimal("100.00"),
            hall_price=Decimal("50.00"),
        )
        Performance.objects.create(
            order=order,
            user=taker,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("10.00"),
        )
        return order

    def _main(self, order):
        return Performance.objects.get(order=order, emp_type=EmpType.MAIN)

    # ── 请求 ─────────────────────────────────────────────────────
    def _post(self, url, payload):
        return self.api.post(url, payload, content_type="application/json")

    def _post_ok(self, url, payload):
        response = self._post(url, payload)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body

    def _transition(self, order, target):
        """走真端点改状态。

    ⚠️ `COMPLETED` 只有 `PENDING_REVIEW →` 这一条入边（`order_status._COMPLETED_SOURCE`）
    ⇒ 当前不是待验收时**先驱动一次中间迁移**；⛔ 不改目标态、不放宽断言。
"""
        target = str(target)
        if (
            target == str(OrderStatus.COMPLETED)
            and str(order.status) != str(OrderStatus.PENDING_REVIEW)
        ):
            self._post_ok(
                f"{ORDERS}/{order.order_no}/transition/",
                {"target": str(OrderStatus.PENDING_REVIEW)},
            )
            order.refresh_from_db()
        self._post_ok(f"{ORDERS}/{order.order_no}/transition/", {"target": target})
        order.refresh_from_db()

    def _set_hall_price(self, order, value):
        self._post_ok(f"{ORDERS}/{order.order_no}/hall-price/", {"price": value})
        order.refresh_from_db()


class DownstreamStatusTriggerTests(TestPeerBase):
    """上家侧改状态 ⇒ 一行下行 `order.update`（或按接受集一行都不写）。"""

    def test_a_transition_to_issue_enqueues_an_order_update(self):
        self._transition(self.order, "ISSUE")
        self.assertTrue(
            PeerSyncEvent.objects.filter(
                kind="order.update",
                status="pending",
                target_ref=self.order.order_no,
            ).exists()
        )

    def test_a_transition_outside_the_accepted_set_enqueues_no_downstream_update(self):
        """上家自己改「已完成」⇒ ⛔ 不推给下家（`COMPLETED` 不在下行接受集里）。

    ⚠️ **只断言下行那一支**：`COMPLETED` **在**上行触发集里，所以这里**不能**写成
    `filter(kind="order.update").exists()`——那会把上行也算进来。
"""
        self._transition(self.order, "COMPLETED")
        self.assertFalse(
            PeerSyncEvent.objects.filter(
                kind="order.update", binding_role="downstream"
            ).exists()
        )

    def test_an_order_without_a_bound_taker_enqueues_nothing(self):
        self._transition(self.plain_order, "ISSUE")
        self.assertFalse(PeerSyncEvent.objects.filter(kind="order.update").exists())


class UpstreamStatusTriggerTests(TestPeerBase):
    """下家侧本地改状态 ⇒ 一行上行 `order.update`（载荷是**上家的**目标态）。"""

    def test_a_downstream_local_pending_review_echoes_up(self):
        """下家本地把单改成待验收 ⇒ 记一行上行 `order.update`，载荷映射成上家的待验收。"""
        self._transition(self.local_order, "PENDING_REVIEW")
        event = PeerSyncEvent.objects.get(binding_role="upstream", kind="order.update")
        self.assertEqual(event.target_ref, self.local_link.peer_order_no)
        self.assertEqual(event.payload, {"status": "PENDING_REVIEW"})

    def test_completed_echoes_up_as_pending_review(self):
        """下家完成 ⇒ 上家收到的是**待验收**（`UPSTREAM_STATUS_MAP` 的有意映射）。

    ⚠️ `COMPLETED` 必须先经 `PENDING_REVIEW`（状态机唯一入边）⇒ `_transition` 驱动两次
    迁移；两次都写同一 `(绑定, 上家单号)` 的 pending 行 ⇒ 刷新成一行，不是两行。
"""
        self._transition(self.local_order, "COMPLETED")
        self.assertEqual(
            PeerSyncEvent.objects.get(binding_role="upstream").payload["status"],
            "PENDING_REVIEW",
        )

    def test_refunded_does_not_echo_up(self):
        self._transition(self.local_order, "REFUNDED")
        self.assertFalse(PeerSyncEvent.objects.filter(binding_role="upstream").exists())


class PriceTriggerTests(TestPeerBase):
    """四条**发单价写入路径**各一条判据（E-0021：「这类路径」而不是「这条路径」）。"""

    def test_changing_hall_price_enqueues_an_order_update(self):
        """写入点 #1：`/hall-price/`（列表工具栏「改发单价」）。"""
        self._set_hall_price(self.order, "66.00")
        event = PeerSyncEvent.objects.get(kind="order.update")
        self.assertEqual(event.binding_role, "downstream")
        self.assertEqual(event.target_ref, self.order.order_no)
        self.assertEqual(event.payload["hall_price"], "66.00")

    def test_assigning_with_a_price_enqueues_an_order_update(self):
        """写入点 #3：`/assign/` **带 `price`** 那一支——接手人就是绑定账号。"""
        self._post_ok(
            f"{ORDERS}/{self.order.order_no}/assign/",
            {"user_id": self.bound_taker.id, "price": "66.00"},
        )
        # 前置哨兵：指派真的发生了（否则「写了事件」可能是因为人压根没被指派）。
        self.assertEqual(self._main(self.order).user_id, self.bound_taker.id)
        event = PeerSyncEvent.objects.get(kind="order.update")
        self.assertEqual(event.payload["hall_price"], "66.00")

    def test_adjust_amount_landing_on_hall_price_enqueues_an_order_update(self):
        """写入点 #4：`/adjust-amount/` 在**没有 `taker_price`** 时写回 `Order.hall_price`。

    提成率 20% ⇒ 反推基价 = `13.20 ÷ 20 × 100 = 66.00`，落点正是发单价那一列。
"""
        self._post_ok(
            f"{PERFORMANCES}/{self._main(self.order).id}/adjust-amount/",
            {"amount_kind": "temp", "amount": "13.20"},
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.hall_price, Decimal("66.00"))
        event = PeerSyncEvent.objects.get(kind="order.update")
        self.assertEqual(event.payload["hall_price"], "66.00")

    def test_throw_to_hall_with_a_price_calls_the_price_hook(self):
        """写入点 #2 的**接线**判据：`/throw-to-hall/` **带 `price`** 那一支确实调到了助手。

    ⚠️ 为什么这条打在**调用**上而不是数据库行：回池钩子会先把接手人清空（见下一条），
    所以这个分支的数据库效果恒为「一行都不写」——只看行**分辨不出**挂钩在不在。
    打桩目标必须是**被查找的那个模块**（E-0029）：视图函数体里是
    `from apps.business.peer_dispatch import notify_order_price_changed` ⇒ 打
    `apps.business.peer_dispatch` 上的名字。

    ⭐ **D2 改准**：带接手人的单现在**抛不动**（有接手人 ⇒
    不许回池，`/throw-to-hall/` 不在例外清单里）⇒ 夹具改成无接单人。本用例钉的是
    「带价那一支**调到了**这个助手」，与接单人无关，判据一个字没放宽。
"""
        order = self._order(_no(10), status=OrderStatus.ISSUE, taker=None)
        with mock.patch(
            "apps.business.peer_dispatch.notify_order_price_changed"
        ) as hook:
            self._post_ok(
                f"{ORDERS}/{order.order_no}/throw-to-hall/", {"price": "66.00"}
            )
        hook.assert_called_once()

    def test_throw_to_hall_without_a_price_calls_no_price_hook(self):
        """同一端点的**不带价**那一支：⛔ 一次都不调（E-0021 的边界：`is not None`）。

    ⭐ **D2 改准**：同上，夹具改成无接单人（有接手人时这个端点现在被拒）。
"""
        order = self._order(_no(11), status=OrderStatus.ISSUE, taker=None)
        with mock.patch(
            "apps.business.peer_dispatch.notify_order_price_changed"
        ) as hook:
            self._post_ok(f"{ORDERS}/{order.order_no}/throw-to-hall/", {})
        hook.assert_not_called()

    def test_throw_to_hall_with_a_price_writes_no_order_update(self):
        """如实登记的行为：抛单这一步**没有可投递的人** ⇒ 价格那一步不排 `order.update`。

    ⭐ **D2 改准**：原版用**有接手人**的单抛单——回池钩子
    先清人，于是「价格那一步没有可投递的人」。D2 之后那个形状**不可达**（有接手人 ⇒
    抛单被拒），可达的形状是「本来就没人接」⇒ 前置哨兵从「清人的事实」换成
    「行上本来就没有人」。
    ⚠️ **如实登记的落差**：原版还断「下游收到那条 `order.withdraw`」——那条事件由
    `_on_pool_entry` 在**真的有接单人被释放**时才排（`released_taker is not None`），
    D2 之后它在这条入口上不再有时机 ⇒ 该半条判据**没有可达的形状**，已删除并在
    报告 §⑥ 登记为残差（它的非恒真版本由 `/clear-taker/` 那条路覆盖：
    `tests/test_peer_withdraw_triggers.py::ClearingTheTakerTests`）。
    现在两半都断：① 价**真的**写了（前置哨兵）；② **没有** `order.update`（本条要钉的）。
"""
        order = self._order(_no(12), status=OrderStatus.ISSUE, taker=None)
        self._post_ok(f"{ORDERS}/{order.order_no}/throw-to-hall/", {"price": "66.00"})
        order.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("66.00"))     # ①
        self.assertIsNone(self._main(order).user_id)             # 本来就没人接（可达形状）
        self.assertFalse(
            PeerSyncEvent.objects.filter(
                kind="order.update", target_ref=order.order_no
            ).exists()
        )                                                        # ②
