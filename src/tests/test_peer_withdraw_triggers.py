"""上下游 P2 Task 3：**六类挂钩点**的端点级判据（spec §7.1 / §16.2）。

本文件回答的不是「入队内核写得对不对」（那在 `tests/test_peer_order_push.py` 的
`EnqueueOrderEventTests` 里逐条钉过），而是**每个语义动作到底有没有把那一行接上**——
挂钩挂错地方 / 根本没挂上在这类改动里是**静默**的（内核单元测试全绿）。

## 判据形状（每条挂钩各一条用例，全部打**真 HTTP**、断**数据库行**）

| 语义动作 | 端点 | 期望事件 |
|---|---|---|
| 清空接手人 | `POST /orders/<单号>/clear-taker/` | 一条 `order.withdraw`（`taker_removed`） |
| 换人 | `POST /orders/<单号>/assign/` | 一条 `order.withdraw`（`taker_removed`）**给旧**接手人的绑定 |
| 同一个人再保存（A→A） | 同上 | **一行都不写** |
| 绩效垃圾桶 | `POST /performances/<id>/delete/` | 一条 `order.withdraw`（`taker_removed`） |
| 抢单 | `POST /hall/<单号>/claim/` | 一条 `order.grab` |
| 删单 | `POST /orders/<单号>/delete/` | 一条 `order.withdraw`（`deleted`） |
| 改已退单 | `POST /orders/<单号>/transition/`（`target=REFUNDED`） | 一条 `order.withdraw`（`refunded`） |

⭐ **清人 / 垃圾桶 / 退单回池是同一条漏斗**：三者的清人副作用都落在
`performance._on_pool_entry`（注册在 `UNASSIGNED` / `WAITING_GRAB` 两个目标态上），
所以本文件对 `/clear-taker/` 与绩效垃圾桶各打一次**真端点**——它们证明的是
「漏斗覆盖了这两个入口」，⛔ 不是给它们各写第二套挂钩。

## 反向控制（没有生效绑定 ⇒ 一行都不写）

`enqueue_order_event` 开头那次绑定查询是**承重**的：六条路径在没有生效绑定时必须
**一行都不写**（否则就是给普通打手也建「空投递」）。单条正向用例抓不住它——
把绑定过滤整段去掉，正向用例照样绿。所以本文件有一条独立的六路径反向控制，
且**故意在库里先放一条别人的生效绑定**（漏掉 `account=taker` 过滤的实现会被它抓住）。
"""

from datetime import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
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
from apps.business.peer_inbound import apply_events
from apps.business.peer_sync import hash_token, new_token
from apps.core.error_codes import ErrorCode
from tests.order_rules_helpers import release_taker
from tests.p5_auth_helpers import grant_games, make_logged_in_client

ORDERS = "/api/business/orders"
HALL = "/api/business/hall"
PERFORMANCES = "/api/business/performances"

#: D2 的后端 `msg`（R4 逐字，spec §4.1.2）：前端 `msg(errText(e))` 原样显示这一整句。
#: ⛔ 期望值写字面量（E-0024）——不从被测模块 import 常量，才抓得到「文案被改坏」。
D2_MSG = "该订单已有接手人，请先清除接手人，再改为「待抢单」或「未分配」"

#: 本文件走到的**订单/绩效端点门**（`OrderViewSet.ORDER_PERMISSIONS` +
#: `PerformanceViewSet.PERFORMANCE_PERMISSIONS`）。`groups` 只建 Group、**不放行**，
#: 所以每个真实登录的夹具都要显式声明它要过的门。
#: - `order.assign_taker` / `order.submit_payout_price` ⇒ `/assign/`
#: - `order.change_status` ⇒ `/transition/`（目标 `REFUNDED`）
#: - `order.clear_taker` ⇒ `/clear-taker/`
#: - `order.delete` ⇒ `/delete/`
#: - `order.submit_payout_price` ⇒ 垃圾桶（`/performances/<id>/delete/`）
#: - `grab_hall.take_order` ⇒ 大厅抢单
#: - `order.list` ⇒ 订单读取（`get_object()` 的 queryset 走列表门）
NODES = (
    "order.assign_taker",
    "order.submit_payout_price",
    "order.change_status",
    # ⭐ 计划 ④：本文件打 `/throw-to-hall/`，该门已改判**独占** 36 ⇒ 补它。
    "order.send_to_grab_hall",
    "order.clear_taker",
    "order.delete",
    "grab_hall.take_order",
    "order.list",
)


def _no(n):
    """20 位订单号（订单对外标识；`OrderViewSet.lookup_value_regex = r"\\d{20}"`）。"""
    return f"2026092600000000{n:04d}"


class _WithdrawTriggerFixture(TenantTestCase):
    """共用夹具：一款游戏、若干账号、一条绑定、一张带 MAIN 绩效行的订单。

    ⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里，普通 `TestCase`
    的 search_path 里没有 `business_*` 表（照 `tests/test_peer_models.py` 的先例）。

    ⚠️ `PeerDownstreamBinding` 的 `account` / `peer_base_url` / `token_in_hash` /
    `token_out` **全部非空** ⇒ 少给一个就 `IntegrityError`。
    """

    def setUp(self):
        # 游戏名租户内唯一（模型约束）；这个名字是本文件自己造的合成值。
        self.game = Game.objects.create(name="上下游撤单夹具游戏")

    # ── 账号与绑定 ────────────────────────────────────────────────
    def _user(self, username):
        return get_user_model().objects.create_user(
            username=username, password="pw-peer-withdraw"
        )

    def _taker(self, username):
        """一个能被指派的账号：带提成档案（缺档案会回落全额，这里给一个真实形态）。"""
        user = self._user(username)
        StaffProfile.objects.create(
            user=user,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=无游戏=拒）
        # ⇒ 能被 /assign/ 指派的夹具账号要持本类的游戏（专钉：tests/test_order_assign_game_gate.py）。
        grant_games(user, self.game)
        return user

    def _bind(self, account, *, base_url="https://peer-down.test.invalid", state=None):
        """给账号建一条**我方为上家**的绑定（生效态；`state` 可覆盖成已解除）。"""
        return PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url=base_url,
            # 对手方给我的令牌只存哈希；我发给下家的是明文——按字段口径给，⛔ 不两列都塞明文。
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            **({"state": state} if state is not None else {}),
        )

    # ── 订单 ─────────────────────────────────────────────────────
    def _order(
        self,
        no,
        *,
        status=OrderStatus.UNASSIGNED,
        taker=None,
        hall_price="100.00",
        total_price="100.00",
    ):
        """订单 + 它的 MAIN 绩效行。

        ⚠️ MAIN 行是夹具的一部分：`/assign/`（`main_performance_of`）与垃圾桶都**必须**
        取到它；进池钩子缺行时虽会补建一条，但那不是本文件要测的形状。
        """
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            total_price=Decimal(total_price),
            hall_price=Decimal(hall_price),
        )
        Performance.objects.create(
            order=order,
            user=taker,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("20.00"),
        )
        return order

    def _main(self, order):
        return Performance.objects.get(order=order, emp_type=EmpType.MAIN)

    # ── 请求 ─────────────────────────────────────────────────────
    def _client(self, username="peer-withdraw-cs"):
        """真实登录（fail-closed）的客服客户端；默认给当前租户全部游戏。"""
        return make_logged_in_client(username=username, groups=("客服",), nodes=NODES)

    def _post(self, client, url, payload=None):
        return client.post(
            url, payload or {}, content_type="application/json"
        ).json()

    def _withdraws(self, order):
        return PeerSyncEvent.objects.filter(
            kind="order.withdraw", target_ref=order.order_no
        )


class ClearingTheTakerTests(_WithdrawTriggerFixture):
    """① 清空接手人（`/clear-taker/`）——本仓**唯一**的清人口（`/assign` 不能清）。"""

    def test_clearing_the_taker_enqueues_one_withdraw(self):
        """`/clear-taker/` 成功 ⇒ 一条 `order.withdraw`，原因 `taker_removed`。

        判别力：去掉 `_on_pool_entry` 里那次入队 ⇒ 本用例的行数断言红。
        """
        account = self._taker("peer-withdraw-clear-account")
        binding = self._bind(account)
        order = self._order(_no(1), status=OrderStatus.IN_PROGRESS, taker=account)

        body = self._post(
            self._client(),
            f"{ORDERS}/{order.order_no}/clear-taker/",
            {"isDelEmp": "N"},
        )

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        # 前置哨兵：清人**真的发生了**（否则「写了事件」可能是因为人压根没被清）。
        self.assertIsNone(self._main(order).user_id)

        rows = self._withdraws(order)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.meta["withdraw_reason"], "taker_removed")
        self.assertEqual(row.binding_id, str(binding.pk))
        self.assertEqual(row.status, PeerSyncEvent.Status.PENDING)
        # ⚠️ `occurred_at` 取 `order.updated_at`（`apply_transition` 刚写过）⇒ 入队必须排在
        #    **业务写入之后**，否则这里会拿到动作前的时刻（下家会按 stale 良性跳过）。
        self.assertEqual(row.meta["occurred_at"], order.updated_at.isoformat())

class ReassignTests(_WithdrawTriggerFixture):
    """② / ③ 换人与 A→A（`/assign/`）。"""

    def test_reassigning_to_another_account_withdraws_the_old_binding(self):
        """换人 ⇒ 撤单**只**发给**旧**接手人的绑定（⛔ 不是新接手人那条）。

        判别力：挂钩里把 `old_taker` 换成 `taker`（新接手人）⇒ 本用例的 `binding_id`
        断言红；把整段挂钩删掉 ⇒ 行数断言红。
        ⚠️ 新旧接手人**各有**一条生效绑定、且取值不同——否则「发给了谁」不可辨。
        """
        old = self._taker("peer-withdraw-old")
        old_binding = self._bind(old)
        new = self._taker("peer-withdraw-new")
        new_binding = self._bind(new, base_url="https://peer-new.test.invalid")
        order = self._order(_no(3), status=OrderStatus.IN_PROGRESS, taker=old)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": new.id}
        )

        self.assertEqual(body["code"], 0, body)
        # 前置哨兵：换人真的发生了。
        self.assertEqual(self._main(order).user_id, new.id)

        rows = self._withdraws(order)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.meta["withdraw_reason"], "taker_removed")
        self.assertEqual(row.binding_id, str(old_binding.pk))
        self.assertNotEqual(row.binding_id, str(new_binding.pk))
        # 指派本身照旧出一条 `order.assign`（发**新**接手人的绑定）——两条互不顶替。
        assigns = PeerSyncEvent.objects.filter(
            kind="order.assign", target_ref=order.order_no
        )
        self.assertEqual(assigns.count(), 1)
        self.assertEqual(assigns.get().binding_id, str(new_binding.pk))

    def test_reassigning_to_the_same_account_enqueues_no_withdraw(self):
        """A→A（同一个人再保存）⇒ 撤单**一行都不写**。

        判别力：去掉 `perf.user_id != taker.id` 这个比较 ⇒ 这里会走一遍
        `reset_taker_performance` 并入队一条撤单 ⇒ 行数断言红。
        """
        account = self._taker("peer-withdraw-same")
        self._bind(account)
        order = self._order(_no(4), status=OrderStatus.IN_PROGRESS, taker=account)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": account.id}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._main(order).user_id, account.id)
        # 前置哨兵：这一次请求**真的走了指派的成功路径**（不是被别的原因挡住了）。
        self.assertEqual(
            PeerSyncEvent.objects.filter(
                kind="order.assign", target_ref=order.order_no
            ).count(),
            1,
        )
        self.assertEqual(self._withdraws(order).count(), 0)


class PerformanceTrashTests(_WithdrawTriggerFixture):
    """④ 绩效垃圾桶——证明它走的是**同一条漏斗**（⛔ 没有第二套挂钩）。"""

    def test_the_performance_trash_endpoint_enqueues_one_withdraw(self):
        """垃圾桶删掉「当前接单人那条 MAIN 行」⇒ 一条 `order.withdraw`（`taker_removed`）。

        ⚠️ 本端点自己**不写**撤单事件：它走 `apply_transition(order, UNASSIGNED, …)`，
        清人由 `_on_pool_entry` 这条**既有漏斗**完成。本用例因此同时是
        「漏斗覆盖了垃圾桶这个入口」的判据。
        """
        account = self._taker("peer-withdraw-trash")
        binding = self._bind(account)
        order = self._order(_no(5), status=OrderStatus.IN_PROGRESS, taker=account)
        perf_id = self._main(order).id

        body = self._post(self._client(), f"{PERFORMANCES}/{perf_id}/delete/")

        self.assertEqual(body["code"], 0, body)
        # 前置哨兵：那一行真的被软删了、接手人也真的被清了。
        perf = Performance.objects.get(pk=perf_id)
        self.assertIsNotNone(perf.deleted_at)
        self.assertIsNone(perf.user_id)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)

        rows = self._withdraws(order)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().meta["withdraw_reason"], "taker_removed")
        self.assertEqual(rows.get().binding_id, str(binding.pk))


class GrabTests(_WithdrawTriggerFixture):
    """⑤ 抢单（`/hall/<单号>/claim/`）。"""

    def test_grabbing_in_the_hall_enqueues_a_grab_event(self):
        """抢单成功 ⇒ 一条 `order.grab`（发给**抢到的那个人**的绑定）。

        判别力：`claim` 里不放入队 ⇒ `DoesNotExist` 红。
        """
        client = self._client("peer-withdraw-grabber")
        grabber = get_user_model().objects.get(username="peer-withdraw-grabber")
        binding = self._bind(grabber)
        order = self._order(_no(6), status=OrderStatus.WAITING_GRAB)

        body = self._post(client, f"{HALL}/{order.order_no}/claim/")

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._main(order).user_id, grabber.id)

        rows = PeerSyncEvent.objects.filter(
            kind="order.grab", target_ref=order.order_no
        )
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.binding_id, str(binding.pk))
        self.assertEqual(row.status, PeerSyncEvent.Status.PENDING)


class DeleteTests(_WithdrawTriggerFixture):
    """⑥ 删单（`POST /orders/<单号>/delete/`）。"""

    def test_soft_deleting_the_order_enqueues_a_deleted_withdraw(self):
        """软删 ⇒ 一条 `order.withdraw`，原因 `deleted`，且 `occurred_at` **严格晚于删单前**的
        `order.updated_at`。

        判别力：删单动作里不放入队 ⇒ 行数断言红；原因写成 `taker_removed` ⇒ 原因断言红；
        **入队不带显式 `occurred_at`**（= 用默认的 `order.updated_at`）⇒ 下面那条
        「严格晚于 `before`」红——`soft_delete_order` 不推 `Order.updated_at`，默认值恰好
        等于 `before`。⛔ 断言**不能**写成 `occurred_at == order.updated_at`：那在
        「入队挪到删除之前」和「删单不推 updated_at」两种实现下都成立，没有判别力。
        """
        account = self._taker("peer-withdraw-delete")
        binding = self._bind(account)
        order = self._order(_no(7), status=OrderStatus.IN_PROGRESS, taker=account)
        # 删单**之前**的最后写入时刻：删除动作不会推它（`soft_delete_order` 的
        # `update_fields` 只有 `deleted_at` / `deleted_by`）。
        before = order.updated_at

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/delete/", {"isDelEmp": "N"}
        )

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertIsNotNone(order.deleted_at)
        # 前置哨兵：删除确实**没有**推 `updated_at`（否则下面那条断言会因为另一条原因成立）。
        self.assertEqual(order.updated_at, before)

        rows = self._withdraws(order)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.meta["withdraw_reason"], "deleted")
        self.assertEqual(row.binding_id, str(binding.pk))
        # ⚠️ 解析回 `datetime` 再比（不靠 ISO 字符串的字面比较）。
        self.assertGreater(
            datetime.fromisoformat(row.meta["occurred_at"]),
            before,
            "删单事件的 occurred_at 必须是**删除时刻**，不是上一次订单写入的时刻",
        )

    def test_deleting_an_order_without_a_main_row_still_succeeds(self):
        """反向控制（真实数据形态）：**没有 MAIN 绩效行**的单删得掉、且不 500。

        `ensure_main_performance_of` 的 docstring 记着「缺行」是真实存在的数据形态
        （不经 `/orders/` 录单端点落库的旧单 / ORM 夹具）。删单的挂钩若要读那行，
        必须用**容错**查询（`main_performance_of` 缺行时抛 `Performance.DoesNotExist`
        ⇒ 本端点会变成 500）。判据：`code == 0` 且一行事件都不写。
        """
        order = self._order(_no(2), status=OrderStatus.IN_PROGRESS)
        self._main(order).delete()

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/delete/", {"isDelEmp": "N"}
        )

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertIsNotNone(order.deleted_at)
        self.assertEqual(PeerSyncEvent.objects.count(), 0)


class RefundTests(_WithdrawTriggerFixture):
    """⑦ 改已退单（`/transition/` 目标 `REFUNDED`）。"""

    def test_marking_the_order_refunded_enqueues_a_refunded_withdraw(self):
        """订单 → `REFUNDED` ⇒ 一条 `order.withdraw`，原因 `refunded`。

        判别力：不在 `settlement_hooks` 上注册这个目标钩子 ⇒ 行数断言红。

        ⚠️ **如实的现状**（本仓今天的事实，不是本任务的期望）：`on_order_refunded`
        清的是 MAIN 行的**两个金额列**，它**保留** `user`——它不是「清接手人」的钩子
        （`reset_taker_performance` 只被 `_on_pool_entry` 与 `/assign/` 调用）。
        所以本钩子读到的人就是这一行下面断言的那一个。⛔ 不要据此改退款的语义。
        """
        account = self._taker("peer-withdraw-refund")
        binding = self._bind(account)
        order = self._order(_no(8), status=OrderStatus.IN_PROGRESS, taker=account)

        body = self._post(
            self._client(),
            f"{ORDERS}/{order.order_no}/transition/",
            {"target": "REFUNDED"},
        )

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        self.assertEqual(self._main(order).user_id, account.id)

        rows = self._withdraws(order)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.meta["withdraw_reason"], "refunded")
        self.assertEqual(row.binding_id, str(binding.pk))

    def test_a_later_clear_taker_does_not_downgrade_the_refunded_row(self):
        """「先改已退单、再清接手人」在同一 pending 窗口内 ⇒ **仍然只有一行**且原因是 `refunded`。

        这是 spec §16.2「同一张单在同一绑定上可能被多条路径同时想发撤单」在本仓的**真实形态**：
        改已退单（`/transition/`）**不会**清接手人，清人是紧随其后的第二个动作
        （`/clear-taker/`，从 `REFUNDED` 回 `UNASSIGNED`，走 `_on_pool_entry` 漏斗）。
        ⚠️ 两个原因都指向**同一个绑定账号**时命中去重：`taker_removed` 不得把已经待投递的
        `refunded` 降级（**反向不升级**）⇒ 下家收到的仍是「订单已被改成已退单」。
        ⛔ 两条用例都不直接连调 `enqueue_order_withdrawn`——判据必须打在**真端点**上。
        """
        account = self._taker("peer-withdraw-refund-then-clear")
        binding = self._bind(account)
        order = self._order(_no(9), status=OrderStatus.IN_PROGRESS, taker=account)
        client = self._client()

        body = self._post(
            client, f"{ORDERS}/{order.order_no}/transition/", {"target": "REFUNDED"}
        )
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._withdraws(order).count(), 1)

        body = self._post(
            client, f"{ORDERS}/{order.order_no}/clear-taker/", {"isDelEmp": "N"}
        )
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(self._main(order).user_id)

        rows = self._withdraws(order)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.binding_id, str(binding.pk))
        self.assertEqual(row.meta["withdraw_reason"], "refunded")


class NoBindingControlTests(_WithdrawTriggerFixture):
    """反向控制：**没有生效绑定**时，六条路径一行都不写（绑定查询是承重的）。"""

    def test_no_binding_means_no_rows_on_any_of_the_six_paths(self):
        """六条路径全部走一遍，`PeerSyncEvent` 必须**恰好 0 行**。

        ⚠️ 库里**故意先放一条别人的生效绑定**：漏掉 `account=taker` 这条过滤的实现
        （例如 `filter(state=ACTIVE).first()`）会在本用例里写出行 ⇒ 被抓住。
        ② 的 `/assign/` 也只能给新接手人写 `order.assign`——新接手人同样无绑定。
        """
        # 旁观者：有生效绑定，但本用例的任何动作都不该发给他。
        self._bind(self._taker("peer-withdraw-bystander"))
        unbound_a = self._taker("peer-withdraw-unbound-a")
        unbound_b = self._taker("peer-withdraw-unbound-b")
        client = self._client("peer-withdraw-unbound-actor")

        # ① 清人
        order = self._order(_no(11), status=OrderStatus.IN_PROGRESS, taker=unbound_a)
        body = self._post(
            client, f"{ORDERS}/{order.order_no}/clear-taker/", {"isDelEmp": "N"}
        )
        self.assertEqual(body["code"], 0, body)

        # ② 换人（旧、新接手人都无绑定）
        order = self._order(_no(12), status=OrderStatus.IN_PROGRESS, taker=unbound_a)
        body = self._post(
            client, f"{ORDERS}/{order.order_no}/assign/", {"user_id": unbound_b.id}
        )
        self.assertEqual(body["code"], 0, body)

        # ③ 绩效垃圾桶
        order = self._order(_no(13), status=OrderStatus.IN_PROGRESS, taker=unbound_a)
        perf_id = self._main(order).id
        body = self._post(client, f"{PERFORMANCES}/{perf_id}/delete/")
        self.assertEqual(body["code"], 0, body)

        # ④ 抢单（操作人自己无绑定）
        order = self._order(_no(14), status=OrderStatus.WAITING_GRAB)
        body = self._post(client, f"{HALL}/{order.order_no}/claim/")
        self.assertEqual(body["code"], 0, body)

        # ⑤ 删单
        order = self._order(_no(15), status=OrderStatus.IN_PROGRESS, taker=unbound_a)
        body = self._post(
            client, f"{ORDERS}/{order.order_no}/delete/", {"isDelEmp": "N"}
        )
        self.assertEqual(body["code"], 0, body)

        # ⑥ 改已退单
        order = self._order(_no(16), status=OrderStatus.IN_PROGRESS, taker=unbound_a)
        body = self._post(
            client, f"{ORDERS}/{order.order_no}/transition/", {"target": "REFUNDED"}
        )
        self.assertEqual(body["code"], 0, body)

        # 判据打在**数据库行**上（E-0009 / E-0024）。
        # ⭐ D-0303（团团通大厅镜像）：
        #    本线的六类事件（order.assign / order.grab / order.withdraw / order.update /
        #    截图两类）仍是**恰好 0 行**——它们的判据「taker 是绑定账号」没动；
        #    但 ④ 抢单会让那张单**离开大厅** ⇒ 大厅镜像按 spec §2.2 **广播**一条
        #    `hall.take` 给旁观者的生效绑定（大厅推送与「谁接了单」无关，正是本线要的）。
        #    ⇒ 判据拆两半：订单域事件 0 行 + 大厅域恰好 1 行 `hall.take`。
        order_kinds = PeerSyncEvent.objects.exclude(kind__startswith="hall.").exclude(
            kind__startswith="order.screenshot."
        )
        self.assertEqual(order_kinds.count(), 0)
        hall_rows = PeerSyncEvent.objects.filter(kind="hall.take")
        self.assertEqual(hall_rows.count(), 1)
        self.assertEqual(
            hall_rows.get().binding_id,
            str(PeerDownstreamBinding.objects.get(account__username="peer-withdraw-bystander").pk),
        )


class UpgradeBranchReachabilityTests(_WithdrawTriggerFixture):
    """「升级一条尚未投递的 `taker_removed`」这条分支**是否可达**的实证用例（Task 2 评审遗留）。

    评审当时怀疑「没有任何真实路径会在同一个 pending 窗口里为同一 `(订单, 绑定)` 写出两种
    撤单原因」。实测结论：**可达**，但**跨请求**——同一 **pending 窗口**（后台 worker 尚未
    投递之前）里，先经池内清人写出 `taker_removed`，再把同一个绑定账号派回来、随后删单 /
    改退单，就会命中升级分支。下面两条用例**打真端点**走完整条链，⛔ 不直接调
    `enqueue_order_withdrawn`（那只能证明函数自己，证明不了路径可达）。
    """

    def _clear_then_reassign(self, client, order, account):
        """先清人（写出 `taker_removed`）、再把同一个账号派回来（仍是 pending 窗口内）。"""
        body = self._post(
            client, f"{ORDERS}/{order.order_no}/clear-taker/", {"isDelEmp": "N"}
        )
        self.assertEqual(body["code"], 0, body)
        rows = self._withdraws(order)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().meta["withdraw_reason"], "taker_removed")

        body = self._post(
            client, f"{ORDERS}/{order.order_no}/assign/", {"user_id": account.id}
        )
        self.assertEqual(body["code"], 0, body)
        # 重新指派**不**再写撤单（这一支的旧接手人是 `None`）⇒ 窗口里仍只有那一条。
        self.assertEqual(self._withdraws(order).count(), 1)

    def test_deleting_after_a_pending_taker_removed_upgrades_that_row(self):
        """清人 → 派回同一绑定账号 → 删单 ⇒ **同一行**被升级成 `deleted`（⛔ 不是两行）。"""
        account = self._taker("peer-withdraw-upgrade-delete")
        binding = self._bind(account)
        order = self._order(_no(21), status=OrderStatus.IN_PROGRESS, taker=account)
        client = self._client()

        self._clear_then_reassign(client, order, account)
        body = self._post(
            client, f"{ORDERS}/{order.order_no}/delete/", {"isDelEmp": "N"}
        )
        self.assertEqual(body["code"], 0, body)

        rows = self._withdraws(order)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.binding_id, str(binding.pk))
        self.assertEqual(row.meta["withdraw_reason"], "deleted")
        # 升级只动撤单那一行：重新指派写下的 `order.assign` 不被牵连。
        self.assertEqual(
            PeerSyncEvent.objects.filter(
                kind="order.assign", target_ref=order.order_no
            ).count(),
            1,
        )

    def test_refunding_after_a_pending_taker_removed_upgrades_that_row(self):
        """清人 → 派回同一绑定账号 → 改已退单 ⇒ **同一行**被升级成 `refunded`。"""
        account = self._taker("peer-withdraw-upgrade-refund")
        binding = self._bind(account)
        order = self._order(_no(22), status=OrderStatus.IN_PROGRESS, taker=account)
        client = self._client()

        self._clear_then_reassign(client, order, account)
        body = self._post(
            client,
            f"{ORDERS}/{order.order_no}/transition/",
            {"target": "REFUNDED"},
        )
        self.assertEqual(body["code"], 0, body)

        rows = self._withdraws(order)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.binding_id, str(binding.pk))
        self.assertEqual(row.meta["withdraw_reason"], "refunded")


class ThrowToHallPoolEntryTests(_WithdrawTriggerFixture):
    """⭐ C5（2026-09-26 修复波）：`_on_pool_entry` 注册在 `WAITING_GRAB` 上的那一半。

    清人 / 垃圾桶那两条用例走的都是 `→ UNASSIGNED`。本类原本走**真端点**
    `POST /orders/<单号>/throw-to-hall/` 把单从 `IN_PROGRESS` 送进 `WAITING_GRAB`，
    证明注册在 `WAITING_GRAB` 上的那个目标态钩子**真的会**给原接手人的绑定发撤单。

    有接手人时 `/throw-to-hall/` 被拒（不在例外清单里），不会顺手清人。本类拆成两条：
    ① 有接手人 ⇒ 抛单被拒（逐字 `msg` + 九列不写 + 无留痕 + 无撤单事件）；
    ② **先显式清除接单人**（R1 例外①的产品动作）再抛 ⇒ `WAITING_GRAB` 且撤单事件照发。
    ⚠️ ② 里那条事件的**发出者**因此是清人那一步（同一个 `_on_pool_entry`，注册在
    `UNASSIGNED` 上）；本文件如实登记这一点，⛔ 不写成「抛单这一步发的」。
    """

    def test_throwing_to_the_hall_is_refused_while_a_taker_is_present(self):
        """⭐ D2：**有接手人** ⇒ `/throw-to-hall/` 被拒（1002 + `msg` **逐字** + 一行不写）。

        ⛔ `/throw-to-hall/` **不在** R1 的例外清单里（spec §4.1.2 的 ⛔ 段）——
        例外只给「显式清除当前接单人」那两处。
        """
        account = self._taker("peer-withdraw-throw-hall")
        self._bind(account)
        order = self._order(_no(23), status=OrderStatus.IN_PROGRESS, taker=account)
        main = self._main(order)
        before = (
            order.status,
            main.user_id,
            main.bonus_amount,
            main.taker_price,
            main.claimed_at,
        )
        logs_before = OperationLog.objects.count()

        body = self._post(self._client(), f"{ORDERS}/{order.order_no}/throw-to-hall/")

        self.assertEqual(body["code"], ErrorCode.BUSINESS, body)      # 1002
        # ⛔ 逐字（R4）：不用 `assertIn`——半句相等会让「前缀被加回来」也全绿。
        self.assertEqual(body["msg"], D2_MSG, body)
        order.refresh_from_db()
        main.refresh_from_db()
        self.assertEqual(
            (
                order.status,
                main.user_id,
                main.bonus_amount,
                main.taker_price,
                main.claimed_at,
            ),
            before,
        )
        # 静默清人正是本闸要消灭的形态 ⇒ 那半条判据必须留在这里。
        self.assertFalse(self._withdraws(order).exists())
        self.assertEqual(OperationLog.objects.count(), logs_before)

    def test_throwing_to_the_hall_after_releasing_the_taker_reaches_the_pool(self):
        """先**显式清除接单人**（R1 例外①的产品动作）再抛单 ⇒ `WAITING_GRAB` 且撤单事件照发。

        ⚠️ **如实登记（E-0011）**：D2 生效后 `/throw-to-hall/` 不再能落在「有接手人」的行上
        （它不在例外清单里）⇒ `WAITING_GRAB` 上那半个钩子今天只能用「**先**在池内把撤单事件
        排掉」的方式观察到。因此下面这条事件的**发出者**是清人那一步
        （`UNASSIGNED` 上的同一个 `_on_pool_entry`），不是抛单这一步。
        判据两半都在：事件**照发**（`count == 1`）且**不重复发**（抛单那一步没有再加一条）。
        """
        account = self._taker("peer-withdraw-throw-hall")
        binding = self._bind(account)
        order = self._order(_no(23), status=OrderStatus.IN_PROGRESS, taker=account)

        release_taker(order, nodes={"order.change_status"})
        order.refresh_from_db()
        # 前置哨兵：清人那一步真的落库了（否则下面「抛单成功」可能只是单子本来就没人接）。
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(self._main(order).user_id)

        body = self._post(self._client(), f"{ORDERS}/{order.order_no}/throw-to-hall/")

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)

        rows = self._withdraws(order)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.meta["withdraw_reason"], "taker_removed")
        self.assertEqual(row.binding_id, str(binding.pk))
        self.assertEqual(row.status, PeerSyncEvent.Status.PENDING)


class CrossSideReassignTests(_WithdrawTriggerFixture):
    """⭐ C1（2026-09-26 修复波）：换人撤单的**跨进程**判据（真端点入队 → 接收侧落库）。

    送的是**发送侧真实入队写下的那一行**，喂给的是**接收侧真实的 `apply_events`**，
    事件对象按 `peer_worker.deliver_one` 组装出站请求的同一形状
    （`kind` / `target_ref` / 事件级 meta 展开在顶层 / `payload`）。

    判别力（变异证据）：把 `/assign/` 换人档的 `occurred_at=timezone.now()` 去掉 ⇒ 这条事件的
    `occurred_at` 退化成"上一次订单写入"（= 首单 `order.assign` 那一刻），与下家
    `PeerOrderLink.last_applied_at` **相等** ⇒ 接收侧按"相等=重复"判 `stale` ⇒ 本用例红
    （下家永不退单）。
    """

    #: 下家侧那条绑定（`PeerUpstreamBinding`）的域名（`*.test.invalid` 是保留域，E-0077）。
    DOWNSTREAM_URL = "https://peer-x-downstream.test.invalid"

    def _wire(self, row):
        """把库里那一行组装成出站事件对象（形状照 `deliver_one`：meta 在前、`payload` 在后）。"""
        return {
            "idempotency_key": row.idempotency_key,
            "kind": row.kind,
            "target_ref": row.target_ref,
            **(row.meta or {}),
            "payload": row.payload,
        }

    def test_reassign_withdraw_reaches_the_downstream_and_refunds_it(self):
        old = self._taker("peer-x-reassign-old")
        old_binding = self._bind(old)
        new = self._taker("peer-x-reassign-new")
        self._bind(new, base_url="https://peer-x-new.test.invalid")
        # 下家侧：一条"我是下家"的绑定 + 建这条绑定的客服（游戏授权走他，A15）。
        downstream_cs = self._user("peer-x-downstream-cs")
        upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室乙",
            peer_base_url=self.DOWNSTREAM_URL,
            peer_account="peer-x-upstream-account",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-x-local.test.invalid",
            bound_by=downstream_cs,
        )
        order = self._order(_no(31))
        client = self._client()

        # ① 第一次指派（UNASSIGNED → IN_PROGRESS）：真端点 + 真入队 ⇒ 一条 `order.assign`。
        body = self._post(
            client, f"{ORDERS}/{order.order_no}/assign/", {"user_id": old.id}
        )
        self.assertEqual(body["code"], 0, body)
        assign_row = PeerSyncEvent.objects.get(
            kind="order.assign", target_ref=order.order_no
        )
        # 首单动作之后的权威时刻（视图手上那个实例与本测试的不是同一个 ⇒ 必须回读）。
        order.refresh_from_db()
        # ⭐ **I-1起，这条事件的水位不再等于 `order.updated_at`**：`/assign/`
        #    两处入队现在显式传 `occurred_at=timezone.now()`（否则同一次请求里更早的
        #    `order.update` 会把它顶成 `stale`）。⇒ 下家应用到的水位拿**事件自己带的时刻**比；
        #    `order.updated_at` 另存一份，供 ③ 的 R-3 哨兵用（两者只差毫秒级）。
        assigned_at = datetime.fromisoformat(assign_row.meta["occurred_at"])
        order_updated_at = order.updated_at

        # ② 把首单事件真喂给接收侧 ⇒ 下家建出那张单，`last_applied_at` = 首单那一刻。
        first = apply_events([self._wire(assign_row)], binding=upstream_binding)
        self.assertEqual(first.applied, 1, first)
        link = PeerOrderLink.objects.get(
            upstream_binding=upstream_binding, peer_order_no=order.order_no
        )
        self.assertEqual(link.last_applied_at, assigned_at)
        downstream_order = link.order

        # ③ 换人（R-3：订单已是 IN_PROGRESS、迁移跳过）。⚠️ **D-0369 起**「一次
        #    `order.save()` 都不发生」只剩机器路径（`ctx.user=None`）成立：人工改派在指派
        #    直写点必写一行（`hall_publisher` + `updated_at`）。
        body = self._post(
            client, f"{ORDERS}/{order.order_no}/assign/", {"user_id": new.id}
        )
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        # ⭐ D-0369 哨兵（替代旧的「updated_at 不变」哨兵——旧前提已被有意的行为变更取代）：
        #    人工改派写发单人=操作人、推 `updated_at`；而下面的水位哨兵**照旧不动** ⇒
        #    「订单行被写了 ≠ 水位被推」这个 I-1 解耦，现在证明得比以前更硬。
        self.assertEqual(
            order.hall_publisher,
            get_user_model().objects.get(username="peer-withdraw-cs"),
        )
        self.assertGreater(order.updated_at, order_updated_at)
        self.assertEqual(link.last_applied_at, assigned_at)

        withdraw_row = self._withdraws(order).get()
        self.assertEqual(withdraw_row.meta["withdraw_reason"], "taker_removed")
        self.assertEqual(withdraw_row.binding_id, str(old_binding.pk))

        # ④ 真喂给接收侧 ⇒ 下家那张单必须变 `REFUNDED` 并写一条订单日志。
        second = apply_events([self._wire(withdraw_row)], binding=upstream_binding)
        self.assertEqual(second.applied, 1, second)
        self.assertEqual(second.skipped, 0, second)
        downstream_order.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(downstream_order.status, OrderStatus.REFUNDED)
        self.assertGreater(link.last_applied_at, assigned_at)
        log = (
            OperationLog.objects.filter(
                action="order.peer_withdraw", order_no=downstream_order.order_no
            )
            .latest("id")
        )
        self.assertEqual(log.reason, "发单方将订单接手人移除")
