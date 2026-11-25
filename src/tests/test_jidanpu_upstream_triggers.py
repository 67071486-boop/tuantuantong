"""上家鸡蛋铺线 **触发点接线**（计划 Task 9；spec §6.2 / §6.4 / §11-17 / §11-22 / §11-23）。

本文件回答的不是「排事件 / 消费事件那两个函数对不对」（那是
`test_jidanpu_upstream_consume.py` 的），而是**每个业务动作到底有没有把那一行接上**、
以及 **A12 / A14 的「零上游写」到底是不是真的**——挂钩挂错地方 / 没挂上 / 顺手多排一条，
在这类改动里都是**静默**的（内核单元测试全绿）。

## 三条触发（spec §6.4 的触发表）

| 触发 | 落点 | 期望 |
|---|---|---|
| 我方订单进「已完成」 | `apply_transition`（**唯一状态写入口**，一处覆盖 `/transition/`、`/submit/`、批量…） | **恰好一条** `upstream.complete`（outbox，漏斗里⛔ 一次上游都不打） |
| 我方**退款**（→ `REFUNDED`） | 同上 | ⛔ **零上游写**（A12 / §15.4）+ 一条台账人话 |
| 我方**改问题单**（→ `ISSUE`） | 同上 | ⛔ **零上游写**（A14 / §15.6）+ 一条台账人话 |
| 我方**删除**（软删） | `recycle.soft_delete_order`（首次删除那一支） | ⛔ **零上游写**（A12 / §15.4）+ 一条台账人话 |

## 判据形状（⛔ 逐条都是「真实出口」）

- ⭐ **A12 / A14 的「零上游写」打在两个真实出口上**（spec §11-22 / §11-23 明文：⛔ 不许拿
  「本地状态对了」代替）：
  ① **台账里没有本线的在飞 outbox 行**（`direction="out"` 且 `kind ∈ OUTBOX_KINDS`）；
  ② **把整条 outbox 跑一遍**（`run_outbox`，注入**桩客户端**）⇒ 桩的 `calls` **恰好为空**。
  ⚠️ 判据 ① 单独不够（台账可能记了个不消费的 kind）；判据 ② 单独也不够（没有在飞行时
  `run_outbox` 压根不建连，「空」是恒真的）⇒ **两条一起**才构成「一次上游写都不打」。
  ⚠️ 夹具里**真的建了绑定 + 链路**（= 把机制装上膛）：漏掉它，上面两条都会**恒真**。
- ⚠️ **桩接没接上本身有判据**（`AGENTS.md` §5-G / E-0029）：`_StubLiveControl` 用**同一只桩**、
  **同一条链路**、**同一个 `run_outbox`** 证明「有一条真在飞行时它看得见调用」⇒ 上面那些
  「`calls` 为空」不是「桩压根没接上」。
- ⚠️ **来源闸（B7 / spec §11-17）**：带 `suppress_outbound_sync=True` 的写入（回拉写回）
  ⇒ **零** `upstream.*` out 事件；⚠️ 退款那一档连**台账人话也不许写**
  （否则「上家退我们的单」会被记成「我方退款不同步上游」，指错方向）。
- **零绑定即零成本**：`apply_transition` 是全仓最热的写入口 ⇒ 没有本线链路时，
  那次「进已完成」恰好**两次**上家表查询（2026-11-09 改准：原「只多一次」之外，
  发货闸 `_order_from_upstream` 还要查一次上家链路——两条 EXISTS 已并成一条；
  多一条 / 少一条都红，判据见 `test_zero_binding_costs_zero_queries` 的 docstring）。
- ⭐ **三个「只记账」出口都以链路为闸**（本线没在跟这张单 ⇒ 一个字都不写）：那句人话
  断言的是「**上家那边还挂着这张单**」（spec §15.4 / §13.1-R12）——本线没录过的单，
  上家那边什么都没有，记它就是**说假话**；口径与 `queue_complete` 的链路短路、
  反方向线 `StatusOutTests.test_a_status_change_without_any_link_queues_nothing` 一致。
  ⚠️ 这条闸是**实测**逼出来的：第一版没加，全量套件当场红了反方向线自己的反向控制
  `test_jidanpu_member_triggers.SoftDeleteTests.test_deleting_a_never_pushed_order_queues_nothing`
  （它按 `target_ref` 数**整张**台账，不是只数本线的 kind）。
- 断言一律打**数据库行**与**桩的调用**（⛔ 不断言 logger）。
- 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `kefu-up-trigger`、
  密码 canary、单号全零段 + 序号。
- ⛔ **零真实网络**：`_StubClient` 打在 SDK 边界（`client.call`）。
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.db import transaction
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_upstream
from apps.business.models import (
    EmpType,
    Game,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamOrderLink,
    Order,
    OrderStatus,
    Performance,
)
from apps.business.order_status import apply_transition
from tests.p5_auth_helpers import make_logged_in_client

ORDERS = "/api/business/orders"

BASE_URL = "https://jidanpu-upstream-triggers.test.invalid"
ACCOUNT = "kefu-up-trigger"
PASSWORD = "pw-canary-triggers"

#: 合成上家单号（全零段，一眼是夹具；⛔ 不用真单号）。
REMOTE_ID = "20260928000000000911"

#: 「我的订单」端点路径（**逐字**抄 spec §2.1；⛔ 不从模块常量派生）。
MY_ORDERS_PATH = "/OrderAjax/GetMyOrderList"

#: kind / 人话的**字面值**就是台账契约（E-0024：钉值，⛔ 不钉常量名）。
KIND_COMPLETE = "upstream.complete"
KIND_REFUND_SKIPPED = "upstream.refund_skipped"
KIND_DELETE_SKIPPED = "upstream.delete_skipped"
KIND_ISSUE_SKIPPED = "upstream.issue_skipped"

#: 三段人话各自**必须**点明的字眼（spec §15.4 / §15.6 与计划的「本文补」行）。
REFUND_WORDS = "上家账号无权限"
ISSUE_WORDS = "我方问题单不推上游"

#: 本文件走到的**端点门**（`OrderViewSet.ORDER_PERMISSIONS`）；`groups` 只建组、⛔ 不放行。
NODES = (
    "order.change_status",            # /transition/（非 COMPLETED 目标）
    "order.change_status_completed",  # /transition/（目标 = COMPLETED）
    "order.delete",                   # /delete/
)

#: 本线的两张表（「查询计数」那条判据按它们过滤 SQL）。
UPSTREAM_TABLES = (
    JidanpuUpstreamOrderLink._meta.db_table,
    JidanpuUpstreamBinding._meta.db_table,
)


class _StubClient:
    """SDK 边界替身：记下**每一次调用**，并按 `rows` 回一份「我的订单」。

    ⚠️ 打在 `client.call` 这一层（SDK 边界）⇒ 本线所有上游读写都要经过它（E-0029：
    桩要打在**被查找的那个模块**上）。⛔ 零真实网络。
    """

    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {}

    def ensure_login(self):
        pass

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, dict(kwargs)))
        self.last_envelope = {
            "code": 0, "msg": "success", "data": self.rows, "count": len(self.rows),
        }
        return self.rows

    @property
    def paths(self) -> list[str]:
        return [path for _method, path, _kwargs in self.calls]


class _ClientCtx:
    """`jidanpu.open_client_with_http(binding)` 的替身（上下文管理器里就是桩客户端）。"""

    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client, None

    def __exit__(self, *_exc):
        return False


class _ConnRecorder:
    """`jidanpu.open_client_with_http` 的替身：**记「建了几次连」**。

    ⚠️ 漏斗（`apply_transition`）里**只准排队、⛔ 一次上游都不打** ⇒ 这个计数必须是 0。
    ⚠️ 只有「建连」被记还不够：真打了上游却复用旧连接是看不见的 ⇒ 断言里**同时**看
    桩自己的 `calls`（两条一起才是「零上游调用」）。
    """

    def __init__(self):
        self.opens = 0
        self.client = _StubClient()

    def __call__(self, binding):
        self.opens += 1
        return _ClientCtx(self.client)


def _no(n: int) -> str:
    """20 位订单号（`OrderViewSet.lookup_value_regex = r"\\d{20}"`）。"""
    return f"2026092800000000{n:04d}"


# ══ 契约（⛔ 钉值，不钉常量名）═════════════════════════════════════════════════


class TriggerContractTests(SimpleTestCase):
    """新台账 kind 与人话的**字面值**（改名 = 静默换语义）。"""

    def test_the_local_only_kinds_are_the_verbatim_ones(self):
        self.assertEqual(
            jidanpu_upstream.KIND_REFUND_SKIPPED, KIND_REFUND_SKIPPED
        )
        self.assertEqual(
            jidanpu_upstream.KIND_DELETE_SKIPPED, KIND_DELETE_SKIPPED
        )
        self.assertEqual(
            jidanpu_upstream.KIND_ISSUE_SKIPPED, KIND_ISSUE_SKIPPED
        )

    def test_the_local_only_kinds_are_never_outbox_todos(self):
        """⭐ A12 / A14 的第一道：这三个 kind **不进** `OUTBOX_KINDS`。

        它们记的是「我方刻意**不**打上游」这个事实 ⇒ 一旦混进 `OUTBOX_KINDS`，
        `run_outbox` 就会把它们当待办去消费（= 真的打了上游）。判别力：把任一个
        加进 `OUTBOX_KINDS` ⇒ 本用例红，且 `..._never_writes_upstream` 那三条也红。
        """
        for kind in (KIND_REFUND_SKIPPED, KIND_DELETE_SKIPPED, KIND_ISSUE_SKIPPED):
            with self.subTest(kind=kind):
                self.assertNotIn(kind, jidanpu_upstream.OUTBOX_KINDS)

    def test_the_two_human_reasons_say_what_the_spec_requires(self):
        """spec §15.4 / §15.6 的字眼：⛔ 不许只剩一句「已跳过」。"""
        self.assertIn(REFUND_WORDS, jidanpu_upstream.REFUND_NOT_SYNCED_REASON)
        self.assertIn(REFUND_WORDS, jidanpu_upstream.DELETE_NOT_SYNCED_REASON)
        self.assertIn(ISSUE_WORDS, jidanpu_upstream.ISSUE_NOT_SYNCED_REASON)


# ══ 夹具 ═══════════════════════════════════════════════════════════════════════


class _TriggerFixture(TenantTestCase):
    """一张带 MAIN 绩效行的订单 + 一份启用绑定 + 一条链路。

    ⚠️ 必须 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
    ⚠️ 绑定与链路是**上膛**用的：没有它们，「零上游写」那两个否定判据会**恒真**。
    """

    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="上家触发点夹具游戏")
        self.owner = get_user_model().objects.create_user(
            username=ACCOUNT, password=PASSWORD, first_name="测试客服甲"
        )
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.owner, jidanpu_base_url=BASE_URL, account=ACCOUNT,
            password=PASSWORD, enabled=True,
        )

    # ── 夹具工厂 ────────────────────────────────────────────────────
    def _order(self, no, *, status=OrderStatus.IN_PROGRESS):
        order = Order.objects.create(
            game=self.game, order_no=no, status=status,
            total_price=Decimal("88.00"), project_text="合成项目甲",
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("20.00"),
        )
        return order

    def _link(self, order, *, remote_id=REMOTE_ID, terminated=False):
        return JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=order, upstream_order_id=remote_id,
            in_hall=False, terminated_at=None if not terminated else order.created_at,
        )

    def _client(self, username="upsync-actor"):
        """真登录（fail-closed）的客服客户端；默认给当前租户全部游戏。"""
        return make_logged_in_client(username=username, groups=("客服",), nodes=NODES)

    def _post(self, client, url, payload=None):
        return client.post(url, payload or {}, content_type="application/json").json()

    def _transition(self, client, order, target):
        return self._post(
            client, f"{ORDERS}/{order.order_no}/transition/", {"target": str(target)}
        )

    # ── 断言工具 ────────────────────────────────────────────────────
    def _out_events(self, order, kind=None):
        qs = JidanpuSyncEvent.objects.filter(
            direction="out", kind__in=jidanpu_upstream.OUTBOX_KINDS,
            target_ref__endswith=order.order_no,
        )
        return qs if kind is None else qs.filter(kind=kind)

    def _run_outbox(self, stub):
        """把这一份绑定的整条 outbox 跑一遍（注入桩客户端）⇒ 真出口。

        ⚠️ 没有在飞事件时 `run_outbox` **一次连都不建**（⛔ 桩也不会被调到）——所以
        「零上游写」必须**同时**看台账（判据 ①）与桩的 `calls`（判据 ②），⛔ 单看后者是恒真的。
        """
        return jidanpu_upstream.run_outbox(self.binding, now=timezone.now(), client=stub)

    def _note(self, order, kind):
        rows = JidanpuSyncEvent.objects.filter(kind=kind, target_ref=order.order_no)
        self.assertEqual(rows.count(), 1, f"{kind} 台账行数不对：{rows.count()}")
        return rows.get()


# ══ ① 我方订单进「已完成」⇒ 恰好一条 upstream.complete（漏斗只排队）═══════════


class CompletionTriggerTests(_TriggerFixture):
    def test_completion_queues_exactly_one_event_and_never_talks_upstream(self):
        """⭐ spec §6.2 / §11-17：漏斗里**只排队**。

        - 判据 ①：**恰好一条** `upstream.complete`（`target_ref` 带绑定归属、`direction="out"`）；
        - 判据 ②：漏斗那一次调用**没有建上游连接**、桩看到的上游调用 **0 次**。

        判别力：把 `order_status.apply_transition` 尾上那条调用删掉 ⇒ 判据 ① 红（0 条）；
        把「只排队」改成「漏斗里直接打上游」⇒ 判据 ② 红。
        """
        order = self._order(_no(1), status=OrderStatus.PENDING_REVIEW)
        link = self._link(order)
        recorder = _ConnRecorder()

        with mock.patch.object(
            jidanpu_upstream.jidanpu, "open_client_with_http", recorder
        ):
            body = self._transition(self._client(), order, OrderStatus.COMPLETED)

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)

        rows = self._out_events(order)
        self.assertEqual(rows.count(), 1, list(rows.values("kind", "target_ref")))
        event = rows.get()
        self.assertEqual(event.kind, KIND_COMPLETE)
        self.assertEqual(event.target_type, "order")
        self.assertEqual(event.target_ref, f"{self.binding.pk}|{order.order_no}")
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.payload["link_id"], link.pk)
        self.assertEqual(event.payload["upstream_binding_id"], self.binding.pk)
        # 桩计数 = 0（漏斗里一次上游都不打）。
        self.assertEqual(recorder.opens, 0)
        self.assertEqual(recorder.client.calls, [])

    def test_zero_binding_costs_zero_queries(self):
        """⭐ 零绑定即零成本（照反方向线 `ZeroBindingCostTests` 的形状）。

        `apply_transition` 是全仓最热的写入口 ⇒ 没有本线链路时，那次「进已完成」
        恰好**两次**上家表查询：
        ① 发货闸 `_order_from_upstream` 的上家链路检查（2026-11-09 拍板「上家来源单不走
        自动发货」时挂在闸顶；**两条 EXISTS 已并成一条** Order 侧 OR-join ⇒ 只占这一次）；
        ② `queue_complete` 的取链路 + `select_related` 绑定（一次 JOIN）。
        （5774197e 加闸时漏跑本网 ⇒ 存量红；本钉值更新 = 把闸的合法成本钉进网，⛔ 不是放宽：
        断言 `1` 会红在「闸被回退」、断言 `> 2` 会红在「并查询退回两条 EXISTS / 取链路被拆」。）

        判别力：把 `queue_complete` 的取链路拆成两次查询 ⇒ 本用例红；
        把 `_order_from_upstream` 退回两条独立 EXISTS ⇒ 本用例红（真查询数也翻回 +2）。
        """
        order = self._order(_no(2), status=OrderStatus.PENDING_REVIEW)

        with CaptureQueriesContext(connection) as ctx:
            with transaction.atomic():
                apply_transition(
                    order, OrderStatus.COMPLETED,
                    nodes=frozenset({"order.change_status_completed"}),
                    actor="upsync-actor",
                )

        mine = [
            q["sql"] for q in ctx.captured_queries
            if any(name in q["sql"] for name in UPSTREAM_TABLES)
        ]
        self.assertEqual(len(mine), 2, mine)
        self.assertEqual(self._out_events(order).count(), 0)


# ══ ② A12：我方退款 / 删除 ⇒ 零上游写 ═════════════════════════════════════════


class RefundZeroWriteTests(_TriggerFixture):
    def test_our_refund_never_writes_upstream(self):
        """⭐ spec §11-22 / A12：退款 ⇒ 上游调用 **0**（桩断言）+ 台账人话点明「上家账号无权限」。

        ⚠️ 夹具里**有**绑定 + 链路（机制上了膛）⇒ 若实现顺手把退款也排成一条
        `upstream.complete`，`run_outbox` 会真的去打上游、桩的 `calls` 非空 ⇒ 红。
        """
        order = self._order(_no(10), status=OrderStatus.IN_PROGRESS)
        self._link(order)

        body = self._transition(self._client(), order, OrderStatus.REFUNDED)

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        # 判据 ①：没有本线的在飞 outbox 行。
        self.assertEqual(self._out_events(order).count(), 0)
        # 判据 ②：整条 outbox 跑一遍 ⇒ 桩的上游调用恰好为空。
        stub = _StubClient()
        stats = self._run_outbox(stub)
        self.assertEqual(stub.calls, [])
        self.assertEqual(
            (stats.done, stats.failed, stats.skipped, stats.abandoned), (0, 0, 0, 0)
        )
        # 判据 ③：一条人话台账（点明「上家账号无权限」）。
        note = self._note(order, KIND_REFUND_SKIPPED)
        self.assertEqual(note.direction, "in")
        self.assertEqual(note.status, "skipped")
        self.assertIn(REFUND_WORDS, note.reason)
        self.assertEqual(note.payload["from_status"], str(OrderStatus.IN_PROGRESS))

    def test_a_suppressed_refund_write_leaves_neither_an_event_nor_a_note(self):
        """⭐ 来源闸（B7 / spec §11-17）也管这条人话：**上家退我们的单**不算「我方退款」。

        回拉跟退走 `apply_transition(..., suppress_outbound_sync=True)`（`follow_refund`）
        ⇒ 那一刻**两个出口都不许动**：⛔ 不排 `upstream.*` 事件、⛔ 也不写
        「我方退款不同步上游」的人话（写了就是**指错方向**：那是上家退的，不是我方退的）。

        判别力：把 `on_order_status_changed` 挪到 `if not suppress_outbound_sync:` 之外 ⇒ 红。
        """
        order = self._order(_no(11), status=OrderStatus.IN_PROGRESS)
        self._link(order)

        with transaction.atomic():
            apply_transition(
                order, OrderStatus.REFUNDED,
                nodes=frozenset({"order.change_status"}), actor="upsync-machine",
                suppress_outbound_sync=True,
            )

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        self.assertEqual(self._out_events(order).count(), 0)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                kind=KIND_REFUND_SKIPPED, target_ref=order.order_no
            ).count(),
            0,
        )
        stub = _StubClient()
        self._run_outbox(stub)
        self.assertEqual(stub.calls, [])


class DeleteZeroWriteTests(_TriggerFixture):
    def test_delete_never_writes_upstream(self):
        """⭐ spec §11-22 / A12：删除 ⇒ 上游调用 **0**（桩断言）+ 台账人话。

        ⚠️ 判据打在本线的 outbox 上（`upstream.*`）：`/delete/` 会**同时**排另外两条线的
        退单事件（`hall.withdraw` / `member.withdraw`），那与本条线无关——⛔ 别拿「零事件」
        这种笼统判据（它会把另外两条线的正确行为误判成红线）。
        """
        order = self._order(_no(20), status=OrderStatus.IN_PROGRESS)
        self._link(order)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/delete/", {"isDelEmp": "N"}
        )

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertIsNotNone(order.deleted_at)
        self.assertEqual(self._out_events(order).count(), 0)
        stub = _StubClient()
        self._run_outbox(stub)
        self.assertEqual(stub.calls, [])
        note = self._note(order, KIND_DELETE_SKIPPED)
        self.assertEqual(note.status, "skipped")
        self.assertIn(REFUND_WORDS, note.reason)


# ══ ③ A14：我方改问题单 ⇒ 零上游写 ═══════════════════════════════════════════


class IssueZeroWriteTests(_TriggerFixture):
    def test_our_issue_status_never_writes_upstream(self):
        """⭐ spec §11-23 / A14：`/transition/` 到问题单 ⇒ 上游调用 **0** + 台账人话。

        ⚠️ 上家**全局禁用**了「问题单」（spec §2.3 实测）⇒ 业务方裁定「下家改问题单，
        不动上家状态」（§15.6）。判据同 A12：台账无在飞行 + 整条 outbox 跑一遍桩为空。
        """
        order = self._order(_no(30), status=OrderStatus.IN_PROGRESS)
        self._link(order)

        body = self._transition(self._client(), order, OrderStatus.ISSUE)

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.ISSUE)
        self.assertEqual(self._out_events(order).count(), 0)
        stub = _StubClient()
        self._run_outbox(stub)
        self.assertEqual(stub.calls, [])
        note = self._note(order, KIND_ISSUE_SKIPPED)
        self.assertEqual(note.status, "skipped")
        self.assertIn(ISSUE_WORDS, note.reason)
        self.assertEqual(note.payload["from_status"], str(OrderStatus.IN_PROGRESS))


# ══ ④ ⭐「零链路 ⇒ 零动作」：三个只记账出口都**以链路为闸** ══════════════════


class UnlinkedOrderWritesNothingTests(_TriggerFixture):
    """本线的**所有**出口都以「本线有没有在跟这张单」为闸（spec §15.4 / A12）。

    那条人话断言的是「**上家那边还挂着这张单**」（spec §13.1-R12）——本线没录过的单，
    上家那边什么都没有，记它就是**说假话**。判据形状与 `queue_complete` 的链路短路、
    反方向线 `StatusOutTests.test_a_status_change_without_any_link_queues_nothing`
    **同一条口径**（「零链路 ⇒ 零动作」）。

    判别力：把任一支的 `_tracks_order` 闸去掉 ⇒ 对应那条用例红（台账多一行）；
    ⚠️ **实测**：本实现一开始**没有**这道闸，全量套件当场红了反方向线自己的反向控制
    `test_jidanpu_member_triggers.SoftDeleteTests.test_deleting_a_never_pushed_order_queues_nothing`
    （它按 `target_ref` 数**整张**台账，不是只数本线的 kind）。
    """

    def test_a_refund_without_a_link_writes_nothing(self):
        order = self._order(_no(60), status=OrderStatus.IN_PROGRESS)

        body = self._transition(self._client(), order, OrderStatus.REFUNDED)

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._out_events(order).count(), 0)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(target_ref=order.order_no).count(), 0
        )

    def test_an_issue_without_a_link_writes_nothing(self):
        order = self._order(_no(61), status=OrderStatus.IN_PROGRESS)

        body = self._transition(self._client(), order, OrderStatus.ISSUE)

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._out_events(order).count(), 0)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(target_ref=order.order_no).count(), 0
        )

    def test_a_delete_without_a_link_writes_nothing(self):
        order = self._order(_no(62), status=OrderStatus.IN_PROGRESS)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/delete/", {"isDelEmp": "N"}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._out_events(order).count(), 0)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(target_ref=order.order_no).count(), 0
        )


# ══ ⑤ ⭐ A16：我方接手（→「代练中」）⛔ 不推上家 ═════════════════════════════


class OtherStatusesQueueNothingTests(_TriggerFixture):
    """⭐ A16 / spec §16.2：进「代练中」**不推**上家「代练中」。

    依据（业务方原话）：「只要订单不管被谁接取了 一般默认都会变成代练中
    所以不需要主动推一次」——且**与实测一致**（spec §2.3：我方 `RecOrderByInternal` 接单后，
    对方那张单**自己就变**「代练中」）⇒ 本线**不额外推一次**。

    判别力：给 `on_order_status_changed` 加一条「IN_PROGRESS ⇒ queue_complete」
    ⇒ 本用例红（台账多一条在飞事件、`run_outbox` 会真的去打上游）。
    """

    def test_moving_into_progress_queues_nothing(self):
        order = self._order(_no(3), status=OrderStatus.UNASSIGNED)
        self._link(order)

        body = self._transition(self._client(), order, OrderStatus.IN_PROGRESS)

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._out_events(order).count(), 0)
        stub = _StubClient()
        self._run_outbox(stub)
        self.assertEqual(stub.calls, [])


# ══ ⑥ 控制：桩真的接上了（否则上面那些「空」是恒真的）════════════════════════


class _StubLiveControl(_TriggerFixture):
    """E-0029：桩接没接上**本身要有判据**。

    用的是**同一只桩**、**同一条链路**、**同一个 `run_outbox`**，只把「有没有一条真在飞行」
    这一个变量换掉 ⇒ 上面那些 `calls == []` 才不是「桩压根没接上」。
    """

    def test_the_stub_sees_the_read_when_a_completion_event_is_really_queued(self):
        order = self._order(_no(40), status=OrderStatus.COMPLETED)
        self._link(order)
        self.assertEqual(jidanpu_upstream.queue_complete(order), 1)

        stub = _StubClient()
        self._run_outbox(stub)

        # 桩看得见这一次上游**读**（回读「我的订单」）⇒ 桩是活的。
        self.assertEqual(stub.paths, [MY_ORDERS_PATH])


# ══ ⑦ B7：带来源闸的「已完成」写回不排提交事件 ═══════════════════════════════


class SuppressedCompletionTests(_TriggerFixture):
    def test_status_pull_write_does_not_queue_a_completion(self):
        """⭐ spec §11-17 / B7：带 `suppress_outbound_sync=True` 的写入 ⇒ **零** `upstream.*`。

        依据：回拉写回也经 `apply_transition`；缺这道闸，每写回一笔就会排一条发往
        上家的「提交完成」（反向映射非双射 ⇒ 会把刚设的状态改掉）。

        判别力：把本线的调用挪出 `if not suppress_outbound_sync:` ⇒ 本用例红。
        """
        order = self._order(_no(50), status=OrderStatus.PENDING_REVIEW)
        self._link(order)

        with transaction.atomic():
            apply_transition(
                order, OrderStatus.COMPLETED,
                nodes=frozenset({"order.change_status_completed"}),
                actor="upsync-machine", suppress_outbound_sync=True,
            )

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertIsNotNone(order.completed_at)
        self.assertEqual(self._out_events(order).count(), 0)
        stub = _StubClient()
        self._run_outbox(stub)
        self.assertEqual(stub.calls, [])
