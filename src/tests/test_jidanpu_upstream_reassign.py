"""上家鸡蛋铺线：上家**重新指派**（复活同一张上家单）⇒ 重录新单 + 链路重置（D-0324 / E-0298）。

生产实锤（诊断，租户 S 两单卡死）：上家把已退单的订单**重新指派**给我方账号
——复用同一张上家单、同一 `orderId`、状态回「待上号 / 代练中」——而「停跟踪 = 往后不为它
写任何东西」的旧语义把重录这条路一并堵死；同时①段的 24h 窗（按上家录单时间收窄）与
③段 `_links_due_for_check` 的 `terminated_at__isnull=True` 过滤 ⇒ **窗外**复活连被核到的
机会都没有（窗口洞）。

七组判据（对应拍板口径）：

① 已终止链路 + 在途行 ⇒ 重录**新**单 + 链路重置 + `upstream.reassign` 台账**恰一条**；
② 旧退单**一字不动**（状态 / 更新时刻断言）；
③ 已终止 + 非在途（待验收 / 已退单 / 已完成）⇒ 维持现状**零写入**；
④ 未终止链路在途 ⇒ 照旧走既有回拉分支（回归钉，行为一字不改）；
⑤ 链路重置后既有回拉 / 跟退**自然接上**（重置后改上家状态 ⇒ 新单能跟）；
⑥ 窗口洞：①②行集里**没有**、只有 `key1` 单查看得到的复活 ⇒ `recheck_terminated_links`
   兜底重核收口（b 选型的落点；同一夹具先证明 `sync_my_orders` 看不到它）；
⑦ 同绑定同上家单号仍只有**一条**链路（约束钉：UPDATE 原行 ⛔ 不 INSERT 第二条；
   第二轮也不造第三张单）。

⚠️ 判据打**数据库行**（`AGENTS.md` §5-G），⛔ 不打 logger；桩打在 SDK 边界、零真实网络。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：地址 `…test.invalid`、密码 `pw-canary-*`、
单号 `20261028…` 全零段。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu, jidanpu_upstream
from apps.business.models import (
    Game,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamOrderLink,
    Order,
    OrderStatus,
)
from tests.p5_auth_helpers import make_logged_in_client

#: 端点路径**逐字**抄 spec §2.1（⛔ 不从被测常量派生）。
MY_ORDERS_PATH = "/OrderAjax/GetMyOrderList"

BASE_URL = "https://jidanpu-upstream-a.test.invalid"
PASSWORD = "pw-canary-reassign"
BINDING_USER = "kefu-reassign-a"

#: 合成上家单号（全零段，一眼是合成值）。
REMOTE_ID = "20261028000000000101"
OLD_ORDER_NO = "20261028000000000111"

#: 已终止链路的「非在途」三档（终态远端标签）⇒ 一档都不该触发重录。
NOT_IN_FLIGHT_TOKENS = ("待验收", "已退单", "已完成")


class _WindowStub:
    """「我的订单」替身：按**请求里的收窄参数**回行——窗口① / 退单视图② / `key1` 单查。

    - `default`：不带 `key1` 也不带 `orderSta` 的那一发（窗口①）；
    - `refund`：带 `orderSta=已退单` 的那一发（窗口②）；
    - `by_key`：带 `key1` 的那一发（按单查，**不带窗**——正是窗口洞收口走的那一跳）；
    - 逐字记下每一次 `query`（判据要看「哪一发带了什么参数」）。
    """

    def __init__(self, *, default=(), refund=(), by_key=None):
        self.default = [dict(row) for row in default]
        self.refund = [dict(row) for row in refund]
        self.by_key = {str(key): dict(row) for key, row in (by_key or {}).items()}
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {}

    def ensure_login(self):
        pass

    def call(self, method, path, **kwargs):
        query = dict(kwargs.get("query") or {})
        self.calls.append((method, path, query))
        assert path == MY_ORDERS_PATH, f"未预期的端点：{method} {path}"
        key = str(query.get("key1") or "")
        if key:
            row = self.by_key.get(key)
            rows = [dict(row)] if row is not None else []
        elif query.get("orderSta"):
            rows = [dict(row) for row in self.refund]
        else:
            rows = [dict(row) for row in self.default]
        page_rows = rows if int(query["page"]) == 1 else []
        self.last_envelope = {
            "code": 0, "msg": "success", "data": page_rows, "count": len(rows),
        }
        return page_rows


class _Base:
    """夹具混入（照 `test_jidanpu_upstream_status._Base` 的形状；刻意不继承 TestCase）。"""

    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 10, 28, 12, 0, tzinfo=dt_timezone.utc)
        #: 客服（= 绑定人）：给 `order.change_status` 节点——⑤ 那一轮「上家已退单 ⇒ 跟退」
        #: 的 `apply_transition` 要过它（`order_status._CHANGE_STATUS`）；⛔ 缺节点时跟退
        #: 会以「被拒 + 台账」的形式绿掉，测不到真的接上了。
        make_logged_in_client(
            username=BINDING_USER, groups=("客服",), nodes=("order.change_status",),
        )
        self.user = User.objects.get(username=BINDING_USER)
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account="acc-reassign-a",
            password=PASSWORD, enabled=True,
        )
        self.game = Game.objects.create(name="测试游戏甲")

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def upstream_row(self, *, order_sta="待上号", order_id=REMOTE_ID, **extra):
        row = {
            "orderId": order_id,
            "orderIdView": f"VIEW-{order_id}",
            "orderSta": order_sta,
            "gameName": "测试游戏甲",
            "gameArea": "安卓",
            "gameServer": "微信区",
            "project": "合成项目",
            "orderContent": "合成需求",
            "orderRemark": "合成备注",
            "roleName": "合成角色",
            "gameAcc": "acc-synth",
            "zgameAcc": "sub-synth",
            "recFee": "88.00",
            "price": "120.00",
        }
        row.update(extra)
        return row

    def refunded_order_with_terminated_link(
        self, *, remote_id=REMOTE_ID, order_no=OLD_ORDER_NO, status=OrderStatus.REFUNDED,
    ):
        """一张**已退单**的旧我方订单 + 一条**已终止**的链路（miss 计数刻意非零，
        钉住重录时一并清零）。"""
        order = Order.objects.create(
            game=self.game, order_no=order_no, status=status,
            total_price=Decimal("100.00"),
        )
        link = JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=order, upstream_order_id=remote_id,
            terminated_at=self.now - timedelta(days=2),
            last_in_at=self.now - timedelta(days=2),
            miss_count=2,
            miss_since=self.now - timedelta(hours=1),
            miss_at=self.now - timedelta(minutes=10),
            completed_submitted_at=self.now - timedelta(days=3),
            in_hall=True,
        )
        return order, link

    def sync(self, stub, *, now=None):
        return jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=stub, now=now or self.now,
        )


class UpstreamReassignTests(_Base, TenantTestCase):
    """①–⑤⑦：`sync_my_orders` 里的复活重录分支 + 重置后的回拉接续 + 约束钉。"""

    # ── ① 重录 + 链路重置 + 台账恰一条（D-0324 的拍板口径）───────────────────

    def test_terminated_link_with_in_flight_row_is_re_recorded_and_link_reset(self):
        old, link = self.refunded_order_with_terminated_link()
        stub = _WindowStub(default=[self.upstream_row(order_sta="待上号")])
        outcome = self.sync(stub)
        self.assertEqual(outcome.recorded, 1)
        #: 新单：未分配池、录单人 = 该绑定客服（与首次录池同一语义）。
        self.assertEqual(Order.objects.count(), 2)
        new_order = Order.objects.exclude(pk=old.pk).get()
        self.assertEqual(new_order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(new_order.sales_user_id, self.binding.user_id)
        self.assertEqual(str(new_order.total_price), "88.00")
        #: 链路**重置挂到新单**：同一条（pk 不变、总数 1——⑦ 的约束钉）、各列归位。
        link.refresh_from_db()
        self.assertEqual(JidanpuUpstreamOrderLink.objects.count(), 1)
        self.assertEqual(link.order_id, new_order.pk)
        self.assertIsNone(link.terminated_at)
        self.assertFalse(link.in_hall)
        self.assertEqual(link.miss_count, 0)
        self.assertIsNone(link.miss_since)
        self.assertIsNone(link.miss_at)
        self.assertIsNone(link.completed_submitted_at)
        self.assertEqual(link.last_in_at, self.now)
        #: 台账**恰一条** `upstream.reassign`，挂在新单上，人话写清「重新指派、已重录新单」。
        events = JidanpuSyncEvent.objects.filter(kind="upstream.reassign")
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertEqual(event.target_ref, new_order.order_no)
        self.assertIn("重新指派", event.reason)
        self.assertIn("重录", event.reason)
        self.assertEqual(event.payload.get("previous_order_no"), OLD_ORDER_NO)

    # ── ② 旧退单一字不动 ───────────────────────────────────────────────────

    def test_explicit_upstream_refund_allows_a_later_reassignment_of_the_same_number(self):
        old, link = self.refunded_order_with_terminated_link(status=OrderStatus.IN_PROGRESS)
        JidanpuUpstreamOrderLink.objects.filter(pk=link.pk).update(terminated_at=None)
        refund = _WindowStub(refund=[self.upstream_row(order_sta="已退单")])
        self.assertEqual(self.sync(refund).followed, 1)
        old.refresh_from_db()
        self.assertEqual(old.status, OrderStatus.REFUNDED)

        active = _WindowStub(default=[self.upstream_row(order_sta="待上号")])
        reassigned = self.sync(active, now=self.now + timedelta(minutes=1))
        repeated = self.sync(active, now=self.now + timedelta(minutes=2))
        old.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual((reassigned.recorded, repeated.recorded), (1, 0))
        self.assertEqual(old.status, OrderStatus.REFUNDED)
        self.assertNotEqual(link.order_id, old.pk)
        self.assertEqual(link.order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(JidanpuUpstreamOrderLink.objects.count(), 1)
        self.assertEqual(Order.objects.count(), 2)

    def test_the_old_refunded_order_is_untouched(self):
        old, _link = self.refunded_order_with_terminated_link()
        before = {
            "status": old.status, "updated_at": old.updated_at,
            "total_price": old.total_price, "order_no": old.order_no,
        }
        self.sync(_WindowStub(default=[self.upstream_row(order_sta="代练中")]))
        old.refresh_from_db()
        self.assertEqual(old.status, before["status"])
        self.assertEqual(old.updated_at, before["updated_at"])
        self.assertEqual(old.total_price, before["total_price"])
        self.assertEqual(old.order_no, before["order_no"])
        #: 旧单名下**零新台账**（重录那条挂在新单上，⛔ 不把旧单的历史搅进来）。
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(target_ref=OLD_ORDER_NO).count(), 0,
        )

    # ── ③ 已终止 + 非在途 ⇒ 维持现状零写入 ──────────────────────────────────

    def test_terminated_link_with_non_in_flight_rows_stays_untouched(self):
        """终态只刷新观察时间，保留历史订单和终止时间。"""
        for index, token in enumerate(NOT_IN_FLIGHT_TOKENS):
            with self.subTest(token=token):
                remote_id = f"2026102800000000020{index}"
                old, link = self.refunded_order_with_terminated_link(
                    remote_id=remote_id, order_no=f"2026102800000000030{index}",
                )
                terminated_before = link.terminated_at
                stub = _WindowStub(
                    default=[self.upstream_row(order_sta=token, order_id=remote_id)],
                )
                outcome = self.sync(stub)
                self.assertEqual(outcome.recorded, 0)
                self.assertEqual(Order.objects.filter(pk=old.pk).count(), 1)
                self.assertEqual(Order.objects.count(), index + 1)  # 只有旧单们
                link.refresh_from_db()
                self.assertEqual(link.order_id, old.pk)
                self.assertEqual(link.terminated_at, terminated_before)
                self.assertEqual(link.last_in_at, self.now)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind="upstream.reassign").count(), 0,
        )

    # ── ④ 未终止链路在途 ⇒ 照旧走回拉分支（回归钉）─────────────────────────

    def test_older_active_result_cannot_reopen_a_newer_terminal_confirmation(self):
        old, link = self.refunded_order_with_terminated_link()
        later = self.now + timedelta(minutes=2)
        stub = _WindowStub(default=[self.upstream_row()])
        read = stub.call

        def confirm_terminal_while_waiting(*args, **kwargs):
            result = read(*args, **kwargs)
            if len(stub.calls) == 1:
                terminal = _WindowStub(by_key={REMOTE_ID: self.upstream_row(order_sta="已退单")})
                jidanpu_upstream.recheck_terminated_links(binding=self.binding, client=terminal, now=later)
            return result

        with mock.patch.object(stub, "call", side_effect=confirm_terminal_while_waiting):
            outcome = self.sync(stub)
        link.refresh_from_db()
        self.assertEqual((outcome.recorded, outcome.skipped), (0, 0))
        self.assertEqual(link.order_id, old.pk)
        self.assertEqual(link.last_in_at, later)
        self.assertEqual(link.terminated_at, self.now - timedelta(days=2))
        self.assertEqual(Order.objects.count(), 1)

    def test_manual_window_confirmation_blocks_a_late_reassignment(self):
        old, link = self.refunded_order_with_terminated_link()
        active = Order.objects.create(
            game=self.game, order_no="20261028000000000701", status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("88.00"),
        )
        active_id = "20261028000000000702"
        JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=active, upstream_order_id=active_id,
        )
        later = self.now + timedelta(minutes=2)
        stub = _WindowStub(default=[self.upstream_row()])
        read = stub.call
        manual = _WindowStub(default=[
            self.upstream_row(order_sta="已退单"), self.upstream_row(order_id=active_id),
        ])

        def confirm_terminal_while_waiting(*args, **kwargs):
            result = read(*args, **kwargs)
            if len(stub.calls) == 1:
                jidanpu_upstream.pull_statuses(binding=self.binding, client=manual, now=later)
            return result

        with mock.patch.object(stub, "call", side_effect=confirm_terminal_while_waiting):
            outcome = self.sync(stub)
        link.refresh_from_db()
        self.assertEqual(len(manual.calls), 2)
        self.assertEqual((outcome.recorded, outcome.skipped), (0, 0))
        self.assertEqual(link.order_id, old.pk)
        self.assertEqual(link.last_in_at, later)
        self.assertEqual(link.terminated_at, self.now - timedelta(days=2))
        self.assertEqual(Order.objects.count(), 2)

    def test_unterminated_in_flight_link_still_walks_the_pull_branch(self):
        order = Order.objects.create(
            game=self.game, order_no=OLD_ORDER_NO, status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("100.00"),
        )
        link = JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=order, upstream_order_id=REMOTE_ID,
            last_in_at=self.now - timedelta(minutes=30),
        )
        outcome = self.sync(_WindowStub(default=[self.upstream_row(order_sta="待上号")]))
        self.assertEqual(outcome.recorded, 0)
        self.assertEqual(outcome.linked, 1)
        self.assertEqual(Order.objects.count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        link.refresh_from_db()
        self.assertEqual(link.last_in_at, self.now)
        self.assertIsNone(link.terminated_at)

    # ── ⑤ 重置后既有回拉 / 跟退自然接上 ─────────────────────────────────────

    def test_after_reset_the_upstream_refund_follows_the_new_order(self):
        old, _link = self.refunded_order_with_terminated_link()
        #: 第一轮：复活重录（窗内在途行）。
        self.sync(_WindowStub(default=[self.upstream_row(order_sta="待上号")]))
        new_order = Order.objects.exclude(pk=old.pk).get()
        #: 第二轮：上家退了这张单（退单视图里那一行）⇒ 跟退必须落在**新单**上。
        outcome = self.sync(
            _WindowStub(refund=[self.upstream_row(order_sta="已退单")]),
            now=self.now.replace(hour=13),
        )
        self.assertEqual(outcome.followed, 1)
        new_order.refresh_from_db()
        self.assertEqual(new_order.status, OrderStatus.REFUNDED)
        #: 旧单不被第二轮再搅一次（状态 / 时刻原样）。
        old.refresh_from_db()
        self.assertEqual(old.status, OrderStatus.REFUNDED)
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(
                kind="upstream.status_in", target_ref=new_order.order_no,
            ).exists(),
        )

    # ── ⑦ 同绑定同上家单号仍只有一条链路（约束钉）───────────────────────────

    def test_second_round_does_not_create_a_third_order_or_second_link(self):
        old, _link = self.refunded_order_with_terminated_link()
        row = self.upstream_row(order_sta="待上号")
        self.sync(_WindowStub(default=[row]))
        link_pk = JidanpuUpstreamOrderLink.objects.get().pk
        #: 第二轮（同一张在途行）：链路已重置 ⇒ 走既有回拉支，⛔ 不再重录。
        outcome = self.sync(
            _WindowStub(default=[row]), now=self.now.replace(hour=13),
        )
        self.assertEqual(outcome.recorded, 0)
        self.assertEqual(outcome.linked, 1)
        self.assertEqual(Order.objects.count(), 2)
        self.assertEqual(JidanpuUpstreamOrderLink.objects.count(), 1)
        self.assertEqual(JidanpuUpstreamOrderLink.objects.get().pk, link_pk)
        #: 重录台账仍恰一条（⛔ 不每轮一条）。
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind="upstream.reassign").count(), 1,
        )


class TerminatedRecheckTests(_Base, TenantTestCase):
    """⑥ 窗口洞：只有 `key1` 单查看得到的复活 ⇒ 兜底重核收口（b 选型）。"""

    def _hole_stub(self, row):
        """①②行集**为空**、只有 `key1` 单查看得到那一行的桩（生产卡死两单的形状：
        复活发生在 24h 窗外 ⇒ 窗口拉取一回空、退单视图也一回空）。"""
        return _WindowStub(by_key={REMOTE_ID: row})

    def test_sync_cannot_see_a_resurrection_outside_the_window(self):
        """洞的另一半先钉死：窗外复活，`sync_my_orders` **看不到**（零写入、零建单）。"""
        old, link = self.refunded_order_with_terminated_link()
        stub = self._hole_stub(self.upstream_row(order_sta="代练中"))
        outcome = self.sync(stub)
        self.assertEqual(outcome.recorded, 0)
        self.assertEqual(Order.objects.count(), 1)
        link.refresh_from_db()
        self.assertEqual(link.order_id, old.pk)
        self.assertIsNotNone(link.terminated_at)
        #: ①②两发之外**没有第三发**：③段根本没核它（terminated 被显式排除——洞本身）。
        self.assertEqual(len(stub.calls), 2)
        self.assertNotIn("key1", stub.calls[0][2])
        self.assertNotIn("key1", stub.calls[1][2])

    def test_recheck_covers_a_resurrection_outside_the_window(self):
        old, link = self.refunded_order_with_terminated_link()
        stub = self._hole_stub(self.upstream_row(order_sta="代练中"))
        outcome = jidanpu_upstream.recheck_terminated_links(
            binding=self.binding, client=stub, now=self.now,
        )
        self.assertEqual(outcome.reassigned, 1)
        self.assertEqual(Order.objects.count(), 2)
        new_order = Order.objects.exclude(pk=old.pk).get()
        self.assertEqual(new_order.status, OrderStatus.UNASSIGNED)
        link.refresh_from_db()
        self.assertEqual(link.order_id, new_order.pk)
        self.assertIsNone(link.terminated_at)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind="upstream.reassign").count(), 1,
        )
        #: 单查**不带窗**（窗口洞收口的那一跳；⛔ 不是靠把窗口拉宽）。
        self.assertTrue(all("key1" in query for _m, _p, query in stub.calls))

    def test_recheck_leaves_still_terminal_and_missing_links_untouched(self):
        old, link = self.refunded_order_with_terminated_link()
        #: 仍在终态：查得到、状态不在途 ⇒ 零写入。
        stub = _WindowStub(by_key={REMOTE_ID: self.upstream_row(order_sta="已退单")})
        outcome = jidanpu_upstream.recheck_terminated_links(
            binding=self.binding, client=stub, now=self.now,
        )
        self.assertEqual((outcome.reassigned, outcome.still_terminal), (0, 1))
        #: 三路都读不到（by_key 空）⇒ 同样零写入（⛔ 不判「远端没有这张单」）。
        stub_missing = _WindowStub()
        outcome_missing = jidanpu_upstream.recheck_terminated_links(
            binding=self.binding, client=stub_missing, now=self.now,
        )
        self.assertEqual((outcome_missing.reassigned, outcome_missing.missing), (0, 1))
        link.refresh_from_db()
        self.assertEqual(link.order_id, old.pk)
        self.assertIsNotNone(link.terminated_at)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(JidanpuSyncEvent.objects.count(), 0)

    def test_recheck_targeting_a_single_upstream_order_id(self):
        """`--upstream-order-id` 的定向回补口：只核那一张（生产两笔卡死单的用法）。"""
        self.refunded_order_with_terminated_link()
        other_old, other_link = self.refunded_order_with_terminated_link(
            remote_id="20261028000000000999", order_no="20261028000000000998",
        )
        stub = _WindowStub(by_key={REMOTE_ID: self.upstream_row(order_sta="待上号")})
        outcome = jidanpu_upstream.recheck_terminated_links(
            binding=self.binding, client=stub, now=self.now, upstream_order_id=REMOTE_ID,
        )
        self.assertEqual(outcome.reassigned, 1)
        self.assertEqual(Order.objects.count(), 3)
        other_link.refresh_from_db()
        self.assertEqual(other_link.order_id, other_old.pk)
        self.assertIsNotNone(other_link.terminated_at)


class RecheckCommandTests(_Base, TenantTestCase):
    """命令接线：租户遍历、`--binding` / `--upstream-order-id` 透传、未知绑定响亮报错。

    ⚠️ 行为判据在 `TerminatedRecheckTests`（直打 `recheck_terminated_links`）；本类只钉
    命令这一层的接线（桩掉 `recheck_terminated_links` 与建连，⛔ 不打真实网络）。
    """

    def setUp(self):
        super().setUp()
        self.schema = connection.schema_name

    def _run(self, *args):
        out = StringIO()
        call_command("recheck_terminated_links", "--schema", self.schema, *args, stdout=out)
        return out.getvalue()

    def test_the_command_invokes_the_shared_impl_for_the_target_binding(self):
        fake = mock.Mock(
            return_value=jidanpu_upstream.TerminatedRecheckOutcome(
                reassigned=1, still_terminal=1, missing=1, skipped=1,
                lines=("20261028…：已重录新单 X（原单 Y 留作历史）",),
            )
        )
        class _Ctx:
            def __enter__(self):
                return (object(), object())
            def __exit__(self, *exc):
                return False
        with mock.patch.object(
            jidanpu_upstream, "recheck_terminated_links", fake,
        ), mock.patch.object(jidanpu, "open_client_with_http", return_value=_Ctx()):
            stdout = self._run(
                "--binding", str(self.binding.pk), "--upstream-order-id", REMOTE_ID,
            )
        fake.assert_called_once()
        args, kwargs = fake.call_args
        #: `binding` 是**按位置**传的（命令调它时与实现签名同形），`upstream_order_id` 按关键字。
        self.assertEqual(args[0].pk, self.binding.pk)
        self.assertEqual(kwargs["upstream_order_id"], REMOTE_ID)
        self.assertIn("已重录新单", stdout)
        self.assertIn("合计重录 1", stdout)

    def test_an_unknown_binding_pk_is_a_loud_command_error(self):
        with self.assertRaises(CommandError):
            self._run("--binding", "999999")
