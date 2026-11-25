"""上家鸡蛋铺线（客服个人绑定）：**状态 / 内容回拉 + 上家退单自动跟退 + 写入面只开一格**
（计划 Task 6；spec §5.3 / §7 / §15.3 / §15.3.1 / §5.4）。

被钉住的契约（每条都**钉值**，⛔ 不是「有就行」）：

① **写入面只开一格**（spec §5.3 / §11-15b）：只有「我方 = 代练中」才允许被上游状态推进；
   我方未分配 / 待验收 / **已完成** / 已退单等一律**只登记、一个字不写**。
   ⚠️ 「我方已完成」那一档要**同时**断言 `status` **与** `completed_at` 两个值——只断状态
   抓不住「完单时间被静默清空」那一半，而边表是**整体放开**的（`COMPLETED → PENDING_REVIEW`
   是合法边）。⚠️ **别把这句读成「`completed_at` 那条断言是唯一拦截」**：它今天**独立**能拦住的
   只有「不改状态、却清空完单时间」这一类**当前产生不出来**的 bug（现存的每条清空路径都同时改
   `status`，于是 `status` 那条断言会先红），而 `completed_at` 另有几条用例也在断（`待验收` 那一档、
   已结账挡住那一档、内容回拉那一档）。保留它是因为 **spec §11-15b 要求钉两个值**，⛔ 不是因为它能单独抓住什么。
② **`DYS`（待验收）不改状态**（spec §7 改准块 1）：它是我方自己提交完成之后的**回音**。
   ⭐ 收窄（码 `DYS` + 中文 `待验收` 两个键）由**几条**用例共同守；评审实测「去掉收窄」
   （摘掉 `REMOTE_STATUS_LABELS["待验收"]`）**一次红 4 条**：`MapRemoteStatusTests` 两条
   （`test_every_row_of_the_narrowed_table` / `test_pending_review_is_never_mapped`，夹具 `token='待验收'`）
   ＋ 行为两条（代练中那一档 `test_a_pending_review_echo_never_moves_an_in_progress_order`、
   已完成那一档 `test_a_completed_order_is_never_rolled_back_by_a_remote_pending_review`）。
   ⇒ ⛔ 别写「唯一」。
③ **上家退单 ⇒ 自动跟退，绩效一起回退**：
   ① MAIN 行两列清零、行与 `user` 都留，清零前写 `MANUAL` 留痕（带真实原金额）；
   ② 未结账的 SALES 行两列先清 0，清 0 前写 `MANUAL` 留痕；链余额 `0.00`，`payable_net == 0.00`。
      纯退单不写 `FULL_REFUND` 行。
   ③ `OrderRefund` 0 行、`refund_fee` 停在原值（有部分退款就停在那个累计额上）。
   已结账的已完成挡住，这些钱不动，并留人话。
④ **只跟踪在途单**（spec §5.4 / §11-14）：两边都终态 ⇒ 写 `terminated_at`；下一轮**零上游调用**
   （判据打在桩的调用集合上，⛔ 不是「没报错」）。
⑤ **内容回拉只补我方为空的字段**（spec §5.3）：已有值⛔ **不覆盖** + 差异留一条台账；金额不回写。

⚠️ 判据打**数据库行** / **桩的调用集合**（`AGENTS.md` §5-G），⛔ 不打 logger。
⚠️ **零真实网络**：`_StubClient` 打在 SDK 边界；`client` 是本线函数的注入口。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `kefu-up-status-a`、
   密码 `pw-canary-status`、单号 `20260928000000000601` 段（一眼是合成值）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase
from jidanpu.errors import ApiError

from apps.audit.models import OperationLog
from apps.business import jidanpu_upstream, jidanpu_upstream_pics
from apps.business.models import (
    EmpType,
    Game,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    JidanpuUpstreamOrderLink,
    Order,
    OrderRefund,
    OrderScreenshot,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    SettleStatus,
)
from apps.business.settlement import settle_performances
from apps.business.settlement_amount import payable_net
from apps.core.storage import get_storage
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 端点路径**逐字**抄 spec §2.1（⛔ 不从被测常量派生）。
MY_ORDERS_PATH = "/OrderAjax/GetMyOrderList"

BASE_URL = "https://jidanpu-upstream-status.test.invalid"
PASSWORD = "pw-canary-status"

#: 台账 kind 与审计人（**字面量**：⛔ 不从被测模块派生——自指断言在常量被改坏时不会红，E-0024）。
KIND_STATUS_IN = "upstream.status_in"
KIND_CONTENT_IN = "upstream.content_in"
MACHINE_ACTOR = "上家鸡蛋铺同步"

#: 上家单号（合成；一段零，一眼是夹具）。
REMOTE_ID = "20260928000000000501"

#: 上家「我的订单」每页固定 20 行（spec §2.1 实测；⛔ 判据不写死它，这里只造夹具）。
PER_PAGE = 20

BINDING_USER = "kefu-up-status-a"
TAKER_USER = "taker-up-status-a"


class _StubClient:
    """`JidanpuClient` 的替身：按 `page` 从一份行里切页，并记录每一次调用。

    ⚠️ 必须把 `last_envelope` 写成 `code == 0`：被测代码的读端点成功码判据
    （`_require_read_success`）打的就是它（⛔ 不靠 SDK 宽松解包）。
    """

    def __init__(self, *, rows=None, per_page=PER_PAGE, account="acc-up-status"):
        self.rows = [dict(row) for row in (rows or [])]
        self.per_page = per_page
        self.account = account
        self.calls: list[tuple[str, str, dict]] = []
        self.logins: list[tuple[str, str]] = []
        self.last_envelope: dict = {}

    def ensure_login(self):
        self.logins.append((self.account, MY_ORDERS_PATH))

    def call(self, method, path, **kwargs):
        if not self.logins:
            self.ensure_login()
        self.calls.append((method, path, dict(kwargs.get("query") or {})))
        assert path == MY_ORDERS_PATH, f"未预期的端点：{method} {path}"
        page = int(kwargs["query"]["page"])
        start = (page - 1) * self.per_page
        page_rows = self.rows[start:start + self.per_page]
        self.last_envelope = {
            "code": 0, "msg": "success", "data": page_rows, "count": len(self.rows),
        }
        return [dict(row) for row in page_rows]


class _RefundViewStub:
    """按**请求里的收窄参数**回不同的行：①默认视图 / ②退单视图 / ③`key1` 单查。

    ⚠️ 为什么需要它：既有 `_StubClient` **忽略** `orderSta` ⇒ ①与②永远回同一份行、
    被并集去重 ⇒ 「上家退单**只在退单视图里**可见」这条**主用途**没有任何用例能覆盖。
    本桩让②**独有**那一行，从而钉住「②独有的退单行命中**已有链路** ⇒ 照样跟退」。
    """

    def __init__(self, *, default=(), refund=(), by_key=None):
        self.default = [dict(row) for row in default]
        self.refund = [dict(row) for row in refund]
        self.by_key = {str(k): dict(v) for k, v in (by_key or {}).items()}
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
        offset = (int(query["page"]) - 1) * PER_PAGE
        page_rows = rows[offset:offset + PER_PAGE]
        self.last_envelope = {
            "code": 0, "msg": "success", "data": page_rows, "count": len(rows),
        }
        return page_rows


# ══ ① 映射表的**本线收窄版**（spec §7 主表 + 改准块 1）═════════════════════════


class MapRemoteStatusTests(SimpleTestCase):
    """`map_remote_status`：`None` = 本线**不映射 / 不改**（待验收 / 池内形态）。"""

    #: spec §7 主表**逐档**（码 + 中文各一条）；`None` = 不映射。
    MAPPING = (
        ("DSH", "IN_PROGRESS"),
        ("待上号", "IN_PROGRESS"),
        ("DLZ", "IN_PROGRESS"),
        ("代练中", "IN_PROGRESS"),
        ("WFP", None),
        ("未分配", None),
        ("待抢单", None),
        ("DYS", None),
        ("待验收", None),
        ("YZT", "PAUSED"),
        ("已暂停", "PAUSED"),
        ("WTD", "ISSUE"),
        ("问题单", "ISSUE"),
        ("YWC", "COMPLETED"),
        ("已完成", "COMPLETED"),
        ("YTD", "REFUNDED"),
        ("已退单", "REFUNDED"),
        ("CXZ", "ISSUE"),
        ("ZCZ", "ISSUE"),
        ("YZC", "ISSUE"),
        ("YCX", "ISSUE"),
        ("YCZ", "ISSUE"),
        ("撤销中", "ISSUE"),
        ("仲裁中", "ISSUE"),
        ("已仲裁", "ISSUE"),
        ("已撤销", "ISSUE"),
        ("异常中", "ISSUE"),
    )

    def test_every_row_of_the_narrowed_table(self):
        for token, expected in self.MAPPING:
            with self.subTest(token=token):
                got = jidanpu_upstream.map_remote_status(token)
                self.assertEqual(None if got is None else str(got), expected)

    def test_completed_is_never_pulled_back_to_pending_review(self):
        """⭐ 业务方 2026-10-25：**验收是上家对我们做的事**。

        上家把这张单标成「已完成」= 他已经验收了我方的交付 ⇒ 它只能映射成我方
        「已完成」那一档（能不能写由 `should_write_status` 判），⛔ **绝不**拉回
        「待验收」（那会把我方已经做完 / 已被上家验收的单打回等待验收）。
        """
        for token in ("YWC", "已完成"):
            with self.subTest(token=token):
                self.assertEqual(
                    jidanpu_upstream.map_remote_status(token), OrderStatus.COMPLETED
                )
                self.assertNotEqual(
                    jidanpu_upstream.map_remote_status(token),
                    OrderStatus.PENDING_REVIEW,
                )
        # 就算有人把映射改坏，**已经完成 / 已经待验收**的单也不许被写回去（写入面只开代练中一格）。
        for ours in (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED):
            with self.subTest(ours=ours):
                write, _ = jidanpu_upstream.should_write_status(
                    Order(status=ours), mapped=OrderStatus.PENDING_REVIEW
                )
                self.assertFalse(write)

    def test_pending_review_is_never_mapped(self):
        """⭐ 改准块 1：`待验收` 是我方提交完成的回音 ⇒ 本线**不映射**（⛔ 不改状态）。"""
        for token in ("DYS", "待验收"):
            with self.subTest(token=token):
                self.assertIsNone(jidanpu_upstream.map_remote_status(token))

    def test_pool_forms_are_never_mapped(self):
        """spec §7 主表：池内形态（未分配 / 待抢单）**不映射**，本线由镜像行表达。"""
        for token in ("WFP", "未分配", "待抢单"):
            with self.subTest(token=token):
                self.assertIsNone(jidanpu_upstream.map_remote_status(token))

    def test_an_unknown_token_is_issue_and_never_completed(self):
        """兜底（spec §7 末行）：认不出来 ⇒ **问题单**；⛔ 绝不猜成已完成。"""
        self.assertEqual(jidanpu_upstream.map_remote_status("ZZZ"), "ISSUE")
        self.assertNotEqual(jidanpu_upstream.map_remote_status("ZZZ"), "COMPLETED")

    def test_an_empty_token_maps_to_nothing(self):
        """`orderSta` 空 / 纯空白 / `None` ⇒ 判不了 ⇒ `None`（⛔ 不落到兜底问题单）。"""
        for token in ("", "   ", None):
            with self.subTest(token=repr(token)):
                self.assertIsNone(jidanpu_upstream.map_remote_status(token))

    def test_the_refunded_row_still_maps_to_refunded(self):
        """`YTD` 仍映射成 `REFUNDED`——但调用方按 `REFUNDED_REMOTE_TOKENS` 路由到**跟退**。"""
        self.assertEqual(jidanpu_upstream.map_remote_status("YTD"), OrderStatus.REFUNDED)
        self.assertEqual(jidanpu_upstream.map_remote_status("已退单"), OrderStatus.REFUNDED)
        self.assertIn("已退单", jidanpu_upstream.REFUNDED_REMOTE_TOKENS)


# ══ ② 「写入面只开一格」的判据本身（纯函数；⛔ 不碰库）══════════════════════════


class ShouldWriteStatusTests(SimpleTestCase):
    """`should_write_status`：**只有我方 = 代练中**才真写；其余一律 `(False, 人话)`。"""

    def _order(self, status):
        return Order(status=status)

    def test_only_in_progress_may_be_advanced(self):
        order = self._order(OrderStatus.IN_PROGRESS)
        write, reason = jidanpu_upstream.should_write_status(order, mapped=OrderStatus.ISSUE)
        self.assertTrue(write)
        self.assertEqual(reason, "")
        write, _ = jidanpu_upstream.should_write_status(order, mapped=OrderStatus.PAUSED)
        self.assertTrue(write)

    def test_in_progress_cannot_be_pushed_straight_to_completed(self):
        """spec §7：入 `COMPLETED` 的边只认「待验收」⇒ 代练中报 `YWC` 这一档**写不进去**。"""
        write, reason = jidanpu_upstream.should_write_status(
            self._order(OrderStatus.IN_PROGRESS), mapped=OrderStatus.COMPLETED,
        )
        self.assertFalse(write)
        self.assertTrue(reason)

    def test_other_states_are_never_advanced(self):
        """⭐ §11-15b：未分配 / 待抢单 / 待验收 / 已完成 / 问题单 / 已暂停 / 已退单**都不许被推进**。"""
        for status in (
            OrderStatus.UNASSIGNED,
            OrderStatus.WAITING_GRAB,
            OrderStatus.PENDING_REVIEW,
            OrderStatus.COMPLETED,
            OrderStatus.ISSUE,
            OrderStatus.PAUSED,
            OrderStatus.REFUNDED,
        ):
            with self.subTest(status=status):
                write, reason = jidanpu_upstream.should_write_status(
                    self._order(status), mapped=OrderStatus.IN_PROGRESS,
                )
                self.assertFalse(write)
                self.assertTrue(reason)

    def test_a_none_mapping_is_never_written(self):
        write, reason = jidanpu_upstream.should_write_status(
            self._order(OrderStatus.IN_PROGRESS), mapped=None,
        )
        self.assertFalse(write)
        self.assertTrue(reason)

    def test_the_same_status_is_a_noop(self):
        write, reason = jidanpu_upstream.should_write_status(
            self._order(OrderStatus.IN_PROGRESS), mapped=OrderStatus.IN_PROGRESS,
        )
        self.assertFalse(write)
        self.assertIn("一致", reason)


# ══ ③ 回拉的行为（打数据库行 / 桩的调用集合）═══════════════════════════════════


class _Base:
    """夹具 + 工厂方法的**混入**；具体用例类用 `(_Base, TenantTestCase)` 组合。

    ⚠️ 刻意**不继承** `TenantTestCase`：本类里没有 `test_*`，但混入本身若带上 `TestCase`
    会被发现一次、再被每个子类各发现一次（同一批用例跑 N 遍）。
    """

    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 9, 28, 12, 0, tzinfo=dt_timezone.utc)
        #: 客服（= 绑定人）：**必须有 `order.change_status`**——它是「代练中 → 问题单 / 已暂停 /
        #: 已退单」这条边上的节点门（`order_status._CHANGE_STATUS`）。⛔ 没节点时用例会以
        #: 「被拒 + 台账」的形式绿掉，测不到真正的写入面。
        make_logged_in_client(
            username=BINDING_USER, groups=("客服",), nodes=("order.change_status",),
        )
        self.user = User.objects.get(username=BINDING_USER)
        self.taker = User.objects.create_user(username=TAKER_USER, password=PASSWORD)
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account="acc-up-status",
            password=PASSWORD, enabled=True,
        )
        self.game = Game.objects.create(name="测试游戏甲")

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def order(self, *, status=OrderStatus.IN_PROGRESS, order_no="20260928000000000601",
              total="100.00", completed_at=None, **fields):
        return Order.objects.create(
            game=self.game, order_no=order_no, status=status,
            total_price=Decimal(total), completed_at=completed_at, **fields,
        )

    def main_perf(self, order, *, temp="60.00", bonus="60.00", settled=False):
        return Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=self.taker,
            temp_amount=Decimal(temp), bonus_amount=Decimal(bonus),
            settle_status=SettleStatus.SETTLED if settled else SettleStatus.UNSETTLED,
        )

    def sales_perf(self, order, *, bonus="8.00", rate="20.00"):
        return Performance.objects.create(
            order=order, emp_type=EmpType.SALES, user=self.user,
            temp_amount=Decimal("0.00"), bonus_amount=Decimal(bonus),
            commission_rate=Decimal(rate),
        )

    def link(self, order, *, remote_id=REMOTE_ID, **kw):
        return JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=order, upstream_order_id=remote_id, **kw,
        )

    def row(self, *, order_sta="代练中", order_id=REMOTE_ID, **extra):
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

    def scripted(self, rows):
        return _StubClient(rows=rows)

    def sync(self, *rows, now=None):
        """跑一轮 `sync_my_orders`（= 调度的常规路径：**同一份行**里顺带回拉）。"""
        return jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted(list(rows)), now=now or self.now,
        )

    def pull(self, *rows, now=None, stub=None):
        """跑一轮**独立**的 `pull_statuses`；`stub` 给了就用它（要看调用集合时用）。

        ⚠️ 返回的是 `(本轮写了状态的行数, stub)`——`pull_statuses` 的返回值已经是
        `PullRoundOutcome`（`applied` + `failed`），这里只把 `applied` 拆出来，
        免得既有断言全都要改（新建用例要 `failed` 就自己拿 `PullRoundOutcome`）。
        """
        stub = stub or self.scripted(list(rows))
        outcome = jidanpu_upstream.pull_statuses(
            binding=self.binding, client=stub, now=now or self.now,
        )
        return outcome.applied, stub

    def notes(self, order, kind=KIND_STATUS_IN):
        return JidanpuSyncEvent.objects.filter(kind=kind, target_ref=order.order_no)

class PullStatusTests(_Base, TenantTestCase):
    """回拉四条口径的行为用例（判据打在**数据库行** / **桩的调用集合**上）。"""

    # ── 写入面只开一格（§5.3 / §11-7 / §11-15b）────────────────────────────

    def test_pool_orders_are_not_pushed_forward_by_the_upstream(self):
        """⭐ spec §11-7 / D-f：我方**未分配** + 上家「待上号」 ⇒ 状态一个字不改，只登记。"""
        order = self.order(status=OrderStatus.UNASSIGNED)
        self.link(order)
        outcome = self.sync(self.row(order_sta="待上号"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(outcome.followed, 0)
        note = self.notes(order).get()
        self.assertEqual(note.status, "skipped")
        self.assertIn("代练中", note.reason)

    def test_a_completed_order_is_never_rolled_back_by_a_remote_pending_review(self):
        """⭐ spec §11-8 / §7 改准块 1：完成单 + 有提交记录、上家「待验收」⇒ **不动**。"""
        order = self.order(
            status=OrderStatus.COMPLETED, completed_at=self.now - timedelta(hours=2),
        )
        self.link(order, completed_submitted_at=self.now - timedelta(hours=3))
        self.sync(self.row(order_sta="待验收"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(order.completed_at, self.now - timedelta(hours=2))
        note = self.notes(order).get()
        self.assertIn("回音", note.reason)

    def test_the_unexpected_pending_review_leaves_a_different_reason(self):
        """改准块 1 的另一半：**没有**提交记录而远端却是 `DYS` ⇒ 不改状态 + 另一句人话。"""
        order = self.order(status=OrderStatus.COMPLETED, completed_at=self.now)
        self.link(order)  #: `completed_submitted_at` 留空
        self.sync(self.row(order_sta="DYS"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        note = self.notes(order).get()
        self.assertIn("异常", note.reason)
        self.assertNotIn("回音", note.reason)

    def test_a_pending_review_echo_never_moves_an_in_progress_order(self):
        """⭐ **`DYS` 不改状态**：我方**代练中** + 上家「待验收」= 别人在对面提交过 ⇒ ⛔ 一个字不改。

        ⚠️ 去掉「`DYS` ⇒ `None`」这条收窄后，`should_write_status` 会放行
        （`IN_PROGRESS → PENDING_REVIEW` 是**合法边**）⇒ 状态被改成待验收 ⇒ 本用例红。
        ⚠️ ⛔ **但本条不是唯一会红的**：评审实测「摘掉 `待验收 ⇒ None`」一次红 **4 条**
        （另见模块 docstring ② 的清单）——⛔ 别把本用例当成去掉收窄的**单独**判据。
        """
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.link(order)
        self.sync(self.row(order_sta="待验收"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        note = self.notes(order).get()
        self.assertIn("待验收", note.reason)

    def test_a_completed_order_keeps_its_completed_at_when_the_upstream_says_issue(self):
        """⭐ spec §11-15b：断言 `status` **与** `completed_at` 两个值都不变。

        ⚠️ **别把 `completed_at` 那条断言当成唯一拦截**：它今天**独立**能拦住的只有
        「不改状态、却清空完单时间」这一类**当前产生不出来**的 bug——现存清空路径都同时改
        `status`，那样 `status` 那条会先红（且 `completed_at` 另有几条用例也在断：已完成 + `待验收`、
        已结账挡住、已完成的内容回拉）。保留它是因为 spec 要求钉两个值。
        """
        completed_at = self.now - timedelta(hours=5)
        order = self.order(status=OrderStatus.COMPLETED, completed_at=completed_at)
        self.sales_perf(order)
        self.link(order)
        self.sync(self.row(order_sta="问题单"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(order.completed_at, completed_at)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_an_issue_status_advances_an_in_progress_order_with_an_audit_row(self):
        """✅ 正面档：我方代练中 + 上家「问题单」⇒ 真写 + 一条 `order.transition` 审计。"""
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.link(order)
        self.sync(self.row(order_sta="问题单"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.ISSUE)
        log = OperationLog.objects.get(action="order.transition")
        self.assertEqual(log.from_value, "代练中")
        self.assertEqual(log.to_value, "问题单")
        self.assertEqual(log.actor, MACHINE_ACTOR)
        self.assertIsNone(log.actor_id)
        self.assertEqual(log.target_id, order.order_no)
        self.assertIn("问题单", self.notes(order).get().reason)

    def test_a_paused_status_advances_an_in_progress_order(self):
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.link(order)
        self.sync(self.row(order_sta="已暂停"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAUSED)

    def test_an_unknown_token_falls_back_to_issue_with_a_human_reason(self):
        """spec §7 末行 / §11-16：认不出来的码 ⇒ 问题单 + 人话台账（⛔ 不猜、⛔ 不改已完成）。"""
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.link(order)
        self.sync(self.row(order_sta="ZZZ"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.ISSUE)
        notes = self.notes(order)
        self.assertTrue(notes.filter(reason__contains="ZZZ").exists())

    def test_an_in_progress_order_reporting_completed_is_not_written(self):
        """spec §7 的兜底那一行：`YWC` 从我方代练中**走不进去**（入已完成只认待验收）⇒ 只登记。"""
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.link(order)
        self.sync(self.row(order_sta="已完成"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertTrue(self.notes(order).exists())

    def test_a_row_without_a_status_leaves_a_human_reason(self):
        """`orderSta` 空 ⇒ **判不了** ⇒ 只留「读不到状态」台账（⛔ 不猜一个状态写进去）。"""
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.link(order)
        self.sync(self.row(order_sta="   "))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        note = self.notes(order).get()
        self.assertIn("orderSta", note.reason)

    # ── 上家退单 ⇒ 自动跟退（A11 / §15.3 / §11-21）───────────────────────────

    def test_upstream_refund_makes_our_order_refunded_and_leaves_two_trails(self):
        """⭐ spec §11-21 / A11：① 状态 = 已退单；② 一条 `order.transition` 审计
        （from/to = 中文 label、actor = 机器名、`actor_id` 为空）；③ 台账有人话。三条缺一不可。"""
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.main_perf(order)
        self.sales_perf(order)
        self.link(order)
        outcome = self.sync(self.row(order_sta="已退单"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        self.assertEqual(outcome.followed, 1)
        log = OperationLog.objects.get(action="order.transition")
        self.assertEqual(log.from_value, "代练中")
        self.assertEqual(log.to_value, "已退单")
        self.assertEqual(log.actor, MACHINE_ACTOR)
        self.assertIsNone(log.actor_id)
        note = self.notes(order).get()
        self.assertIn("跟着退", note.reason)
        self.assertEqual(note.status, "done")
        #: spec §4.4：台账 `payload` 必须带「归属绑定」（运维要能回答「这行归哪一份绑定」）。
        self.assertEqual(note.payload["upstream_binding_id"], self.binding.pk)

    def test_a_refund_visible_only_in_the_refund_view_still_follows_the_refund(self):
        """⭐ 主用途：那一行**只在退单视图（②）里**出现、且我方**已有链路** ⇒ 照样跟退。

        ⚠️ 为什么单独立一条：§0 那两轮真机读数里，退单行**有时只在②里可见**（默认视图看不见）；
        而②的行必须与①走**同一支** `_pull_row` 才会跟退。既有用例里②独有的那一行**没有链路**
        （只走「不录 + 一条人话」），覆盖不到这条路。
        判据三条：① 我方状态变「已退单」；② 真的跟退（`followed == 1`）＋审计在；③ ③**没有**为这一张
        再打一发 `key1`（它已经在①②行集里 ⇒ 不该被当成「没露面」）。
        """
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.main_perf(order)
        self.sales_perf(order)
        self.link(order)
        stub = _RefundViewStub(
            default=[],                                     # ① 默认视图**看不见**它
            refund=[self.row(order_sta="已退单")],           # ② 退单视图才看得见
        )
        outcome = jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=stub, now=self.now,
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        self.assertEqual(outcome.followed, 1)
        self.assertTrue(OperationLog.objects.filter(action="order.transition").exists())
        self.assertIn("跟着退", self.notes(order).get().reason)
        #: ③ 只为「①②都没露面」的链路打 `key1` ⇒ 这一张**不该**出现在单查里。
        self.assertEqual([c[2].get("key1") for c in stub.calls if c[2].get("key1")], [])

    def test_the_refund_notes_never_carry_credentials(self):
        """计划 Task 6 Step 3：`order.transition` 的 `reason` 与人话台账里⛔ 不含凭据（spec §8.3）。

        ⚠️ 判据是**搜字符串**（⛔ 不是「看起来没有」）：密码与上家账号名逐字不出现。
        """
        order = self.order(status=OrderStatus.IN_PROGRESS, total="100.00")
        self.main_perf(order)
        self.sales_perf(order)
        self.link(order)
        self.sync(self.row(order_sta="已退单"))
        log = OperationLog.objects.get(action="order.transition")
        note = self.notes(order).get()
        self.assertEqual(log.reason, jidanpu_upstream.FOLLOW_REFUND_REASON)
        for text in (log.reason, log.summary, note.reason):
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn(self.binding.account, text)

    def test_the_follow_refund_ledger_names_the_upstream_account(self):
        """⭐ spec §15.3 第 ④ 步 (b)：跟退台账的人话要**点名「上家账号显示名」**
        （`JidanpuUpstreamBinding.account_name`），⛔ 不是登录账号。

        ⚠️ 显示名**空白时**不回落登录账号（那一列是凭据面）——照审计那一句（见
        `test_the_refund_notes_never_carry_credentials` 的夹具：`account_name=""`、`account` 逐字禁出现）。
        ⚠️ 显示名只进**台账**；审计行仍用共用的 `FOLLOW_REFUND_REASON`（spec 第 ④ 步 (a) 没要求它）。
        """
        self.binding.account_name = "上家客服乙"
        self.binding.save(update_fields=["account_name"])
        order = self.order(status=OrderStatus.IN_PROGRESS, total="100.00")
        self.main_perf(order)
        self.sales_perf(order)
        self.link(order)
        self.sync(self.row(order_sta="已退单"))

        note = self.notes(order).get()
        self.assertIn("上家客服乙", note.reason)
        self.assertIn("跟着退", note.reason)
        #: 登录账号（凭据面）仍然一个字都不出现。
        self.assertNotIn(self.binding.account, note.reason)
        log = OperationLog.objects.get(action="order.transition")
        self.assertEqual(log.reason, jidanpu_upstream.FOLLOW_REFUND_REASON)
        self.assertNotIn("上家客服乙", log.reason)

    def test_the_refund_takes_the_money_back_along_with_the_status(self):
        """⭐ spec §11-21c / §15.3.1（**T17 已定：钱一起回退**）——退回来的那几笔**逐条钉住**。

        新口径（`D-0187` 的 Q1/Q2 + `D-0181`）下「钱一起回退」由**五笔**表达，逐条钉住：
        ① 退款面**一行都没有**、`OrderRefund.count() == 0`；② `refund_fee` 停在 `0.00`（⛔ 没被写回毛收款）；
        ③ MAIN 行两列清零、**行与 `user` 都留** + 清零前的 `MANUAL` 留痕（带真实原金额 `60.00`）；
        ④ SALES 行两列清零、`MANUAL` 留痕带真实原生效 `8.00`，且**一条 `FULL_REFUND` 都不写**
        （纯退单 ⇒ 链空 ⇒ 第 3 步 `deduction == 0`），链余额 `0.00`；
        ⑤ `payable_net == 0.00`（钱真的回到 0、且**不变负**，`/settle` 不会触发 `1011`）。

        夹具：在途单 `total_price=100.00`、MAIN `temp/bonus=60.00`、SALES `bonus=8.00 / rate=20%`。
        夹具的 SALES `bonus` **刻意非 0**（`8.00`）——删掉清 0 那一步 ⇒ ④ 的两列与 ⑤ 的应付当红。
        """
        order = self.order(status=OrderStatus.IN_PROGRESS, total="100.00")
        main = self.main_perf(order, temp="60.00", bonus="60.00")
        sales = self.sales_perf(order, bonus="8.00", rate="20.00")
        self.link(order)
        self.sync(self.row(order_sta="已退单"))

        # ⭐ D-0181：①/② 两笔「退款面」的钱（`OrderRefund(FULL)` 行 + `refund_fee`
        #    置成毛收款）**已整体删除** ⇒ 今天是「一行都没有、一列都不动」。
        #    ⛔ 绩效侧那两笔（下面 ③/④）一字未改。
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        # ③ MAIN 行两列清零、**行与 `user` 都留**，且有清零前的 `MANUAL` 留痕（带真实原金额）。
        main.refresh_from_db()
        self.assertEqual(main.temp_amount, Decimal("0.00"))
        self.assertEqual(main.bonus_amount, Decimal("0.00"))
        self.assertEqual(main.user_id, self.taker.pk)
        trace = PerformanceAdjustment.objects.get(
            performance=main, kind=PerformanceAdjustment.Kind.MANUAL,
        )
        self.assertEqual(trace.amount, Decimal("0.00"))
        self.assertIn("60.00", trace.reason)
        # ④ SALES 行两列清零、**行与 `user` 都留**，且有清 0 前的 `MANUAL` 留痕（带真实原生效）。
                #    ⭐ D-0187：这条留痕就是「钱一起回退」在新口径下的**证据行**——链上那条
        #    `−8.00` 已不再产生（见 docstring 的更正块），原值改由留痕承载。
        sales.refresh_from_db()
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(sales.user_id, self.user.pk)
        sales_trace = PerformanceAdjustment.objects.get(
            performance=sales, kind=PerformanceAdjustment.Kind.MANUAL,
        )
        self.assertEqual(sales_trace.amount, Decimal("0.00"))      # ⛔ 非 0 会改 payable 的 Σ
        self.assertIsNone(sales_trace.refund_id)
        self.assertEqual(sales_trace.settle_status, SettleStatus.UNSETTLED)
        self.assertIn("原生效 8.00", sales_trace.reason)
        #    ⛔ 纯退单**一条 `FULL_REFUND` 都不写**（旧口径下这里恰好一条 `−8.00`）。
        self.assertEqual(
            PerformanceAdjustment.objects.filter(
                performance=sales,
                kind=PerformanceAdjustment.Kind.FULL_REFUND,
            ).count(),
            0,
        )
        # ⑤ 链余额 `0.00` + `bonus + Σ全部调整 == 0`（D-0181 的目标式不变量）+ 应付 `0.00`。
        chain = sum(
            (
                row.amount
                for row in PerformanceAdjustment.objects.filter(
                    performance=sales,
                    kind__in=(
                        PerformanceAdjustment.Kind.PARTIAL_REFUND,
                        PerformanceAdjustment.Kind.FULL_REFUND,
                    ),
                )
            ),
            Decimal("0.00"),
        )
        self.assertEqual(chain, Decimal("0.00"))
        self.assertEqual(
            sales.bonus_amount
            + sum(
                (
                    row.amount
                    for row in PerformanceAdjustment.objects.filter(performance=sales)
                ),
                Decimal("0.00"),
            ),
            Decimal("0.00"),
        )
        net = payable_net(
            sales,
            PerformanceAdjustment.objects.filter(
                performance=sales, settle_status=SettleStatus.UNSETTLED
            ),
        )
        self.assertEqual(net, Decimal("0.00"))
        self.assertGreaterEqual(net, Decimal("0.00"))     # ⛔ 变负会让 /settle 触发 1011

    def test_follow_refund_adds_no_order_refund_row(self):
        """① **改准（D-0181）**：跟退**不再**写 `OrderRefund(kind=FULL)` 行（恰好 **0** 行）。

        ⚠️ 旧断言是「恰好一行、`amount == 毛收款 − 旧 refund_fee`」——那半条口径由业务方
        2026-10-03 推翻（spec §G2 / §G5-3，含上家跟退）；本用例**翻面**成它的哨兵。
        """
        order = self.order(status=OrderStatus.IN_PROGRESS, total="100.00")
        self.main_perf(order)
        self.sales_perf(order)
        self.link(order)
        self.sync(self.row(order_sta="YTD"))
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))

    def test_follow_refund_writes_no_refund_row_even_with_a_prior_partial(self):
        """⭐ **改准（D-0181，2026-10-03）**：跟退**不碰退款面**——即使单上**已有**
        `refund_fee = 30.00`（先前部分退款留下的），跟退之后它仍停在 `30.00`，
        且不新增任何 `OrderRefund` 行。

        跟退不写事件行、也不改 `refund_fee`。非零累计额是部分退款留下的，跟退不许动它。
        夹具必须带非零旧值：0 与 0 恒等，看不出「没动」。
        """
        order = self.order(
            status=OrderStatus.IN_PROGRESS, total="100.00", refund_fee=Decimal("30.00"),
        )
        OrderRefund.objects.create(
            order=order, kind=OrderRefund.Kind.PARTIAL, amount=Decimal("30.00"),
            reason="先前部分退款", request_id="prior-partial-up-status-1",
        )
        self.main_perf(order)
        self.sales_perf(order)
        self.link(order)
        self.sync(self.row(order_sta="已退单"))

        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("30.00"))          # ⛔ 不动
        refunds = list(OrderRefund.objects.filter(order=order).order_by("id"))
        self.assertEqual(
            [(row.kind, row.amount) for row in refunds],
            [("PARTIAL", Decimal("30.00"))],
        )                                                             # ⛔ 不新增行
        # ⚠️ **不变式本身仍然成立**（`refund_fee = Σ OrderRefund.amount`：跟退这条路径
        #    既不写行、也不改列）；今天能拿它举例 / 演练的只剩「部分退款 + 回池冲销」那一半
        #    （这里数到的就是那条 `PARTIAL`）。
        self.assertEqual(
            sum(row.amount for row in refunds), order.refund_fee
        )

    def test_follow_refund_leaves_the_refund_fee_alone(self):
        """② **改准（D-0181）**：跟退**不写** `order.refund_fee`（旧断言是「设置为毛收款」）。"""
        order = self.order(status=OrderStatus.IN_PROGRESS, total="100.00")
        self.main_perf(order)
        self.sales_perf(order)
        self.link(order)
        self.sync(self.row(order_sta="YTD"))
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))

    def test_follow_refund_clears_the_main_row_and_keeps_the_row_and_its_user(self):
        """③ 钱的那一行：MAIN 两列清零、**行与 `user` 都留** + 清零前的 `MANUAL` 留痕。"""
        order = self.order(status=OrderStatus.IN_PROGRESS, total="100.00")
        main = self.main_perf(order, temp="60.00", bonus="60.00")
        self.link(order)
        self.sync(self.row(order_sta="YTD"))
        main.refresh_from_db()
        self.assertEqual(main.temp_amount, Decimal("0.00"))
        self.assertEqual(main.bonus_amount, Decimal("0.00"))
        self.assertEqual(main.user_id, self.taker.pk)
        self.assertEqual(Performance.objects.filter(order=order, emp_type=EmpType.MAIN).count(), 1)
        trace = PerformanceAdjustment.objects.get(
            performance=main, kind=PerformanceAdjustment.Kind.MANUAL,
        )
        self.assertEqual(trace.amount, Decimal("0.00"))
        self.assertIn(self.taker.username, trace.reason)
        self.assertIn("60.00", trace.reason)

    def test_follow_refund_takes_the_sales_row_to_zero_without_a_full_refund_row(self):
        """④ 钱的那一行（新口径）：SALES 行两列清 0 + 链余额 0 + 应付 `0.00`，且**不写**
        `FULL_REFUND`（⛔ 不是 `−amount × rate / 100 = −20.00` 的产物）。

        夹具：在途单 `total_price=100.00`、无接单人、SALES `bonus=8.00 / rate=20%`。

        未结账的 SALES 行先被清 0，纯退单不写 `FULL_REFUND`。
        误用 `−amount × rate / 100` 会多出一条 `−20.00` 的调整行、把 `payable_net` 打成负数，
        下面三条当场红。夹具的 SALES `bonus` 刻意非 0：删掉清 0 那一步，`bonus` 与应付都停在 `8.00`。
        """
        order = self.order(status=OrderStatus.IN_PROGRESS, total="100.00")
        self.main_perf(order)
        sales = self.sales_perf(order, bonus="8.00", rate="20.00")
        self.link(order)
        self.sync(self.row(order_sta="YTD"))
        sales.refresh_from_db()
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        # ⛔ 旧形状（恰好一条 `−8.00` 的 `FULL_REFUND`）不存在了。
        self.assertEqual(
            PerformanceAdjustment.objects.filter(
                performance=sales,
                kind=PerformanceAdjustment.Kind.FULL_REFUND,
            ).count(),
            0,
        )
        # ⛔ 也更不是 `−amount × rate / 100 = −20.00` 的产物。
        self.assertFalse(
            PerformanceAdjustment.objects.filter(
                performance=sales, amount=Decimal("-20.00")
            ).exists()
        )
        chain = sum(
            (
                row.amount
                for row in PerformanceAdjustment.objects.filter(
                    performance=sales,
                    kind__in=(
                        PerformanceAdjustment.Kind.PARTIAL_REFUND,
                        PerformanceAdjustment.Kind.FULL_REFUND,
                    ),
                )
            ),
            Decimal("0.00"),
        )
        self.assertEqual(chain, Decimal("0.00"))
        # D-0181 的目标式不变量（新口径下照旧成立：`bonus` 与链同时为 0）。
        self.assertEqual(
            sales.bonus_amount
            + sum(
                (
                    row.amount
                    for row in PerformanceAdjustment.objects.filter(performance=sales)
                ),
                Decimal("0.00"),
            ),
            Decimal("0.00"),
        )
        net = payable_net(
            sales,
            PerformanceAdjustment.objects.filter(
                performance=sales, settle_status=SettleStatus.UNSETTLED
            ),
        )
        self.assertEqual(net, Decimal("0.00"))
        self.assertGreaterEqual(net, Decimal("0.00"))     # ⛔ 变负会让 /settle 触发 1011

    def test_a_completed_but_unsettled_order_is_refunded_too(self):
        """D-0156 的另一半（spec §15.3.1 三档表）：**未结账**的已完成**照退**。"""
        order = self.order(
            status=OrderStatus.COMPLETED, total="100.00", completed_at=self.now - timedelta(hours=1),
        )
        self.main_perf(order, settled=False)
        self.sales_perf(order)
        self.link(order)
        outcome = self.sync(self.row(order_sta="已退单"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        self.assertEqual(outcome.followed, 1)
        # ⭐ D-0181：跟退不再建 `OrderRefund` 事件行。
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)

    def test_a_second_round_of_the_same_refund_is_idempotent(self):
        """跟退幂等：第二轮（我方已是已退单）⇒ ⛔ 不再退一次、不再记 `OrderRefund`、不再灌台账。

        ⚠️ 第二轮的「幂等」由**扫描面**表达（`PULL_EXCLUDED_STATUSES` 把已退单排除在外）；
        直接调 `follow_refund` 的那条幂等出口另有 `FollowRefundDirectTests` 钉住。
        """
        order = self.order(status=OrderStatus.IN_PROGRESS, total="100.00")
        self.main_perf(order)
        self.sales_perf(order)
        self.link(order)
        self.sync(self.row(order_sta="已退单"))
        events_before = JidanpuSyncEvent.objects.count()
        outcome = self.sync(self.row(order_sta="已退单"), now=self.now + timedelta(minutes=5))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        # ⭐ D-0181：跟退不再建 `OrderRefund` 事件行。
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)
        self.assertEqual(outcome.followed, 0)
        self.assertEqual(JidanpuSyncEvent.objects.count(), events_before)

    # ── 已结账挡住（§11-21b / T19；四笔钱一个都没动）────────────────────────

    def test_a_settled_completed_order_is_not_auto_refunded(self):
        """⭐ spec §11-21b（**✅ T19 已定**）：已完成 **且已结账** + 上家 `YTD` ⇒ **挡住**：
        状态不变 + **四笔钱一个都没动** + 人话台账 / 提醒人工（⛔ 不是静默）。"""
        completed_at = self.now - timedelta(hours=4)
        order = self.order(status=OrderStatus.COMPLETED, total="100.00", completed_at=completed_at)
        main = self.main_perf(order, temp="60.00", bonus="60.00", settled=True)
        sales = self.sales_perf(order, bonus="8.00", rate="20.00")
        link = self.link(order)
        outcome = self.sync(self.row(order_sta="已退单"))

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(order.completed_at, completed_at)
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        self.assertEqual(outcome.followed, 0)
        #: 四笔钱：① 没有退款事件行 ② `refund_fee` 没动 ③ MAIN 两列没动 ④ SALES 链上一条调整都没有。
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)
        main.refresh_from_db()
        self.assertEqual(main.temp_amount, Decimal("60.00"))
        self.assertEqual(main.bonus_amount, Decimal("60.00"))
        self.assertEqual(main.settle_status, SettleStatus.SETTLED)
        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("8.00"))
        self.assertEqual(PerformanceAdjustment.objects.filter(performance__order=order).count(), 0)
        #: 人话留痕 + 提醒人工（⛔ 不是静默）。
        note = self.notes(order).get()
        self.assertIn("已结账", note.reason)
        self.assertIn("人工", note.reason)
        self.assertIn("挡住", note.reason)
        #: ⭐ **写入面被挡住那一档也要独立断一次**：挡住的两层是
        #: ① 状态 / 钱一个字不动（上面四笔 + status/completed_at）② **审计行一行都不许有**——
        #: 少了本句，「悄悄写一条假的 `order.transition` 审计却把状态写成原样」看不出来
        #: （写入面被挡那一档 `test_a_completed_order_keeps_its_completed_at_…` 有同一条）。
        self.assertEqual(OperationLog.objects.count(), 0)
        #: 残差登记（spec §13.1-R21）：真要「已结账也回退」是**另开一条线**的事，台账里要说清。
        self.assertIn("另开一条线", note.reason)
        self.assertEqual(note.payload["upstream_binding_id"], self.binding.pk)
        #: 两边都终态（我方已完成 + 上家已退单）⇒ 停止跟踪（spec §5.4）。
        link.refresh_from_db()
        self.assertEqual(link.terminated_at, self.now)

    # ── 只跟踪在途单（§5.4 / §11-14）────────────────────────────────────────

    def test_terminal_rows_stop_being_polled(self):
        """⭐ spec §11-14 / A7：两边都终态 ⇒ 写 `terminated_at`；下一轮**零上游调用**。"""
        order = self.order(status=OrderStatus.COMPLETED, completed_at=self.now)
        self.link(order, completed_submitted_at=self.now)
        self.sync(self.row(order_sta="待验收"))
        link = JidanpuUpstreamOrderLink.objects.get()
        self.assertEqual(link.terminated_at, self.now)
        #: 下一轮：独立回拉的入口**一次上游都不打**（判据打在桩的调用集合上）。
        applied, stub = self.pull(self.row(order_sta="待验收"))
        self.assertEqual(applied, 0)
        self.assertEqual(stub.calls, [])

    def test_a_refunded_order_is_not_scanned(self):
        """`PULL_EXCLUDED_STATUSES`（已退单）⇒ 连上游都不查（返回 0、桩零调用）。"""
        order = self.order(status=OrderStatus.REFUNDED)
        self.link(order)
        applied, stub = self.pull(self.row(order_sta="待验收"))
        self.assertEqual(applied, 0)
        self.assertEqual(stub.calls, [])

    def test_a_followed_refund_enters_the_untracked_face(self):
        """计划 Task 6 Step 4 ②：**跟退之后**那一单按 §5.4 停跟踪 ⇒ 下一轮零上游调用。"""
        order = self.order(status=OrderStatus.IN_PROGRESS, total="100.00")
        self.main_perf(order)
        self.sales_perf(order)
        self.link(order)
        self.sync(self.row(order_sta="已退单"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        applied, stub = self.pull(self.row(order_sta="已退单"))
        self.assertEqual(applied, 0)
        self.assertEqual(stub.calls, [])

    def test_settlement_is_not_gated_by_the_upstream_pending_review(self):
        """⭐ spec §11-26 / A10（**不回归**）：上家停在「待验收」⛔ **不是**我方结账的前提。

        调**既有**结账服务 `settle_performances`，断言**成功**（⛔ 不是「没报错」）：我方那张
        已完成的单照样结得掉（`order__status=COMPLETED` 这一条是结账自己的前提）。
        ⚠️ **改准（评审，实测）**：本条⛔ **不是**「`DYS` 不改状态」的交叉判据
        （原句写「若 `DYS` 被写回成待验收 ⇒ 本用例红」，那是**错的**）：我方是**已完成**，
        `should_write_status` 的 ②（只有代练中那一格）**先**把它挡住 ⇒ 摘掉 `DYS` 收窄后本用例
        **仍然绿**（实测：摘 `待验收 ⇒ None` 时红的 4 条里没有它）。真正的收窄判据见模块 docstring ②。
        """
        grant_games(self.user, self.game)
        order = self.order(
            status=OrderStatus.COMPLETED, total="100.00", completed_at=self.now,
        )
        main = self.main_perf(order, temp="60.00", bonus="60.00")
        self.link(order, completed_submitted_at=self.now)
        self.sync(self.row(order_sta="待验收"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        result = settle_performances([main.pk], actor="财务甲", user=self.user)
        self.assertIn(main.pk, result.settled_performances)
        main.refresh_from_db()
        self.assertEqual(main.settle_status, SettleStatus.SETTLED)
        self.assertTrue(main.is_settled)

    def test_a_soft_deleted_order_is_not_scanned(self):
        """我方已软删的单 ⇒ 不进扫描面（远端没有它的立足点，改它只会响亮地失败）。"""
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.link(order)
        Order.objects.filter(pk=order.pk).update(deleted_at=self.now)
        applied, stub = self.pull(self.row(order_sta="问题单"))
        self.assertEqual(applied, 0)
        self.assertEqual(stub.calls, [])

    def test_a_disabled_binding_does_not_touch_the_upstream(self):
        """spec §8.4：停用的绑定 ⇒ 一次上游都不打。"""
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.link(order)
        self.binding.enabled = False
        self.binding.save(update_fields=["enabled"])
        applied, stub = self.pull(self.row(order_sta="问题单"))
        self.assertEqual(applied, 0)
        self.assertEqual(stub.calls, [])

    def test_a_disabled_binding_syncs_nothing_at_all(self):
        """⭐ spec §8.4：停用的绑定跑**调度那一轮的常规路径**（`sync_my_orders`）也 ⇒
        **一次上游都不打**、⛔ 一张单都不建、⛔ 台账一行都不写，返回**全零**的 `MyOrdersOutcome`。

        ⚠️ 本函数是**公开入口**（计划 Task 13 会调）——⛔ 别把停用闸门只留在 `pull_statuses` 那一侧。
        修复前评审实测：停用 + 本路径 ⇒ **上游被调 1 次**、还会新建 **1 张未分配订单**（`recorded=1`），
        与 `pull_statuses` 的 `if not binding.enabled: return 0` 形成**不对称的踩坑面**。
        判据打在**桩的调用集合**与**订单 / 台账行数**上（§5-G：⛔ 不是「没报错」）。
        夹具那一行是**在途**（`待上号`）且游戏对得上 ⇒ 少了闸门它会**真的建单**（这就是它能红的原因）。
        """
        self.binding.enabled = False
        self.binding.save(update_fields=["enabled"])
        stub = self.scripted([self.row(order_sta="待上号")])
        outcome = jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=stub, now=self.now,
        )
        self.assertEqual(stub.calls, [])
        self.assertEqual(
            (outcome.pulled, outcome.recorded, outcome.linked, outcome.skipped, outcome.followed),
            (0, 0, 0, 0, 0),
        )
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(JidanpuSyncEvent.objects.count(), 0)

    def test_a_linked_row_missing_from_the_round_reads_as_unreadable(self):
        """spec §5.3 末行：窗口①②里没有这一行、**按 `key1` 补核也读不到** ⇒ 一律当「读不到」，
        ⛔ 不判远端没有这张单。

        ⚠️ **改准（窗口拉取）**：一轮的请求形状 = ① 窗口视图 + ② 同窗口「已退单」视图
        + ③ 这一张单的 `key1` 单查；而且**只有③那几发不带 `stime`/`etime`**
        （⛔ 旧用例断的「整表 1 发」正是被消灭的形状）。
        ⚠️ **再改准（退单兜底）**：③ 现在是**两问**——默认视图问不到，就再问一次
        「`key1` + `orderSta=已退单`」（⛔ 不带窗）⇒ 一轮共 **4 发**。
        """
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.link(order)
        applied, stub = self.pull()  # 空列表
        order.refresh_from_db()
        self.assertEqual(applied, 0)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(len(stub.calls), 4)
        self.assertEqual(
            [query.get("key1") for _m, _p, query in stub.calls],
            [None, None, REMOTE_ID, REMOTE_ID],
        )
        self.assertEqual(stub.calls[-1][2].get("orderSta"), "已退单")
        self.assertNotIn("stime", stub.calls[-1][2])
        self.assertIn("读不到", self.notes(order).get().reason)

    def test_pull_statuses_counts_the_applied_status_writes(self):
        """`pull_statuses` 的返回值 = 本轮**写了状态**的行数（跟退也算写了状态）。"""
        first = self.order(status=OrderStatus.IN_PROGRESS, order_no="20260928000000000611")
        second = self.order(status=OrderStatus.IN_PROGRESS, order_no="20260928000000000612")
        self.link(first, remote_id="20260928000000000511")
        self.link(second, remote_id="20260928000000000512")
        applied, _stub = self.pull(
            self.row(order_sta="问题单", order_id="20260928000000000511"),
            self.row(order_sta="已暂停", order_id="20260928000000000512"),
        )
        self.assertEqual(applied, 2)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, OrderStatus.ISSUE)
        self.assertEqual(second.status, OrderStatus.PAUSED)

    # ── 内容回拉：只补我方为空的字段（§5.3）────────────────────────────────

    def test_content_pull_only_fills_blank_fields(self):
        """⭐ 内容回拉**只补我方为空的字段**；上游值与我方不同但未覆盖 ⇒ 留一条台账。"""
        order = self.order(
            status=OrderStatus.IN_PROGRESS,
            project_text="", content="", game_role_name="", game_account="", game_sub_account="",
            remark="我方原备注",
        )
        self.link(order)
        self.sync(self.row(
            order_sta="代练中",
            project="上家项目", orderContent="上家需求", orderRemark="上家备注",
            roleName="上家角色", gameAcc="上家账号", zgameAcc="上家子号",
        ))
        order.refresh_from_db()
        self.assertEqual(order.project_text, "上家项目")
        self.assertEqual(order.content, "上家需求")
        self.assertEqual(order.game_role_name, "上家角色")
        self.assertEqual(order.game_account, "上家账号")
        self.assertEqual(order.game_sub_account, "上家子号")
        #: ⛔ 我方已有值的那一格**不覆盖**（我方人工改过的值优先）。
        self.assertEqual(order.remark, "我方原备注")
        filled = JidanpuSyncEvent.objects.get(kind=KIND_CONTENT_IN, status="done")
        self.assertIn("project_text", filled.reason)
        kept = JidanpuSyncEvent.objects.get(kind=KIND_CONTENT_IN, status="skipped")
        self.assertIn("remark", kept.reason)
        #: 内容回拉⛔ 不碰金额（钱只在我方建单那一次落）。
        self.assertEqual(order.total_price, Decimal("100.00"))
        self.assertIsNone(order.hall_price)

    def test_content_pull_leaves_no_note_when_everything_matches(self):
        """两边逐字一致 ⇒ 一条台账都不写（⛔ 不每轮灌噪声）。"""
        order = self.order(
            status=OrderStatus.IN_PROGRESS, project_text="合成项目", content="合成需求",
            remark="合成备注", game_role_name="合成角色", game_account="acc-synth",
            game_sub_account="sub-synth",
        )
        self.link(order)
        self.sync(self.row(order_sta="代练中"))
        self.assertEqual(JidanpuSyncEvent.objects.filter(kind=KIND_CONTENT_IN).count(), 0)

    def test_content_pull_reaches_a_completed_order(self):
        """spec §5.3：我方**已完成** ⇒ 回拉只补内容 + 记台账 + 判终态（⛔ 不改状态）。"""
        order = self.order(
            status=OrderStatus.COMPLETED, completed_at=self.now,
            project_text="", content="", game_role_name="", game_account="", game_sub_account="",
            remark="",
        )
        self.link(order, completed_submitted_at=self.now)
        self.sync(self.row(order_sta="已完成", project="补齐的项目"))
        order.refresh_from_db()
        self.assertEqual(order.project_text, "补齐的项目")
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(order.completed_at, self.now)


class PullObservationOrderingTests(_Base, TenantTestCase):
    def test_a_later_per_order_probe_uses_its_own_start_time(self):
        order = self.order()
        link = self.link(order)
        confirm_at = self.now + timedelta(seconds=2)
        probe_at = self.now + timedelta(seconds=4)
        stub = _Key1Stub(by_key={REMOTE_ID: self.row(order_sta="已退单")})
        read = stub.call

        def confirm_during_list_read(*args, **kwargs):
            rows = read(*args, **kwargs)
            if len(stub.calls) == 1:
                self.sync(self.row(), now=confirm_at)
            return rows

        with mock.patch.object(stub, "call", side_effect=confirm_during_list_read):
            result = jidanpu_upstream.pull_statuses(
                binding=self.binding, client=stub, now=self.now, probe_clock=lambda: probe_at,
            )

        self.assertEqual(result.applied, 1)
        link.refresh_from_db()
        self.assertEqual(link.order.status, OrderStatus.REFUNDED)
        self.assertEqual(link.terminated_at, probe_at)

    def test_older_refund_cannot_overwrite_a_newer_active_read(self):
        order = self.order(remark="")
        link = self.link(order)
        main = self.main_perf(order)
        newer_at = self.now + timedelta(seconds=2)
        old = self.scripted([self.row(order_sta="已退单", orderRemark="旧备注")])
        read = old.call

        def with_newer_read(*args, **kwargs):
            rows = read(*args, **kwargs)
            if len(old.calls) == 1:
                self.sync(self.row(orderRemark=""), now=newer_at)
            return rows

        with mock.patch.object(old, "call", side_effect=with_newer_read):
            applied, _ = self.pull(stub=old)

        order.refresh_from_db()
        link.refresh_from_db()
        main.refresh_from_db()
        self.assertEqual(applied, 0)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(order.remark, "")
        self.assertEqual(main.bonus_amount, Decimal("60.00"))
        self.assertEqual(link.last_in_at, newer_at)

    def test_older_missing_row_cannot_restart_the_streak_after_a_newer_hit(self):
        order = self.order()
        link = self.link(order)
        for minutes in (0, 3):
            self.pull(stub=_Key1Stub(), now=self.now + timedelta(minutes=minutes))
        link.refresh_from_db()
        self.assertEqual(link.miss_count, 2)
        old = _Key1Stub()
        read = old.call
        older_at = self.now + timedelta(minutes=6)
        newer_at = older_at + timedelta(seconds=2)

        def with_newer_read(*args, **kwargs):
            rows = read(*args, **kwargs)
            query = kwargs["query"]
            if query.get("key1") == REMOTE_ID and query.get("orderSta"):
                self.sync(self.row(), now=newer_at)
            return rows

        with mock.patch.object(old, "call", side_effect=with_newer_read):
            self.pull(stub=old, now=older_at)

        order.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(link.miss_count, 0)
        self.assertEqual(link.last_in_at, newer_at)

    def test_older_active_row_cannot_reassign_an_order_closed_by_newer_reads(self):
        order = self.order()
        link = self.link(order)
        old = self.scripted([self.row()])
        read = old.call
        closed_at = self.now + timedelta(minutes=7)

        def with_newer_reads(*args, **kwargs):
            rows = read(*args, **kwargs)
            if len(old.calls) == 1:
                for minutes in (1, 4, 7):
                    self.sync(now=self.now + timedelta(minutes=minutes))
            return rows

        with mock.patch.object(old, "call", side_effect=with_newer_reads):
            outcome = jidanpu_upstream.sync_my_orders(
                binding=self.binding, client=old, now=self.now,
            )

        link.refresh_from_db()
        self.assertEqual(outcome.recorded, 0)
        self.assertEqual(link.order_id, order.pk)
        self.assertEqual(link.order.status, OrderStatus.REFUNDED)
        self.assertEqual(link.terminated_at, closed_at)


class _Key1Stub:
    """只按 `key1` **逐字相等**回那一行；不带 `key1` 的那两发（窗口①②）一律回空集。

    ⚠️ 回 `count=0` 让 `fetch_my_orders` 在第一页就收尾（⛔ 不会翻空页）。
    ⚠️ 逐字记下每一次的 `query`：③ 的**轮转 / 上限**判据就打在「这一轮问了哪几个 key1」上。
    ⭐ `by_key_refund`：**带 `orderSta` 的那一发**（退单兜底，2026-10-05 受控实验修）单独一张表
    —— 真机实测「默认视图按单号」对一张已退单的单回 0 行，而「单号 + `orderSta=已退单`」才命中
    ⇒ 两张表必须能分开配，否则「兜底这一发到底发没发、发了什么」检不出来。
    """

    def __init__(self, *, by_key=None, by_key_refund=None):
        self.by_key = {str(key): dict(row) for key, row in (by_key or {}).items()}
        self.by_key_refund = {
            str(key): dict(row) for key, row in (by_key_refund or {}).items()
        }
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {}

    def ensure_login(self):
        pass

    def call(self, method, path, **kwargs):
        query = dict(kwargs.get("query") or {})
        self.calls.append((method, path, query))
        assert path == MY_ORDERS_PATH, f"未预期的端点：{method} {path}"
        source = self.by_key_refund if query.get("orderSta") else self.by_key
        row = source.get(str(query.get("key1") or ""))
        rows = [dict(row)] if row is not None else []
        self.last_envelope = {
            "code": 0, "msg": "success", "data": rows, "count": len(rows),
        }
        return rows

    @property
    def keys(self) -> list[str | None]:
        """这一轮问过的 `key1`（窗口那两发记 `None`；**含**退单兜底那一发）。"""
        return [query.get("key1") for _m, _p, query in self.calls]

    @property
    def plain_keys(self) -> list[str | None]:
        """只数**不带 `orderSta`** 的 `key1`（= 默认视图那一发）。

        ⚠️，一张「没露面」的链路会发**两**发 `key1`（默认视图 + 退单兜底）
        ⇒ 「一轮核了哪几张、有没有轮转」这类判据必须打在这个属性上（⛔ 打 `keys` 会把
        兜底那一发也算进去，读起来像「一轮核了 40 张」）。
        ⚠️ 排除的**只有**「`key1` + `orderSta`」那一发；② 那发虽然带 `orderSta` 但**没有 `key1`**
        （它是窗口视图）⇒ 仍记 `None`（否则「①+② 两发」会少算一发，判据形状就变了）。
        """
        return [
            query.get("key1")
            for _m, _p, query in self.calls
            if not (query.get("key1") and query.get("orderSta"))
        ]


# ══ ③b ⭐ 在途按单补核（计划 §1-③）══════════════════════════


class InFlightLinkCheckTests(_Base, TenantTestCase):
    """⭐ 计划 §2-3/§2-4：①② 行集里没有的在途链路 ⇒ 按 `key1` 逐单补核（上限 **5**、轮转）。

    ⚠️ **2026-10-20 改准**：上限从 **20 降到 5**（`docs/plans/2026-10-20-upstream-waf-risk-reduction.md`
    §2.4：压低对上游的发数、避开对端 WAF）⇒ 下面那条「上限 + 轮转」用例里的**字面值**同一次
    改准（它钉的正是这次改的常量：一轮补核几张、一轮发几发）。判据形状与强度**没变**。
    """


    def test_a_probe_hit_goes_through_the_same_pull_row(self):
        """命中 ⇒ 走**同一支** `_pull_row`（判据打在**终态**上：状态真的被回拉）。

        ⚠️ 少了③这一支，这一单永远停在「代练中」——窗口 24 小时之外的旧单就没人回拉了。
        """
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.link(order)
        stub = _Key1Stub(by_key={REMOTE_ID: self.row(order_sta="问题单")})
        outcome = jidanpu_upstream.pull_statuses(
            binding=self.binding, client=stub, now=self.now,
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.ISSUE)         # 终态：回拉真的发生了
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(stub.keys, [None, None, REMOTE_ID])      # 只有③带 key1
        note = self.notes(order).get()
        self.assertIn("问题单", note.reason)

    def test_the_scheduled_round_also_probes_and_counts_the_hit(self):
        """⭐ 调度那一轮的常规路径（`sync_my_orders`）也做③，并把命中计入 `linked` / `pulled`。"""
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.link(order)
        stub = _Key1Stub(by_key={REMOTE_ID: self.row(order_sta="问题单")})
        outcome = jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=stub, now=self.now,
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.ISSUE)
        self.assertEqual(outcome.linked, 1)
        self.assertEqual(outcome.pulled, 1)
        self.assertEqual(outcome.recorded, 0)
        self.assertEqual(stub.keys, [None, None, REMOTE_ID])

    def test_a_probe_with_zero_rows_only_leaves_a_note_and_stays_in_flight(self):
        """⭐ 计划 §1-③ / §2-3：**0 行 ⇒ 只留一条台账人话，⛔ 不判「远端没有这张单」**。

        判据三条（缺一条就检不出「0 行被当成消失」）：
        ① 状态一个字不动；② 链路**仍在途**（`terminated_at is None`）；③ 台账是人话「读不到」。
        ⚠️ `last_in_at` 被刷成当轮时刻是**刻意**的：那是**轮转**的载体（下一轮轮到别的那些）。
        """
        order = self.order(status=OrderStatus.IN_PROGRESS)
        link = self.link(order)
        stub = _Key1Stub()
        outcome = jidanpu_upstream.pull_statuses(
            binding=self.binding, client=stub, now=self.now,
        )
        order.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(outcome.applied, 0)
        self.assertEqual(outcome.failed, 1)
        note = self.notes(order).get()
        self.assertEqual(note.status, "failed")
        self.assertIn("读不到", note.reason)
        self.assertIsNone(link.terminated_at, "0 行被当成了「远端没有这张单」")
        self.assertEqual(link.last_in_at, self.now)

    def test_a_refunded_link_is_probed_in_the_refund_view_without_a_window(self):
        """⭐ 受控实验修（2026-10-05）：默认视图看不见已退单的单 ⇒ 按单号补核必须**再问一次退单视图**。

        现场：业务方把一张**我方在跟踪**的单改成已退单 ⇒ 那一路真机实测是
        「默认视图按单号回 **0 行**」（于是被记成「读不到」、**跟退永远不触发**），
        而「单号 + `orderSta=已退单`」能命中。
        判据四条：① 我方状态真的变「已退单」；② `followed == 1` + 审计在；
        ③ 兜底那一发**带 `orderSta`、⛔ 不带 `stime`/`etime`**（实测：带了窗恒为 0 行）；
        ④ 顺序是「先默认视图、后兜底」（⛔ 不是每轮无脑多发一发）。
        """
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.main_perf(order)
        self.sales_perf(order)
        self.link(order)
        stub = _Key1Stub(by_key_refund={REMOTE_ID: self.row(order_sta="已退单")})
        outcome = jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=stub, now=self.now,
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        self.assertEqual(outcome.followed, 1)
        self.assertTrue(OperationLog.objects.filter(action="order.transition").exists())
        self.assertIn("跟着退", self.notes(order).get().reason)
        probes = [c[2] for c in stub.calls if c[2].get("key1")]
        self.assertEqual(len(probes), 2, f"按单号那一发应当有「默认 + 退单兜底」两问：{probes}")
        self.assertNotIn("orderSta", probes[0])
        self.assertEqual(probes[1].get("orderSta"), "已退单")
        self.assertNotIn("stime", probes[1])
        self.assertNotIn("etime", probes[1])

    def test_the_refund_probe_is_skipped_when_the_plain_probe_hits(self):
        """兜底只在**默认视图问不到**时才发（⛔ 命中率高时不白打上游）。

        失败方向：把兜底写成「无条件多发一发」⇒ 本用例红（多出一发带 `orderSta` 的 `key1`）。
        """
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.link(order)
        stub = _Key1Stub(by_key={REMOTE_ID: self.row(order_sta="代练中")})
        jidanpu_upstream.sync_my_orders(binding=self.binding, client=stub, now=self.now)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        probes = [c[2] for c in stub.calls if c[2].get("key1")]
        self.assertEqual(len(probes), 1)
        self.assertNotIn("orderSta", probes[0])

    def test_the_check_is_capped_and_rotates_over_rounds(self):
        """⭐ 计划 §2-4：在途 **25** 张 ⇒ 一轮只补核 **5** 张，下一轮轮到**另外那 5 张**。

        ⚠️ 轮转键 = `last_in_at` 最旧优先（每次补核刷成当轮时刻）。夹具让 25 张的 `last_in_at`
        依次变新 ⇒ 第一轮该是**最旧的前 5 张**（逐条钉住顺序），第二轮必须轮到**紧接的 5 张**、
        ⛔ 不重问第一轮那批（少了轮转，同一批会被反复问、后面的永远不被核）。
        ⚠️ **2026-10-20 改准**：上限 20 → 5（spec §2.4）⇒ 本条的字面值同步改（发数 42 → 12）。
        """
        ids = [f"20260928000000000{i:03d}" for i in range(801, 826)]   # 25 个合成单号
        for index, remote_id in enumerate(ids):
            order = self.order(order_no=f"20260928000000000{901 + index:03d}")
            self.link(
                order, remote_id=remote_id,
                last_in_at=self.now - timedelta(minutes=25 - index),
            )
        stub = _Key1Stub()
        first = jidanpu_upstream.pull_statuses(
            binding=self.binding, client=stub, now=self.now,
        )
        self.assertEqual(first.applied, 0)
        self.assertEqual(first.failed, 5)
        # ① + ② + 5 张各**两问**（默认视图 + 退单兜底）= 12 发
        self.assertEqual(len(stub.calls), 12)
        self.assertEqual(stub.plain_keys, [None, None, *ids[:5]])   # 最旧优先、逐条钉住
        #: 每张漏面的单都补了那一发退单兜底（⛔ 不是只对第一张发）—— 5 发，一张不少。
        #: ⚠️ 判据必须同时带 `key1`：② 那一发（窗口 + 退单视图）也带 `orderSta`，别把它算进来。
        self.assertEqual(
            len([
                c[2] for c in stub.calls
                if c[2].get("orderSta") == "已退单" and c[2].get("key1")
            ]), 5,
        )

        stub.calls.clear()
        jidanpu_upstream.pull_statuses(
            binding=self.binding, client=stub, now=self.now + timedelta(seconds=61),
        )
        second_keys = [key for key in stub.plain_keys if key]
        self.assertLessEqual(set(ids[5:10]), set(second_keys),
                             "上一轮没轮到的那批第二轮没轮到（⛔ 饿死同一批）")
        self.assertSetEqual(
            set(second_keys).intersection(ids[:5]), set(),
            "第二轮不许重问第一轮刚补核过的那批（⛔ 轮转失效 = 同一批发两遍）",
        )


# ══ ④ `follow_refund` 直调（幂等出口 / 挡住出口；不走回拉）══════════════════════


class FollowRefundDirectTests(_Base, TenantTestCase):
    def test_it_is_idempotent_when_the_order_is_already_refunded(self):
        order = self.order(status=OrderStatus.REFUNDED, total="100.00")
        self.main_perf(order)
        outcome = jidanpu_upstream.follow_refund(order, now=self.now, binding=self.binding)
        self.assertFalse(outcome.changed)
        self.assertIn("幂等", outcome.reason)
        self.assertEqual(OrderRefund.objects.count(), 0)

    def test_a_settled_completed_order_is_blocked(self):
        order = self.order(status=OrderStatus.COMPLETED, total="100.00", completed_at=self.now)
        main = self.main_perf(order, settled=True)
        self.assertTrue(main.is_settled)
        outcome = jidanpu_upstream.follow_refund(order, now=self.now, binding=self.binding)
        self.assertFalse(outcome.changed)
        self.assertIn("挡住", outcome.reason)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(OrderRefund.objects.count(), 0)

    def test_it_reuses_the_shared_rollback_blocker(self):
        """⛔ 不复制第二份「已结账」判据：`follow_refund` 必须**调**既有那一把尺子。"""
        order = self.order(status=OrderStatus.COMPLETED, total="100.00", completed_at=self.now)
        self.main_perf(order, settled=True)
        with mock.patch.object(
            jidanpu_upstream.jidanpu_common, "status_rollback_blocked", return_value=True,
        ) as blocker:
            outcome = jidanpu_upstream.follow_refund(order, now=self.now, binding=self.binding)
        self.assertFalse(outcome.changed)
        blocker.assert_called_once()
        #: 桩接上了才有这个结论（对照：不接桩时同一个夹具会真退）。
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)

    def test_a_denied_transition_is_reported_not_fatal(self):
        """缺节点 ⇒ `apply_transition` 预期拒绝 ⇒ 只登记（⛔ 不静默、⛔ 不把状态写成已退单）。"""
        order = self.order(status=OrderStatus.IN_PROGRESS, total="100.00")
        self.main_perf(order)
        self.sales_perf(order)
        self.link(order)
        #: 把节点集打空（模拟该账号缺 `order.change_status`）。
        with mock.patch.object(
            jidanpu_upstream, "nodes_of", return_value=frozenset(),
        ):
            self.sync(self.row(order_sta="已退单"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(OrderRefund.objects.count(), 0)
        note = self.notes(order).get()
        self.assertEqual(note.status, "failed")
        self.assertIn("拒绝", note.reason)


# ══ D-0267（E-0265）：认领单的群码截图拉取（挂点 = `_pull_row` 内容回拉之后）════════

#: 截图拉取的读口（`ViewPic`；逐字抄 spec §2.4，⛔ 不从被测常量派生）。
VIEW_PIC_PATH = "/OrderAjax/ViewPic"
#: 合成 PNG 字节（魔数 + 零；`sniff_image_type` 认它，member 线图片测试同款）。
PNG_BODY = b"\x89PNG\r\n\x1a\n" + b"\0" * 8


class _ViewPicStub(_StubClient):
    """在 `_StubClient`（「我的订单」）之上补 **`ViewPic`** 那一跳（截图拉取的读口）。

    - `view_pic`：`msg.data` 里的图片条目（`src` / `alt`；实测形状 `jidanpu_upstream_pics`）；
    - `view_pic_error`：给一个异常 ⇒ 那一跳当「读失败」（`fetch_remote_pics` 认不出
      ViewPic 业务壳 ⇒ 原样上诉 ⇒ `pull_order_screenshots` 收成 `failed`）；
    - `view_pic_calls`：那一跳的调用数（判「不再重试」打在它上面，⛔ 不是「没报错」）。
    """

    def __init__(self, *, rows=None, view_pic=(), view_pic_error=None):
        super().__init__(rows=rows)
        self.view_pic = [dict(item) for item in view_pic]
        self.view_pic_error = view_pic_error
        self.view_pic_calls = 0

    def call(self, method, path, **kwargs):
        if path == VIEW_PIC_PATH:
            self.view_pic_calls += 1
            self.calls.append((method, path, dict(kwargs.get("query") or {})))
            if self.view_pic_error is not None:
                raise self.view_pic_error
            self.last_envelope = {
                "code": 0, "msg": {"title": "", "data": [dict(i) for i in self.view_pic]},
            }
            return []
        return super().call(method, path, **kwargs)


class ClaimedOrderScreenshotPullTests(_Base, TenantTestCase):
    """⭐ D-0267（E-0265）：上家线**认领单**的群码截图拉取。

    背景：截图拉取此前只接在「打手个人绑定」线（member），上家线（`JidanpuUpstreamBinding`）
    的回拉只走内容列 ⇒ 认领单永远没有群码截图。修法 = `_pull_row` 内容回拉之后接
    `jidanpu_upstream_pics.pull_order_screenshots`，幂等与计数口径见
    `jidanpu_upstream._pull_claimed_order_pics` 的 docstring。

    判据五条（每条都打**数据库行** / **桩的调用数**，⛔ 不打 logger）：
      ① 首轮：ViewPic 回图 ⇒ `OrderScreenshot` 行落库 + `done` 台账（修前红：无此路径）；
      ② ViewPic 失败 ⇒ `failed` 台账人话可见，**状态回写不受影响**；
      ③ 每次尝试都计数，到 5 ⇒ 停手 + 终态台账（第 6 轮起零 ViewPic 调用）；
      ④ 已有 `done`（本拉取归属的成功台账）⇒ 不再拉（H5：判据是**台账**，
         ⛔ 不是「订单名下有无任意截图行」——打手自传的本地图不许挡群码）；
      ⑤ 部分成功（存 2 报 1）算成功轮（S3）。
    ⚠️ 指派单（无镜像行）不在范围：既有全部用例的夹具就是它 ⇒ `ViewPic` 零调用，
    这一条由 `_Base` 系既有用例的桩（`assert path == MY_ORDERS_PATH`）天然钉住。
    """

    def setUp(self):
        super().setUp()
        self.order_no_seq = 0

    def _claimed_fixture(self, *, order_no, remote_id=REMOTE_ID, sales_user=None,
                         status=OrderStatus.IN_PROGRESS):
        """认领单的完整夹具：我方订单 + 链路 + **已转正**的镜像行（D-0267 的触发面）。"""
        order = self.order(order_no=order_no, status=status, sales_user=sales_user)
        self.link(order, remote_id=remote_id)
        JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL,
            upstream_order_id=remote_id,
            status=JidanpuUpstreamHallOrder.Status.PROMOTED,
            game=self.game,
            owner_binding=self.binding,
            promoted_order=order,
            first_seen_at=self.now,
        )
        return order

    def _order_no(self):
        self.order_no_seq += 1
        return f"2026092800000001{self.order_no_seq:04d}"

    def _pic_item(self, name="pull-1.png", alt="【机器人01】上传，说明：群二维码"):
        return {"src": f"https://oss-synth.invalid/{name}", "alt": alt}

    def _run(self, stub):
        with mock.patch.object(
            jidanpu_upstream_pics, "download_pic", return_value=PNG_BODY,
        ):
            return jidanpu_upstream.sync_my_orders(
                binding=self.binding, client=stub, now=self.now,
            )

    def _pics_ledger(self, order):
        return JidanpuSyncEvent.objects.filter(
            kind=jidanpu_upstream.KIND_PULL_PICS_IN, target_type="order",
            target_ref=order.order_no,
        )

    def test_first_round_pulls_the_group_qr_screenshots(self):
        """① 首轮：ViewPic 回图 ⇒ 截图行落库（群码图种）+ `done` 台账 + `uploaded_by` 口径。"""
        sales = User.objects.create_user(username="sales-up-pics", password=PASSWORD)
        order = self._claimed_fixture(order_no=self._order_no(), sales_user=sales)
        stub = _ViewPicStub(rows=[self.row()], view_pic=[self._pic_item()])
        self._run(stub)
        shots = OrderScreenshot.objects.filter(order=order)
        self.assertEqual(shots.count(), 1)
        shot = shots.get()
        self.assertEqual(shot.uploaded_by_id, sales.pk)   # S1：sales_user 优先
        self.assertEqual(shot.img_type, OrderScreenshot.IMG_GROUP_QR)
        self.assertEqual(shot.status, OrderScreenshot.STATUS_READY)
        self.assertEqual(stub.view_pic_calls, 1)
        done = self._pics_ledger(order).get()
        self.assertEqual(done.status, "done")
        self.assertIn("新存 1 张", done.reason)

    def test_late_automatic_photo_does_not_become_authoritative(self):
        order = self._claimed_fixture(order_no=self._order_no())
        stub = _ViewPicStub(rows=[self.row()], view_pic=[self._pic_item()])
        read = stub.call

        def confirm_newer_during_photo_fetch(method, path, **kwargs):
            rows = read(method, path, **kwargs)
            if path == VIEW_PIC_PATH:
                jidanpu_upstream.sync_my_orders(
                    binding=self.binding, client=_ViewPicStub(rows=[self.row()]),
                    now=self.now + timedelta(seconds=2),
                )
            return rows

        with mock.patch.object(stub, "call", side_effect=confirm_newer_during_photo_fetch):
            self._run(stub)

        self.assertEqual(stub.view_pic_calls, 1)
        self.assertFalse(OrderScreenshot.objects.filter(order=order).exists())
        self.assertFalse(self._pics_ledger(order).filter(status="done").exists())

    def test_a_failed_view_pic_never_blocks_the_status_write(self):
        """② ViewPic 读失败 ⇒ `failed` 台账人话；同一行的状态回写照常（口径③）。"""
        order = self._claimed_fixture(order_no=self._order_no())
        stub = _ViewPicStub(
            rows=[self.row(order_sta="YZT")],
            view_pic_error=ApiError("传输断了", code=None),
        )
        outcome = self._run(stub)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAUSED, "拉图失败挡了状态回写")
        self.assertEqual(outcome.linked, 1, "这一行没走回拉分支")
        self.assertEqual(OrderScreenshot.objects.filter(order=order).count(), 0)
        failed = self._pics_ledger(order).get()
        self.assertEqual(failed.status, "failed")
        self.assertIn("没拉成", failed.reason)
        self.assertIn("ApiError", failed.reason)

    def test_attempts_are_capped_at_five_with_a_terminal_note(self):
        """③ 空/失败每轮都计数，到 5 ⇒ 停手 + 一条终态台账（note_once 防复读）。"""
        order = self._claimed_fixture(order_no=self._order_no())
        stub = _ViewPicStub(rows=[self.row()], view_pic=[])   # 上家一直没传图
        for _ in range(6):
            self._run(stub)
        self.assertEqual(self._pics_ledger(order).count(), 6)  # 5 次尝试 + 1 条终态
        self.assertEqual(stub.view_pic_calls, 5, "第 6 轮还在打 ViewPic（上限形同虚设）")
        attempts = self._pics_ledger(order).filter(status="skipped").count()
        self.assertEqual(attempts, 5)
        terminal = self._pics_ledger(order).exclude(status="skipped").get()
        self.assertIn("停止自动重试", terminal.reason)
        #: 第 7 轮：零调用、零新行（终态台账被 note_once 挡住）。
        self._run(stub)
        self.assertEqual(stub.view_pic_calls, 5)
        self.assertEqual(self._pics_ledger(order).count(), 6)

    def test_a_successful_round_stops_future_pulls(self):
        """④ 已有 `done` 台账（本拉取归属）⇒ 不再拉——哪怕那张截图是打手自传的同类。"""
        order = self._claimed_fixture(order_no=self._order_no())
        stub = _ViewPicStub(rows=[self.row()], view_pic=[self._pic_item()])
        self._run(stub)
        self.assertEqual(stub.view_pic_calls, 1)
        stub.view_pic_calls = 0
        self._run(stub)
        self.assertEqual(stub.view_pic_calls, 0, "成功过的单还在重拉（幂等键失效）")
        self.assertEqual(self._pics_ledger(order).filter(status="done").count(), 1)
        self.assertEqual(OrderScreenshot.objects.filter(order=order).count(), 1)

    def test_an_existing_ready_group_qr_is_not_duplicated_by_the_pull(self):
        """⭐ 计划 Rev.2 Task 3 的**上家线回归**：共享拉图新增了「本单已有 ready 群码 ⇒ 不再落
        第二张」。上家线（`_pull_pics_once`）也走同一份共享拉图 ⇒ 这里钉住它**没被改坏**：

        - 远端再来一张群码 ⇒ 不落第二张（行数仍是 1）；
        - 不记失败（`failed=()` 的表现：台账里没有 failed 行）；
        - 对象存储里**没有孤儿**（跳发生在下载/上传之前，那个本会算出来的对象名不存在）。
        """
        order = self._claimed_fixture(order_no=self._order_no())
        existing_key = f"test/orders/{order.order_no}/qr.existing.png"
        OrderScreenshot.objects.create(
            order=order, img_type=OrderScreenshot.IMG_GROUP_QR,
            object_key=existing_key, uploaded_by=self.user,
            status=OrderScreenshot.STATUS_READY,
        )
        name = "pull-second.png"
        would_be_key = jidanpu_upstream_pics.build_inbound_object_key(
            order=order, img_type=OrderScreenshot.IMG_GROUP_QR,
            src=f"https://oss-synth.invalid/{name}", ext="png",
        )
        stub = _ViewPicStub(rows=[self.row()], view_pic=[self._pic_item(name)])

        self._run(stub)

        self.assertEqual(
            list(OrderScreenshot.objects.filter(order=order)
                 .values_list("object_key", flat=True)),
            [existing_key],
        )
        self.assertIsNone(get_storage().head(would_be_key), "跳过的图留下了孤儿对象")
        self.assertEqual(self._pics_ledger(order).filter(status="failed").count(), 0)
        self.assertEqual(stub.view_pic_calls, 1)

    def test_a_partial_success_counts_as_a_success_round(self):
        """⑤ 存 2 报 1 ⇒ `done` 台账（人话带失败数）、下一轮不再拉（行级判重兜底）。

        ⚠️ 2026-10-25 改准（计划 Rev.2 Task 3）：这三张必须是**订单截图**——群码现在
        「每单一张 ready」在下载前就被跳过，拿群码测「存 2 报 1」会只剩 1 行（判据失真）。
        """
        order = self._claimed_fixture(order_no=self._order_no())
        shot_alt = "【机器人01】上传，说明：订单截图"
        stub = _ViewPicStub(rows=[self.row()], view_pic=[
            self._pic_item("pull-1.png", alt=shot_alt),
            self._pic_item("pull-2.png", alt=shot_alt),
            self._pic_item("pull-3.png", alt=shot_alt),
        ])

        def flaky_download(src):
            if "pull-3" in src:
                raise jidanpu_upstream_pics.PicDownloadError("下家图片地址回 HTTP 500")
            return PNG_BODY

        with mock.patch.object(
            jidanpu_upstream_pics, "download_pic", side_effect=flaky_download,
        ):
            jidanpu_upstream.sync_my_orders(binding=self.binding, client=stub, now=self.now)
        self.assertEqual(OrderScreenshot.objects.filter(order=order).count(), 2)
        done = self._pics_ledger(order).get()
        self.assertEqual(done.status, "done")
        self.assertIn("另有 1 张没存上", done.reason)
        stub.view_pic_calls = 0
        self._run(stub)
        self.assertEqual(stub.view_pic_calls, 0)

    def test_an_assigned_order_without_a_promoted_mirror_is_never_pulled(self):
        """范围边界（H6/S4）：指派单（无镜像行）⇒ 零 ViewPic 调用、零台账。"""
        order = self.order(order_no=self._order_no())
        self.link(order)
        stub = _ViewPicStub(rows=[self.row()])
        self._run(stub)
        self.assertEqual(stub.view_pic_calls, 0)
        self.assertEqual(self._pics_ledger(order).count(), 0)
        self.assertEqual(OrderScreenshot.objects.filter(order=order).count(), 0)


class _SourceFallbackStub:
    """「我的订单」按 `key1` 回那一行（其余 key 一律 0 行）。

    ⚠️ 逐字记下每一发的 `key1`：判据要能区分「来源单号那一问到底发没发」。
    """

    def __init__(self, *, source_no, order_row=None, additional_rows=()):
        self.source_no = str(source_no)
        self.order_rows = ([order_row] if order_row else []) + list(additional_rows)
        self.key1_calls: list[str] = []
        self.last_envelope: dict = {}

    def ensure_login(self):
        pass

    def call(self, method, path, **kwargs):
        if path != MY_ORDERS_PATH:  # pragma: no cover —— 这一支只该问「我的订单」
            raise AssertionError(f"未预期的端点：{method} {path}")
        query = dict(kwargs.get("query") or {})
        key = str(query.get("key1") or "")
        if key:
            self.key1_calls.append(key)
        rows = [dict(row) for row in self.order_rows] if key == self.source_no else []
        self.last_envelope = {"code": 0, "msg": "success", "data": rows, "count": len(rows)}
        return rows


class SourceOrderNoFallbackTests(_Base, TenantTestCase):
    """⭐ 2026-10-23：按**来源单号**（`orderIdView`）在「我的订单」里补核。"""

    def _source_no(self):
        return f"VIEW-{REMOTE_ID}"

    def test_a_row_found_by_source_no_goes_through_the_same_pull_row(self):
        """上家单号两发 0 行 ⇒ 再按来源单号问一次；命中就照样回拉（终态判据）。"""
        order = self.order(status=OrderStatus.IN_PROGRESS, source_order_no=self._source_no())
        self.link(order)
        stub = _SourceFallbackStub(
            source_no=self._source_no(), order_row=self.row(order_sta="问题单"),
        )
        outcome = jidanpu_upstream.pull_statuses(
            binding=self.binding, client=stub, now=self.now,
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.ISSUE)
        self.assertEqual(outcome.applied, 1)
        self.assertIn(self._source_no(), stub.key1_calls)
        self.assertIn(REMOTE_ID, stub.key1_calls, "应当先问过上家单号两发")

    def test_same_source_on_a_different_remote_order_cannot_refund_or_edit_this_order(self):
        source_no = self._source_no()
        order = self.order(status=OrderStatus.IN_PROGRESS, source_order_no=source_no)
        self.main_perf(order)
        self.link(order)
        stub = _SourceFallbackStub(source_no=source_no, order_row=self.row(
            order_sta="已退单", order_id="synthetic-other-upstream-order",
            orderIdView=source_no, orderRemark="别单备注",
        ))

        outcome = jidanpu_upstream.pull_statuses(binding=self.binding, client=stub, now=self.now)

        order.refresh_from_db()
        self.assertEqual((order.status, order.remark), (OrderStatus.IN_PROGRESS, ""))
        self.assertEqual((outcome.applied, outcome.failed), (0, 1))
        self.assertEqual(stub.key1_calls, [REMOTE_ID, REMOTE_ID, source_no])

    def test_source_lookup_continues_past_an_unrelated_row_to_the_linked_order(self):
        source_no = self._source_no()
        order = self.order(status=OrderStatus.IN_PROGRESS, source_order_no=source_no)
        self.link(order)
        unrelated = self.row(order_id="synthetic-other-upstream-order", orderIdView=source_no)
        expected = self.row(order_sta="问题单", orderRemark="本单备注")
        stub = _SourceFallbackStub(source_no=source_no, additional_rows=[unrelated, expected])

        outcome = jidanpu_upstream.pull_statuses(binding=self.binding, client=stub, now=self.now)

        order.refresh_from_db()
        self.assertEqual((order.status, order.remark), (OrderStatus.ISSUE, "本单备注"))
        self.assertEqual(outcome.applied, 1)

    def test_when_all_probes_miss_it_still_only_notes_and_stays_in_flight(self):
        """三路都 0 行 ⇒ 仍然只记「读不到」，⛔ 不判远端没有这张单。"""
        order = self.order(status=OrderStatus.IN_PROGRESS, source_order_no=self._source_no())
        link = self.link(order)
        stub = _SourceFallbackStub(source_no=self._source_no(), order_row=None)
        outcome = jidanpu_upstream.pull_statuses(
            binding=self.binding, client=stub, now=self.now,
        )
        order.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(outcome.failed, 1)
        self.assertIn(self._source_no(), stub.key1_calls)
        self.assertIn("读不到", self.notes(order).get().reason)
        self.assertIsNone(link.terminated_at)

    def test_the_source_lookup_is_skipped_when_the_order_has_no_source_no(self):
        """我方没存来源单号 ⇒ 不发这一问（⛔ 不拿空串去打上游）。"""
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.link(order)
        stub = _SourceFallbackStub(source_no="", order_row=None)
        jidanpu_upstream.pull_statuses(binding=self.binding, client=stub, now=self.now)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertNotIn("", stub.key1_calls)
        self.assertEqual(stub.key1_calls.count(REMOTE_ID), 2, "只该有默认 + 退单兜底两发")


class RemovedTakerThreeStrikesTests(_Base, TenantTestCase):
    """⭐ 2026-10-23（业务方裁定）：连续 3 次「上家单号 / 来源单号都查不到」⇒ 判「上家已移除接手人」。

    判据四条：① 没结账 ⇒ 改「已退单」并写清日志；② 结过账 ⇒ **不改状态**，只提醒；
    ③ 中间查到一次 ⇒ 计数清零；④ 门槛 = 3 次 **且** 跨度 ≥ 5 分钟（不是同一轮重复）。
    """

    def _three_misses(self, *, minutes=(0, 3, 6), order=None):
        order = order or self.order(status=OrderStatus.IN_PROGRESS)
        link = self.link(order)
        stub = _Key1Stub()
        for m in minutes:
            jidanpu_upstream.pull_statuses(
                binding=self.binding, client=stub, now=self.now + timedelta(minutes=m),
            )
        order.refresh_from_db()
        link.refresh_from_db()
        return order, link

    def test_three_consecutive_misses_mark_the_order_refunded(self):
        order, link = self._three_misses()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        self.assertIsNotNone(link.terminated_at)
        self.assertIsNone(link.miss_since)
        self.assertTrue(self.notes(order).filter(reason__contains="上家已移除当前接手人").exists())

    def test_the_reason_stamps_miss_since_in_the_project_timezone(self):
        """D-0302（2026-10-24）：台账人话里「自 {since} 起」是**项目时区**墙钟。

        三连从 `self.now`（UTC 2026-09-28 12:00）起算 ⇒ `miss_since` 的北京墙钟是
        **09-28 20:00**；旧写法裸 `strftime` 出 UTC 的 12:00、订单台账上差 8 小时。
        判据钉值：「自 09-28 20:00 起」逐字出现在 reason 里（夹具全合成值，E-0077）。
        """
        order, _link = self._three_misses()
        reasons = list(self.notes(order).values_list("reason", flat=True))
        self.assertTrue(
            any("自 09-28 20:00 起" in r for r in reasons),
            reasons,
        )
        self.assertFalse(
            any("自 09-28 12:00 起" in r for r in reasons),
            "reason 里的时刻还是 UTC 墙钟（D-0302 回退）",
        )

    def test_a_settled_order_is_only_flagged_not_refunded(self):
        order = self.order(status=OrderStatus.IN_PROGRESS)
        self.main_perf(order, settled=True)
        order, link = self._three_misses(order=order)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS, "结过账的单不许自动退")
        reasons = list(self.notes(order).values_list("reason", flat=True))
        self.assertTrue(any("已结过账" in r for r in reasons), reasons)
        self.assertEqual(link.miss_count, 0)

    def test_a_hit_in_between_resets_the_counter(self):
        order = self.order(status=OrderStatus.IN_PROGRESS)
        link = self.link(order)
        miss = _Key1Stub()
        for m in (0, 3):
            jidanpu_upstream.pull_statuses(
                binding=self.binding, client=miss, now=self.now + timedelta(minutes=m),
            )
        link.refresh_from_db()
        self.assertEqual(link.miss_count, 2)
        hit = _Key1Stub(by_key={REMOTE_ID: self.row(order_sta="代练中")})
        jidanpu_upstream.pull_statuses(
            binding=self.binding, client=hit, now=self.now + timedelta(minutes=6),
        )
        link.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(link.miss_count, 0)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    def test_two_misses_are_not_enough(self):
        order, link = self._three_misses(minutes=(0, 6))
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(link.miss_count, 2)
