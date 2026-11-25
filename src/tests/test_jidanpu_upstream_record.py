"""上家鸡蛋铺线（客服个人绑定）：**指派单录进未分配池**（计划 Task 5；spec §5.2 / A3 / A7 / A9）。

本文件检四件事（每条都**钉值**，⛔ 不是「有就行」）：

① **录进门槛**（A7 / spec §5.2）：只有上家 `orderSta ∈ {待上号, 代练中}`（**在途**）才录；
   已完成 / 已退单 / 问题单这些历史行**零建单** + 一条人话台账——否则第一次绑定会把该账号
   名下全部历史单灌进未分配池；
② **录单人 = 该绑定的客服**（A3）：`Order.status == UNASSIGNED`、`Order.sales_user` 是他、
   `Performance(emp_type=SALES)` 那行的 `user` **也是他**（断言**数据库行**），
   而打手那条（`MAIN`）的 `user` **空着**等人指派；
③ **钱的口径**（A9 / spec §15.1 改准 + **口径修订 2026-09-29**）：
   `total_price ← recFee`（上家接单价 = 我方客户价，**不受比例影响**）、
   **`hall_price` = 勾兑后的发单价**（`recFee × 该绑定 hall_price_percent / 100`，
   默认 100 = 原价接手；**`recFee` 空 / 认不出 ⇒ `hall_price = 0.00`**，⛔ 不再留空）、
   上家 `price` **不落任何金额列**（全表搜）；
④ **幂等 + 链路**：同一张上家单只录一次（重复轮 / 并发轮都不新增），链路
   `upstream_order_id` **逐字**等于上家 `orderId`，且这一路**⛔ 不套地址级去重**
   （`dedupe_owner` 只管「谁负责拉大厅」，两件事不同）。

⚠️ **修复轮追加的三条**（评审实测的缺陷 + 业务方新口径）：

⑤ **不录台账的幂等键含 `binding`**：同地址两份绑定看到同一行 ⇒ 台账 **2 行**（各指各的绑定），
   再跑一轮仍 2 行（⛔ 少了 `binding`，第二份会被第一份**静默吞掉**）；
⑥ **超宽上游文本按列宽截断**（`project_text` 900（D-0218，此前 300）/ 游戏三列 128）且订单**照录**——⛔ 没有用例时
   把截断删掉是全绿的（PostgreSQL `value too long` 才会在真库里炸）；
⑦ **`order_type` 写「代练」= `OrderType.BOOST`**（与 `promote_mirror` 同一条同类路）。

⚠️ 判据打**数据库行**与**桩的调用集合**（`AGENTS.md` §5-G），⛔ 不打 logger。
⚠️ **零真实网络**：`_StubClient` 打在 SDK 边界。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `kefu-up-a`、
   密码 `pw-canary-a`、单号 `20260928000000000201` 起（全零段，一眼是合成值）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import models as django_models
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase
from jidanpu.errors import ApiError

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business import jidanpu_upstream
from apps.business.models import (
    EmpType,
    Game,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    JidanpuUpstreamOrderLink,
    Order,
    OrderSource,
    OrderStatus,
    OrderType,
    Performance,
)
from apps.business.order_no import generate_order_no
from tests.p5_auth_helpers import make_logged_in_client

#: `Order` 上的**金额列**（`DecimalField`）——从模型元数据派生，⛔ 不手抄列名：
#: 将来新增一个金额列会自动进判据。
#: ⚠️ 派生法本身有「筛错 ⇒ 空集 ⇒ 断言恒真」的风险 ⇒ 下面用 `_money_field_names()` 的
#: 一条**非空且含具名列**的钉子兜住（E-0065 同族：自指断言必须有独立契约值兜底）。


def _money_field_names() -> list[str]:
    return [
        field.name
        for field in Order._meta.get_fields()
        if isinstance(field, django_models.DecimalField) and getattr(field, "concrete", False)
    ]

#: 端点路径**逐字**抄 spec §2.1（⛔ 不从被测常量派生）。
MY_ORDERS_PATH = "/OrderAjax/GetMyOrderList"

BASE_URL = "https://jidanpu-upstream-a.test.invalid"
PASSWORD = "pw-canary-a"

#: 上家「我的订单」每页固定 20 行（spec §2.1 **实测**，⛔ 不是契约 ⇒ 判据不写死它，这里只造夹具）。
PER_PAGE = 20

#: 三档历史状态（都不在在途集合里 ⇒ 一条都不该录）。
HISTORY_TOKENS = ("已完成", "已退单", "问题单")


class _StubClient:
    """`JidanpuClient` 的替身：按 `page` 从一份行里切页，并记录每一次调用。

    ⚠️ 必须把 `last_envelope` 写成 `code == 0`：被测代码的读端点成功码判据
    （`_require_read_success`）打的就是它（⛔ 不靠 SDK 宽松解包）。
    """

    def __init__(self, *, rows=None, per_page=PER_PAGE, account="acc-up-a"):
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


class _WindowStub:
    """按**请求里的收窄参数**回不同的行：窗口① / 退单视图② / `key1` 单查③。

    ⚠️ 判据要看「哪一发带了什么参数」⇒ 逐字记下每一次的 `query`（⛔ 不带窗口的那一发
    = 旧的整表拉取，正是要消灭的形状）。
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
        offset = (int(query["page"]) - 1) * PER_PAGE
        page_rows = rows[offset:offset + PER_PAGE]
        self.last_envelope = {
            "code": 0, "msg": "success", "data": page_rows, "count": len(rows),
        }
        return page_rows


class _TimedWindowStub:
    def __init__(self, rows, *, refunds=(), fail_query=None):
        self.rows = list(rows)
        self.refunds = list(refunds)
        self.fail_query = fail_query
        self.calls = []
        self.last_envelope = {}

    def call(self, method, path, *, query):
        assert (method, path) == ("GET", MY_ORDERS_PATH)
        self.calls.append(query.copy())
        if self.fail_query is not None and self.fail_query(query):
            raise ApiError("合成：上家暂时读不到这页", status_code=200, code=-1)
        if "key1" in query:
            source = self.rows + self.refunds if query.get("orderSta") else self.rows
            result = [
                row for _, row in source
                if row["orderId"] == query["key1"] and (not query.get("orderSta") or row["orderSta"] == query["orderSta"])
            ]
        else:
            start, end = (datetime.fromisoformat(query[key]) for key in ("stime", "etime"))
            # 退单视图可单独带上默认视图没有的记录，也可能返回同号的不同状态。
            source = self.rows + self.refunds if query.get("orderSta") else self.rows
            result = [
                row for at, row in sorted(source, key=lambda item: (item[0], item[1]["orderId"]), reverse=True)
                if start <= at <= end and (not query.get("orderSta") or row["orderSta"] == query["orderSta"])
            ]
        offset = (int(query["page"]) - 1) * PER_PAGE
        self.last_envelope = {"code": 0, "count": len(result)}
        return result[offset:offset + PER_PAGE]


class UpstreamAssignedOrderRecordTests(TenantTestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 28, 12, 0, tzinfo=dt_timezone.utc)
        #: 客服甲 = 本线绑定的**录单人**；刻意给 `first_name`（钉住 `sales_agent` 走**显示名**）。
        self.user = User.objects.create_user(
            username="kefu-up-a", password=PASSWORD, first_name="测试客服甲",
        )
        #: 同地址的另一位客服（造「这一路不套地址级去重」的对照）。
        self.other_user = User.objects.create_user(
            username="kefu-up-b", password=PASSWORD, first_name="测试客服乙",
        )
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account="acc-up-a",
            password=PASSWORD, enabled=True,
        )
        self.other_binding = JidanpuUpstreamBinding.objects.create(
            user=self.other_user, jidanpu_base_url=BASE_URL, account="acc-up-b",
            password=PASSWORD, enabled=True,
        )
        self.game_a = Game.objects.create(name="测试游戏甲")

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def my_order_row(self, *, order_sta="待上号", order_id="20260928000000000201",
                     rec_fee="88.00", price="120.00", game_name="测试游戏甲", **extra):
        row = {
            "orderId": order_id,
            "orderIdView": f"VIEW-{order_id}",
            "orderSta": order_sta,
            "gameName": game_name,
            "gameArea": "安卓",
            "gameServer": "微信区",
            "project": "合成项目",
            "orderContent": "合成需求",
            "orderRemark": "合成备注",
            "roleName": "合成角色",
            "gameAcc": "acc-synth",
            "zgameAcc": "sub-synth",
            "recFee": rec_fee,
            "price": price,
        }
        row.update(extra)
        return row

    def scripted(self, rows, *, account="acc-up-a"):
        return _StubClient(rows=rows, account=account)

    def test_large_window_records_newest_then_resumes_after_reloading_binding(self):
        local_time = self.now.astimezone(dt_timezone(timedelta(hours=8))).replace(tzinfo=None)
        fixtures = [
            (local_time - timedelta(minutes=i + 2), self.my_order_row(order_id=f"BATCH-{i:04}"))
            for i in range(1325)
        ]

        def sync_once(now):
            upstream = _TimedWindowStub(fixtures)
            binding = JidanpuUpstreamBinding.objects.get(pk=self.binding.pk)
            jidanpu_upstream.sync_my_orders(binding=binding, client=upstream, now=now)
            list_calls = sum("key1" not in call for call in upstream.calls)
            self.assertLessEqual(list_calls, 24)

        sync_once(self.now)
        links = JidanpuUpstreamOrderLink.objects.filter(binding=self.binding)
        self.assertTrue(links.filter(upstream_order_id="BATCH-0000").exists())
        first_count = links.count()
        self.assertLess(first_count, 1325)

        fixtures.append((local_time + timedelta(minutes=1), self.my_order_row(order_id="BATCH-NEW")))
        sync_once(self.now + timedelta(minutes=2))
        self.assertTrue(links.filter(upstream_order_id="BATCH-NEW").exists())
        self.assertGreater(links.count(), first_count + 1)

        for minute in range(3, 28):
            sync_once(self.now + timedelta(minutes=minute))
            if links.count() == len(fixtures):
                break
        self.assertSetEqual(set(links.values_list("upstream_order_id", flat=True)), {row["orderId"] for _, row in fixtures})
        self.assertEqual(Order.objects.count(), 1326)
        sync_once(self.now + timedelta(minutes=28))
        self.assertEqual(Order.objects.count(), 1326)

    def test_dense_window_resumes_offset_and_keeps_default_view_precedence(self):
        timestamp = self.now.astimezone(dt_timezone(timedelta(hours=8))).replace(tzinfo=None)
        timestamp -= timedelta(minutes=10)
        live = [
            (timestamp, self.my_order_row(order_id=f"DENSE-{i:04}"))
            for i in range(350)
        ]
        refunds = [(timestamp, self.my_order_row(order_id="DENSE-0349", order_sta="已退单"))]
        links = JidanpuUpstreamOrderLink.objects.filter(binding=self.binding)
        count_was_partial = False
        for minute in range(25):
            stub = _TimedWindowStub(live, refunds=refunds)
            outcome = jidanpu_upstream.sync_my_orders(
                binding=JidanpuUpstreamBinding.objects.get(pk=self.binding.pk),
                client=stub, now=self.now + timedelta(minutes=minute),
            )
            self.assertLessEqual(sum("key1" not in call for call in stub.calls), 24)
            count_was_partial |= 0 < links.count() < 350
            if not outcome.backfill_pending:
                break
        self.assertTrue(count_was_partial)
        self.assertFalse(outcome.backfill_pending)
        self.assertEqual(links.count(), 350)
        self.assertEqual(links.get(upstream_order_id="DENSE-0349").order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(Order.objects.filter(status=OrderStatus.REFUNDED).count(), 0)

    def test_dense_window_rechecks_earlier_pages_when_the_count_stays_unchanged(self):
        timestamp = self.now.astimezone(dt_timezone(timedelta(hours=8))).replace(tzinfo=None)
        timestamp -= timedelta(hours=23, minutes=59, seconds=50)
        fixtures = [
            (timestamp, self.my_order_row(order_id=f"DRIFT-{i:04}"))
            for i in range(350)
        ]
        links = self.binding.links.all()
        changed = False
        for minute in range(25):
            outcome = jidanpu_upstream.sync_my_orders(
                binding=JidanpuUpstreamBinding.objects.get(pk=self.binding.pk), client=_TimedWindowStub(fixtures),
                now=self.now + timedelta(minutes=minute),
            )
            if not changed and 20 < links.count() < 350:
                # 同一冻结查询的第一页发生等量替换，续页的上一页并未变化。
                fixtures[-1] = (timestamp, self.my_order_row(order_id="DRIFT-NEW"))
                changed = True
            if not outcome.backfill_pending:
                break
        self.assertTrue(changed)
        self.assertFalse(outcome.backfill_pending)
        self.assertTrue(links.filter(upstream_order_id="DRIFT-NEW").exists())
        self.assertEqual(Order.objects.count(), 351)

    def test_failed_historical_batch_can_resume_without_replaying_an_applied_batch(self):
        timestamp = self.now.astimezone(dt_timezone(timedelta(hours=8))).replace(tzinfo=None)
        fixtures = [
            (timestamp - timedelta(minutes=i + 2), self.my_order_row(order_id=f"RETRY-{i:04}"))
            for i in range(400)
        ]
        links = JidanpuUpstreamOrderLink.objects.filter(binding=self.binding)
        jidanpu_upstream.sync_my_orders(binding=self.binding, client=_TimedWindowStub(fixtures), now=self.now)
        self.assertTrue(0 < links.count() < 400)
        fixtures.append((timestamp + timedelta(minutes=1), self.my_order_row(order_id="RETRY-NEW")))

        recent_cutoff = timestamp.strftime("%Y-%m-%d %H:%M:%S")
        failed = _TimedWindowStub(fixtures, fail_query=lambda query: query.get("etime", "9999") <= recent_cutoff)
        with self.assertRaisesMessage(ApiError, "暂时读不到"):
            jidanpu_upstream.sync_my_orders(
                binding=self.binding, client=failed, now=self.now + timedelta(minutes=2),
            )
        self.assertTrue(links.filter(upstream_order_id="RETRY-NEW").exists())

        retry = _TimedWindowStub(fixtures)
        jidanpu_upstream.sync_my_orders(
            binding=JidanpuUpstreamBinding.objects.get(pk=self.binding.pk),
            client=retry, now=self.now + timedelta(minutes=2),
        )
        failed_windows = [query for query in failed.calls if query.get("etime", "9999") <= recent_cutoff]
        self.assertTrue(failed_windows)
        self.assertIn(retry.calls[0], failed_windows)
        for minute in range(3, 12):
            if links.count() == 401:
                break
            jidanpu_upstream.sync_my_orders(
                binding=self.binding, client=_TimedWindowStub(fixtures), now=self.now + timedelta(minutes=minute),
            )
        self.assertEqual(links.count(), 401)
        self.assertEqual(Order.objects.count(), 401)

    def test_partial_window_does_not_count_unseen_orders_as_missing(self):
        make_logged_in_client(username="permitted-probe", groups=("测试客服",), nodes=("order.change_status",))
        self.binding.user = User.objects.get(username="permitted-probe")
        self.binding.save(update_fields=["user"])
        jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([self.my_order_row(order_id="ABSENT")]),
            now=self.now - timedelta(minutes=10),
        )
        link = self.binding.links.get(upstream_order_id="ABSENT")
        Order.objects.filter(pk=link.order_id).update(status=OrderStatus.IN_PROGRESS)
        link.miss_count = 2
        link.miss_since = self.now - timedelta(minutes=9)
        link.miss_at = self.now - timedelta(minutes=6)
        link.save(update_fields=["miss_count", "miss_since", "miss_at"])

        timestamp = self.now.astimezone(dt_timezone(timedelta(hours=8))).replace(tzinfo=None)
        fixtures = [
            (timestamp - timedelta(minutes=i + 2), self.my_order_row(order_id=f"HISTORY-{i:04}", order_sta="已完成"))
            for i in range(1000)
        ]
        stub = _TimedWindowStub(fixtures)
        outcome = jidanpu_upstream.sync_my_orders(binding=self.binding, client=stub, now=self.now)
        self.assertTrue(outcome.backfill_pending)
        self.assertTrue(any(query.get("key1") == "ABSENT" for query in stub.calls))
        link.refresh_from_db()
        self.assertEqual(link.miss_count, 2)
        self.assertEqual(link.order.status, OrderStatus.IN_PROGRESS)

        # 同一条在窗口外的退单一旦被精确读到，列表尚未拉齐也要按原规则跟退。
        history_end = timestamp.strftime("%Y-%m-%d %H:%M:%S")
        for minute in (1, 2):
            stub = _TimedWindowStub(fixtures, refunds=[
                (timestamp - timedelta(days=2), self.my_order_row(order_id="ABSENT", order_sta="已退单")),
            ], fail_query=lambda query: query.get("etime", "9999") <= history_end)
            with self.assertRaisesMessage(ApiError, "暂时读不到"):
                jidanpu_upstream.sync_my_orders(
                    binding=self.binding, client=stub, now=self.now + timedelta(minutes=minute),
                )
        link.refresh_from_db()
        self.assertEqual(link.order.status, OrderStatus.REFUNDED)

    def test_a_failed_recent_window_does_not_starve_backfill_or_later_new_orders(self):
        timestamp = self.now.astimezone(dt_timezone(timedelta(hours=8))).replace(tzinfo=None)
        fixtures = [
            (timestamp - timedelta(minutes=i + 3), self.my_order_row(order_id=f"FAIR-{i:04}"))
            for i in range(600)
        ]
        links = JidanpuUpstreamOrderLink.objects.filter(binding=self.binding)
        jidanpu_upstream.sync_my_orders(binding=self.binding, client=_TimedWindowStub(fixtures), now=self.now)
        first_count = links.count()
        self.assertTrue(0 < first_count < 600)
        blocked_end = (timestamp + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        blocked = lambda query: query.get("etime") == blocked_end
        for minute in (1, 2):
            if minute == 2:
                fixtures.append((timestamp + timedelta(seconds=90), self.my_order_row(order_id="FAIR-NEW")))
            with self.assertRaisesMessage(ApiError, "暂时读不到"):
                jidanpu_upstream.sync_my_orders(
                    binding=self.binding, client=_TimedWindowStub(fixtures, fail_query=blocked),
                    now=self.now + timedelta(minutes=minute),
                )
        self.assertGreater(links.exclude(upstream_order_id="FAIR-NEW").count(), first_count)
        self.assertTrue(links.filter(upstream_order_id="FAIR-NEW").exists())

    def test_full_page_with_low_reported_count_checks_for_a_tail(self):
        fixtures = [self.my_order_row(order_id=f"TAIL-{i:02}") for i in range(15)]
        stub = _StubClient(rows=fixtures, per_page=10)
        real_call = stub.call

        def low_count(*args, **kwargs):
            rows = real_call(*args, **kwargs)
            stub.last_envelope["count"] = 10
            return rows

        stub.call = low_count
        outcome = jidanpu_upstream.sync_my_orders(binding=self.binding, client=stub, now=self.now)
        self.assertFalse(outcome.backfill_pending)
        self.assertEqual(outcome.recorded, 15)
        self.assertEqual(self.binding.links.count(), 15)

    def test_duplicated_id_across_pages_keeps_dense_window_open(self):
        timestamp = self.now.astimezone(dt_timezone(timedelta(hours=8))).replace(tzinfo=None)
        timestamp -= timedelta(minutes=10)
        fixtures = [
            (timestamp, self.my_order_row(order_id=f"OVERLAP-{i:03}"))
            for i in range(350)
        ]
        absent = self.my_order_row(order_id="ABSENT")
        jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([absent]), now=self.now - timedelta(minutes=10),
        )
        absent_link = self.binding.links.get(upstream_order_id="ABSENT")
        Order.objects.filter(pk=absent_link.order_id).update(status=OrderStatus.IN_PROGRESS)
        absent_link.miss_count = 2
        absent_link.save(update_fields=["miss_count"])
        stub = _TimedWindowStub(fixtures)
        real_call = stub.call

        def duplicate_from_previous_page(method, path, *, query):
            rows = real_call(method, path, query=query)
            if query.get("orderSta") or "stime" not in query:
                return rows
            start, end = (datetime.fromisoformat(query[key]) for key in ("stime", "etime"))
            if end - start <= timedelta(seconds=2) and int(query["page"]) == 5 and rows:
                # 保持行数和 count 相同，但前面出现过的单号挤掉一条真实订单。
                return [fixtures[-1][1], *rows[1:]]
            return rows

        stub.call = duplicate_from_previous_page
        for minute in range(12):
            now = self.now + timedelta(minutes=minute)
            try:
                outcome = jidanpu_upstream.sync_my_orders(binding=self.binding, client=stub, now=now)
            except ApiError as exc:
                self.assertIn("分页返回重复单号", str(exc))
                break
            self.assertTrue(outcome.backfill_pending)
        else:
            self.fail("跨页重复的密集分页未被识别")
        self.binding.refresh_from_db()
        self.assertIsNotNone(self.binding.my_orders_progress)
        absent_link.refresh_from_db()
        self.assertEqual(absent_link.miss_count, 2)
        self.assertEqual(absent_link.order.status, OrderStatus.IN_PROGRESS)

        for minute in range(12, 27):
            outcome = jidanpu_upstream.sync_my_orders(
                binding=self.binding, client=_TimedWindowStub(fixtures), now=self.now + timedelta(minutes=minute),
            )
            if not outcome.backfill_pending:
                break
        self.assertFalse(outcome.backfill_pending)
        self.assertEqual(self.binding.links.count(), 351)

    # ── ① 录进未分配池 + 录单人（spec §11-5 / A3）────────────────────────────

    def test_assigned_order_lands_in_the_pool_with_the_customer_service_as_recorder(self):
        """⭐ spec §11-5 / A3：`UNASSIGNED` + `sales_user` = 该客服 + `SALES` 绩效行的 `user` 也是他。

        ⚠️ 判据打在**数据库行**上（`AGENTS.md` §5-G），⛔ 不只看返回值。
        """
        outcome = jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([self.my_order_row()]), now=self.now,
        )
        self.assertEqual(outcome.recorded, 1)
        order = Order.objects.get()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(order.sales_user_id, self.binding.user_id)
        #: 显示名（不是 `username`）——`apps.accounts.display.user_display_name` 的口径。
        self.assertEqual(order.sales_agent, "测试客服甲")
        sales = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(sales.user_id, self.binding.user_id)
        #: 打手那条**空着**等人指派（本线⛔ 不自动指派）。
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertIsNone(main.user_id)

    def test_game_rules_only_filter_new_pool_orders_and_set_new_hall_price(self):
        game_b = Game.objects.create(name="测试排除游戏")
        ids = ("SYN-OLD", "SYN-ALLOWED", "SYN-BLOCKED")
        old = self.my_order_row(order_id=ids[0], game_name=game_b.name)
        jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([old]), now=self.now,
        )

        self.binding.game_rules = [{"game_id": self.game_a.pk, "hall_price_percent": 25}]
        self.binding.save(update_fields=["game_rules"])
        rows = [
            self.my_order_row(order_id=ids[0], game_name=game_b.name, rec_fee="150.00"),
            self.my_order_row(order_id=ids[1], rec_fee="100.00"),
            self.my_order_row(order_id=ids[2], game_name=game_b.name),
        ]
        outcome = jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted(rows), now=self.now,
        )
        orders = {
            link.upstream_order_id: link.order
            for link in JidanpuUpstreamOrderLink.objects.select_related("order")
        }
        self.assertEqual(set(orders), set(ids[:2]))
        self.assertEqual((outcome.linked, outcome.recorded, outcome.skipped), (1, 1, 1))
        self.assertEqual(orders[ids[0]].hall_price, Decimal("88.00"))
        self.assertEqual(orders[ids[1]].total_price, Decimal("100.00"))
        self.assertEqual(orders[ids[1]].hall_price, Decimal("25.00"))

    def test_direct_assignment_of_old_card_reuses_old_percent_without_accepting_other_accounts_cards(self):
        game_b = Game.objects.create(name="测试已移除的游戏")
        matching = self.my_order_row(order_id="SYN-OLD-ASSIGN", game_name=game_b.name, rec_fee="120.00")
        foreign = self.my_order_row(order_id="SYN-OTHER-ASSIGN", game_name=game_b.name)
        for wire, owner in ((matching, self.binding), (foreign, self.other_binding)):
            JidanpuUpstreamHallOrder.objects.create(
                upstream_base_url=BASE_URL, upstream_order_id=wire["orderId"], game=game_b,
                owner_binding=owner, first_seen_at=self.now, status="listed",
                hall_price_percent_at_sync=35, upstream_payload=wire,
                remote_rec_fee=Decimal("90.00"),
            )
        card = JidanpuUpstreamHallOrder.objects.get(upstream_order_id=matching["orderId"])
        shadow = Order.objects.create(
            order_no=generate_order_no(), game=game_b, upstream_hall_card=card,
            total_price=Decimal("90.00"), hall_price=Decimal("31.50"),
            status=OrderStatus.WAITING_GRAB,
        )
        self.binding.game_rules = [{"game_id": self.game_a.pk, "hall_price_percent": 81}]
        self.binding.save(update_fields=["game_rules"])

        outcome = jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([matching, foreign]), now=self.now,
        )
        self.assertEqual((outcome.recorded, outcome.skipped), (1, 1))
        link = JidanpuUpstreamOrderLink.objects.get(binding=self.binding)
        self.assertEqual(link.upstream_order_id, matching["orderId"])
        self.assertEqual(link.order.hall_price, Decimal("42.00"))
        self.assertEqual(link.order.total_price, Decimal("120.00"))
        self.assertEqual(link.order.status, OrderStatus.UNASSIGNED)
        shadow.refresh_from_db()
        card.refresh_from_db()
        self.assertEqual(shadow.status, OrderStatus.REFUNDED)
        self.assertEqual(card.status, JidanpuUpstreamHallOrder.Status.PROMOTED)
        self.assertEqual(card.promoted_order_id, link.order_id)
        again = jidanpu_upstream.record_assigned_order(
            self.binding, matching, game=game_b, now=self.now,
        )
        self.assertEqual(again.pk, link.order_id)
        self.assertEqual(JidanpuUpstreamOrderLink.objects.filter(binding=self.binding).count(), 1)

    def test_the_assigned_order_carries_the_upstream_content_projection(self):
        """spec §5.2 的内容投影**逐字**落列（⛔ 不猜、⛔ 不随手拼接）。"""
        jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([self.my_order_row()]), now=self.now,
        )
        order = Order.objects.get()
        self.assertEqual(order.project_text, "合成项目")
        self.assertEqual(order.content, "合成需求")
        self.assertEqual(order.remark, "合成备注")
        self.assertEqual(order.game_role_name, "合成角色")
        self.assertEqual(order.game_account, "acc-synth")
        self.assertEqual(order.game_sub_account, "sub-synth")
        #: 来源留空（D-e）。
        self.assertEqual(order.source, "")

    def test_the_link_row_carries_the_upstream_order_id_verbatim(self):
        """⭐ 幂等键是上家 `orderId`（B4）：链路那一列**逐字**等于夹具里的 `orderId`。"""
        jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([self.my_order_row()]), now=self.now,
        )
        link = JidanpuUpstreamOrderLink.objects.get()
        self.assertEqual(link.upstream_order_id, "20260928000000000201")
        self.assertEqual(link.binding_id, self.binding.pk)
        self.assertEqual(link.order_id, Order.objects.get().pk)
        # ⭐ 口径（业务方口径，orderIdView 计划）：录池单的来源单号 = **上家展示号**
        #    （夹具 `orderIdView`＝`VIEW-{orderId}` 逐字）；e25b2f5a 存的是 `orderId`（平台
        #    内部号），业务方看起来「展示号对不上」。链路键仍是 `orderId`（上一条断言）。
        self.assertEqual(
            Order.objects.get().source_order_no, "VIEW-20260928000000000201"
        )
        #: 指派单没进上家大厅 ⇒ `in_hall=False`（spec §5.2）。
        self.assertFalse(link.in_hall)
        self.assertEqual(link.last_in_at, self.now)

    def test_the_source_order_no_falls_back_to_order_id_when_no_view_number(self):
        """⭐ 回落判据（orderIdView 计划 §3）：上家没给 `orderIdView` ⇒ 回落 `orderId`。

        失败方向：回落路写成空串 / 抛错 ⇒ 本用例红；改回只存 `orderId` ⇒ 上一条钉
        展示号的用例红——两条合起来把「优先 / 回落」两档都钉死。
        """
        row = self.my_order_row(orderIdView="")
        jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([row]), now=self.now,
        )
        self.assertEqual(Order.objects.get().source_order_no, "20260928000000000201")

    def test_the_recorded_order_is_tagged_as_boost(self):
        """⭐ 修复轮 ③：录进来的单 `order_type` 写「代练」= `OrderType.BOOST`。

        与仓库里另一条同类路（`jidanpu_hall.promote_mirror` 转正）**同一致**：镜像 / 指派单
        一律按代练档。⛔ 留空会让订单页的「订单类型」是空、`grab_limits` 也只能走兜底分支。
        ⚠️ 判据钉**字面值** `"BOOST"`（除枚举外再钉一次独立契约值，E-0024）。
        """
        jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([self.my_order_row()]), now=self.now,
        )
        order = Order.objects.get()
        self.assertEqual(order.order_type, OrderType.BOOST)
        self.assertEqual(order.order_type, "BOOST")

    def test_overwide_upstream_text_is_clipped_to_the_column_width_and_still_recorded(self):
        """⭐ 修复轮 ②：超宽上游文本按**列宽**截断、**逐字**等于截断后的值，且这一单照录。

        上游给 1000 字的项目（其余字段 500 字，合成夹具）⇒ 不截断时 PostgreSQL 会以
        `value too long` 拒掉整笔录单（`Order.project_text` = **900**（D-0218；此前 300）、
        `game_role_name` / `game_account` / `game_sub_account` = 128）。
        ⚠️ 期望里的 **900 / 128 / 500 / 1000 是字面值**：它们是模型契约（或 `TextField` 无上限），
        ⛔ 不从 `_clip_for_order` / `Order._meta` 派生——派生会让「截断被删掉」时**恒绿**（E-0024）。
        """
        row = self.my_order_row(
            project="P" * 1000,
            orderContent="C" * 500,
            orderRemark="M" * 500,
            roleName="R" * 500,
            gameAcc="A" * 500,
            zgameAcc="S" * 500,
        )
        outcome = jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([row]), now=self.now,
        )
        self.assertEqual(outcome.recorded, 1)
        order = Order.objects.get()
        self.assertEqual(order.project_text, "P" * 900)
        self.assertEqual(order.game_role_name, "R" * 128)
        self.assertEqual(order.game_account, "A" * 128)
        self.assertEqual(order.game_sub_account, "S" * 128)
        #: `TextField` 列（无宽度上限）原样保留 ⇒ 截断是**按列**的，不是一刀切。
        self.assertEqual(order.content, "C" * 500)
        self.assertEqual(order.remark, "M" * 500)
        #: 链路 / 台账照落 ⇒ 截断没有把这一轮变成「没录」。
        self.assertEqual(JidanpuUpstreamOrderLink.objects.count(), 1)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind="upstream.record").count(), 1,
        )

    def test_the_record_note_is_human_and_written_once(self):
        """台账人话里要有客服名（spec §5.2 的「上家指派给 <客服>，已录进未分配池」），且**同键只一条**。"""
        row = self.my_order_row()
        jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([row]), now=self.now,
        )
        events = JidanpuSyncEvent.objects.filter(kind="upstream.record")
        self.assertEqual(events.count(), 1)
        self.assertIn("测试客服甲", events.get().reason)
        jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([row]), now=self.now.replace(hour=13),
        )
        self.assertEqual(events.count(), 1)

    # ── ② 老单门槛（spec §11-6 / A7）─────────────────────────────────────────

    def test_history_orders_are_not_recorded_but_leave_a_reason(self):
        """⭐ spec §11-6 / A7：已完成 / 已退单 / 问题单的历史行 ⇒ **零**建单 + 台账有人话原因。"""
        rows = [
            self.my_order_row(order_sta=token, order_id=f"2026092800000000030{i}")
            for i, token in enumerate(HISTORY_TOKENS)
        ]
        outcome = jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted(rows), now=self.now,
        )
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(JidanpuUpstreamOrderLink.objects.count(), 0)
        self.assertEqual(outcome.recorded, 0)
        self.assertEqual(outcome.skipped, len(HISTORY_TOKENS))
        events = JidanpuSyncEvent.objects.filter(kind="upstream.record_skipped")
        self.assertEqual(events.count(), len(HISTORY_TOKENS))
        self.assertTrue(all(event.reason for event in events))
        #: 每条原因要点明**是哪个状态**被挡住（否则运维只能猜）。
        for token in HISTORY_TOKENS:
            self.assertTrue(
                events.filter(reason__contains=token).exists(), f"{token} 的原因没点明状态",
            )

    def test_the_other_in_flight_token_is_recorded_too(self):
        """⭐ 门槛是**两个**在途状态：`代练中` 同样必须录（⛔ 只留 `待上号` 会让这一档全被拦）。"""
        jidanpu_upstream.sync_my_orders(
            binding=self.binding,
            client=self.scripted([self.my_order_row(order_sta="代练中")]),
            now=self.now,
        )
        self.assertEqual(Order.objects.get().status, OrderStatus.UNASSIGNED)

    def test_a_row_without_a_readable_status_is_not_recorded(self):
        """`orderSta` 空 / 缺失 ⇒ **判不了**在途 ⇒ 不录 + 人话台账（⛔ 不猜、⛔ 不按历史单放行）。"""
        row = self.my_order_row()
        row["orderSta"] = "   "
        jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([row]), now=self.now,
        )
        self.assertEqual(Order.objects.count(), 0)
        event = JidanpuSyncEvent.objects.get(kind="upstream.record_skipped")
        self.assertIn("orderSta", event.reason)

    def test_a_row_without_an_order_id_is_not_recorded_and_leaves_a_reason(self):
        """缺 `orderId` ⇒ 没有幂等键 ⇒ 不录（⛔ 不拿会重号的 `orderIdView` 顶替，B4）。"""
        jidanpu_upstream.sync_my_orders(
            binding=self.binding,
            client=self.scripted([{"orderIdView": "VIEW-X", "orderSta": "待上号"}]),
            now=self.now,
        )
        self.assertEqual(Order.objects.count(), 0)
        event = JidanpuSyncEvent.objects.get(kind="upstream.record_skipped")
        self.assertIn("orderId", event.reason)

    def test_an_order_whose_game_is_unknown_is_not_recorded(self):
        """游戏四级都不中 ⇒ 建不了单（`Order.game` 非空）⇒ 不录 + 请人工加别名（⛔ 不硬塞）。"""
        jidanpu_upstream.sync_my_orders(
            binding=self.binding,
            client=self.scripted([self.my_order_row(game_name="某款我们没有的游戏")]),
            now=self.now,
        )
        self.assertEqual(Order.objects.count(), 0)
        event = JidanpuSyncEvent.objects.get(kind="upstream.record_skipped")
        self.assertIn("加别名", event.reason)

    # ── ③ 钱（spec §11-25 / A9 / §15.1 + 口径修订）────────────

    def test_amounts_take_the_accept_price_and_never_the_upstream_customer_price(self):
        """⭐ spec §11-25 / A9：夹具 `recFee=88.00`、`price=120.00` ⇒ `total_price=88.00`、
        发单价 = 勾兑后的价（默认百分比 100 ⇒ 原价接手 = 88.00），且 `120` **不落到任何金额列**。"""
        jidanpu_upstream.sync_my_orders(
            binding=self.binding,
            client=self.scripted([self.my_order_row(rec_fee="88.00", price="120.00")]),
            now=self.now,
        )
        order = Order.objects.get()
        self.assertEqual(str(order.total_price), "88.00")   # 逐字等于夹具里的 recFee
        self.assertEqual(str(order.hall_price), "88.00")    # 默认 100% ⇒ 原价接手
        #: 金额列**从 `Order._meta` 派生**：将来新增一个金额列会自动进这条判据，
        #: ⛔ 不手抄那 6 个列名。⚠️ 先钉「派生集非空且含具名列」，否则筛错成空集会让循环恒真。
        money_fields = _money_field_names()
        self.assertTrue(
            {"total_price", "hall_price", "refund_fee"} <= set(money_fields), money_fields,
        )
        for field in money_fields:
            self.assertNotEqual(getattr(order, field), Decimal("120.00"), field)

    def test_the_recorded_order_price_is_blended_but_the_customer_price_is_not(self):
        """⭐ **口径修订（业务方 2026-09-29）**：上家**指派单**录进未分配池时，
        `hall_price` = `recFee × 该绑定 hall_price_percent / 100`（⛔ 不再留空）；
        `total_price` 仍照抄 `recFee`（**不受比例影响**）。

        ⚠️ 这一路与大厅线转正 `promote_upstream_mirror` **共用** `upstream_hall_price`
        ——两处各抄一份公式必然漂移（本仓反复踩的坑）。失败方向：让
        `record_assigned_order` 不传 `hall_price`（旧的「留空」口径）⇒ 本用例红。
        """
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_price_percent=50
        )
        #: ⚠️ **必须刷库**：`record_assigned_order` 读的是**传进去的那个实例**的属性，
        #: 不刷新的话它会拿夹具里那份 `hall_price_percent=100` 算（本用例会以假象通过 / 失败）。
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_price_percent, 50)   # 夹具自证
        jidanpu_upstream.sync_my_orders(
            binding=self.binding,
            client=self.scripted([self.my_order_row(rec_fee="99.99", price="120.00")]),
            now=self.now,
        )
        order = Order.objects.get()
        self.assertEqual(str(order.total_price), "99.99")   # ⛔ 比例不许影响客户价
        self.assertEqual(str(order.hall_price), "50.00")    # 99.99 × 50% = 49.995 ⇒ 50.00

    def test_a_shell_written_zero_percent_is_never_a_free_order(self):
        """⭐ 判据 ④（兜底）：shell 直写 0（绕过 `full_clean()`）⇒ 按 **100** 算，⛔ 不出免费单。"""
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_price_percent=0
        )
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_price_percent, 0)    # 夹具自证：0 真的写进去了
        jidanpu_upstream.sync_my_orders(
            binding=self.binding,
            client=self.scripted([self.my_order_row(rec_fee="88.00", price="120.00")]),
            now=self.now,
        )
        order = Order.objects.get()
        self.assertEqual(str(order.hall_price), "88.00")
        self.assertNotEqual(str(order.hall_price), "0.00")  # ⛔ 不是免费单

    def test_a_negative_accept_price_survives_the_amount_recompute(self):
        """⭐ D-0306 补充对照（评审要求）：「负价单走完金额重算**不炸**」。

        放行负价之后，负的 `hall_price` 会进打手金额算式（`基价 × 提成率`）。本用例把一张
        recFee = -5.00 的指派单录进来、建一条 MAIN 绩效行并调**真实的重算函数**，断言
        `CommissionError / AmountError` 都不出现、金额是负的 `-2.50`（-5 × 50%）。
        ⚠️ 若哪天有人在金额链上加了 `>= 0` 的下界，这里会以 `AmountError` 红——那正是
        要它红的方向（D-0015 / D-0306：负数金额是合法业务边界）。
        """
        jidanpu_upstream.sync_my_orders(
            binding=self.binding,
            client=self.scripted([self.my_order_row(rec_fee="-5.00", price="120.00")]),
            now=self.now,
        )
        order = Order.objects.get()
        taker = User.objects.create_user(username="taker-neg-price-a", password="pw-neg-a")
        StaffProfile.objects.create(
            user=taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("50.00"),
        )
        # 录单那一支已经建了一条 MAIN 行（`uniq_order_main_taker`）⇒ **改挂人**，不另建。
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        perf.user = taker
        perf.temp_amount = Decimal("0.00")
        perf.bonus_amount = Decimal("0.00")
        perf.save()
        from apps.business.performance import recompute_order_amounts

        recompute_order_amounts(perf)   # ⛔ 不许抛 AmountError / CommissionError
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("-2.50"))

    def test_a_missing_accept_price_records_the_order_with_a_zero_total(self):
        """⭐ 判据 ④（**口径修订，业务方 2026-09-29**）：`recFee` 空 ⇒ 客户价 `0.00`（照既有录单口径）
        ——⛔ **不**回落到上家 `price`（那正是红线）；**发单价也按 0 勾兑** ⇒ `hall_price = 0.00`
        （⛔ 不再留空、⛔ 不再有「请填发单价」那一档）。

        业务原话：「如果是空的，默认按照 0 算就行了」「不需要提示」。
        失败方向：把 `upstream_hall_price` 的 `None` 分支改回 `return None` ⇒ 这里读到 `None`
        ⇒ 本用例红（大厅投影 / 转正建单各有一条同族用例）。
        """
        jidanpu_upstream.sync_my_orders(
            binding=self.binding,
            client=self.scripted([self.my_order_row(rec_fee="", price="120.00")]),
            now=self.now,
        )
        order = Order.objects.get()
        self.assertEqual(order.total_price, Decimal("0.00"))
        self.assertEqual(order.hall_price, Decimal("0.00"))

    def test_a_negative_accept_price_is_recorded_as_a_negative_price(self):
        """⭐ D-0306（2026-10-26 业务方终裁）：`recFee < 0` **原值直落**（⛔ 不再折成「没给价」）。

        旧口径（ D4）把负值 fail-closed 成 `0.00`；终裁是「订单流负价放行」
        ⇒ `total_price = -5.00`、`hall_price = -5.00`（默认 100% 勾兑）。本条判据打在
        **建出来的订单行**上，与 peer 订单事件路的负价口径同源。
        """
        jidanpu_upstream.sync_my_orders(
            binding=self.binding,
            client=self.scripted([self.my_order_row(rec_fee="-5.00", price="120.00")]),
            now=self.now,
        )
        order = Order.objects.get()
        self.assertEqual(order.total_price, Decimal("-5.00"))
        self.assertEqual(order.hall_price, Decimal("-5.00"))

    def test_the_accept_price_boundary_keeps_zero_and_passes_negatives_through(self):
        """值域边界钉在**纯函数**上：负数原样返回；`0 / 正数 / -0` 照常（D-0015）。"""
        of = jidanpu_upstream._rec_fee_or_none
        self.assertEqual(of("-5.00"), Decimal("-5.00"))
        self.assertEqual(of(-5), Decimal("-5.00"))
        self.assertEqual(of("-0.01"), Decimal("-0.01"))
        #: 0 与正数照常（⚠️ 用 `is not None` 判有没有价，E-0021：0 是假值但**有**价）。
        self.assertEqual(of("0"), Decimal("0.00"))
        self.assertEqual(of("-0.00"), Decimal("0.00"))   # 归一化后等于 0，不是负数
        self.assertEqual(of("88.00"), Decimal("88.00"))
        #: 「没给价 / 认不出」两档一个字不改。
        self.assertIsNone(of(None))
        self.assertIsNone(of(""))
        self.assertIsNone(of("abc"))

    # ── ④ 幂等（spec §5.2 末段）─────────────────────────────────────────────

    def test_a_second_round_does_not_create_a_second_order(self):
        """⭐ 同一张上家单只录一次：重复跑一轮 ⇒ 订单 / 链路都还是**一张**，第二轮走 `linked` 支。"""
        row = self.my_order_row()
        jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([row]), now=self.now,
        )
        later = self.now.replace(hour=13)
        outcome = jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=self.scripted([row]), now=later,
        )
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(JidanpuUpstreamOrderLink.objects.count(), 1)
        self.assertEqual(outcome.recorded, 0)
        self.assertEqual(outcome.linked, 1)
        #: 已录过的只刷新链路拉取时刻（⛔ 不重建订单）。
        self.assertEqual(JidanpuUpstreamOrderLink.objects.get().last_in_at, later)

    def test_a_concurrent_duplicate_is_treated_as_done_not_a_second_order(self):
        """⭐ 并发兜底：另一执行者刚插入链路（查重那一跳看不见）⇒ `IntegrityError` 必须把
        **这次新建的订单一起回滚**、按「已做过」返回既有订单（⛔ 不留第二张单、⛔ 不当失败）。
        """
        row = self.my_order_row()
        first = jidanpu_upstream.record_assigned_order(
            self.binding, row, game=self.game_a, now=self.now,
        )
        link = JidanpuUpstreamOrderLink.objects.get()
        #: 第一跳查重看不见（`None`）、异常后那一跳才看得见（`link`）——正是并发的时间线。
        with mock.patch.object(
            jidanpu_upstream, "_link_for_by_remote", side_effect=[None, link],
        ):
            again = jidanpu_upstream.record_assigned_order(
                self.binding, row, game=self.game_a, now=self.now.replace(hour=13),
            )
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(JidanpuUpstreamOrderLink.objects.count(), 1)
        self.assertEqual(JidanpuSyncEvent.objects.filter(kind="upstream.record").count(), 1)

    def test_the_assigned_path_does_not_use_the_address_owner_dedupe(self):
        """⭐ A2 的去重只服务「谁负责拉大厅」：同地址**第二份**绑定被指派的单照样录进它自己的客服名下。

        ⛔ 若在这一路套上 `dedupe_owner`（同地址只认 `id` 最小者），第二份的指派单会被
        **静默丢掉**——而「上家把账号指给了谁」与「谁负责拉大厅」是两件事。
        """
        jidanpu_upstream.sync_my_orders(
            binding=self.other_binding,
            client=self.scripted([self.my_order_row()], account=self.other_binding.account),
            now=self.now,
        )
        order = Order.objects.get()
        self.assertEqual(order.sales_user_id, self.other_binding.user_id)
        self.assertEqual(order.sales_agent, "测试客服乙")
        self.assertEqual(JidanpuUpstreamOrderLink.objects.get().binding_id, self.other_binding.pk)

    def test_each_binding_records_its_own_assigned_order_for_the_same_upstream_id(self):
        """同地址两份绑定各自的账号各被指派**同一张**上家单 ⇒ **各录各的**（链路带 `binding`）。

        ⚠️ 这也是 `JidanpuUpstreamOrderLink` 两条唯一约束都带 `binding` 的落点（spec §4.2）：
        把两条链路当成「同一张单只能一份」会命中错误的去重口径。
        """
        for binding, account in (
            (self.binding, "acc-up-a"), (self.other_binding, "acc-up-b"),
        ):
            jidanpu_upstream.sync_my_orders(
                binding=binding, client=self.scripted([self.my_order_row()], account=account),
                now=self.now,
            )
        self.assertEqual(Order.objects.count(), 2)
        self.assertEqual(JidanpuUpstreamOrderLink.objects.count(), 2)
        self.assertEqual(
            set(JidanpuUpstreamOrderLink.objects.values_list("binding_id", flat=True)),
            {self.binding.pk, self.other_binding.pk},
        )

    def test_each_binding_keeps_its_own_skipped_note_for_the_same_upstream_row(self):
        """⭐ 修复轮 ①：不录台账的幂等键**必须含 `binding`**。

        上家会把**同一个 `orderId`** 派给同地址的多个账号（本线允许同地址多份绑定）⇒ 两份
        「没录」是**两件独立的事**。⛔ 键里少了 `binding`（`target_ref` + `reason` 都逐字相同），
        第二份会被第一份**静默吞掉**：台账只剩 1 行、`payload` 只指向先跑的那一份
        ⇒ 回答不了「这行归哪一份绑定」（spec §5.2 ⛔ 不静默 / §4.4 台账口径）。
        """
        row = self.my_order_row(order_sta="已完成")
        for binding, account in (
            (self.binding, "acc-up-a"), (self.other_binding, "acc-up-b"),
        ):
            jidanpu_upstream.sync_my_orders(
                binding=binding, client=self.scripted([row], account=account), now=self.now,
            )
        events = JidanpuSyncEvent.objects.filter(kind="upstream.record_skipped")
        self.assertEqual(events.count(), 2)
        self.assertEqual(
            {event.payload["upstream_binding_id"] for event in events},
            {self.binding.pk, self.other_binding.pk},
        )
        #: 再跑一轮 ⇒ 幂等：**每个绑定各自**那一句原因只留一条（两行不涨、也不互相吞）。
        for binding, account in (
            (self.binding, "acc-up-a"), (self.other_binding, "acc-up-b"),
        ):
            jidanpu_upstream.sync_my_orders(
                binding=binding, client=self.scripted([row], account=account),
                now=self.now.replace(hour=13),
            )
        self.assertEqual(events.count(), 2)
        self.assertEqual(
            {event.payload["upstream_binding_id"] for event in events},
            {self.binding.pk, self.other_binding.pk},
        )

    def test_the_whole_round_is_paged_through_not_just_the_first_page(self):
        """⛔ 别只读第一页：夹具造 21 行（> 每页固定 20）⇒ 两页都要走、21 张单都录进来。

        ⚠️ **改准（窗口拉取）**：一轮现在是**两发查询**（① 窗口 + ② 同窗口「已退单」）
        ⇒ 页号序列是 `[1, 2, 1, 2]`（每发各自翻页）；行仍是 21 行（② 与① 按 `orderId` 取并集）。
        """
        rows = [
            self.my_order_row(order_id=f"20260928000000001{i:03d}")
            for i in range(21)
        ]
        stub = self.scripted(rows)
        outcome = jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=stub, now=self.now,
        )
        self.assertEqual(
            [query["page"] for _m, _p, query in stub.calls], [1, 2, 3, 1, 2, 3],
        )
        self.assertEqual(outcome.pulled, 21)
        self.assertEqual(Order.objects.count(), 21)

    # ── ⭐ 计划 §1-①②：窗口拉取 + 退单视图并集 ────────────────────────

    def test_the_window_parameters_are_sent_verbatim(self):
        """⭐ 计划 §2-1：`stime` / `etime` **逐字**钉住（最近 24 小时、按 `now` 当刻算）。

        ⚠️ 期望值是**字面量**（⛔ 不从 `my_orders_window` 派生——自指断言不会红，E-0024）：
        `now` 是 UTC 12:00 ⇒ 业务时区（`TIME_ZONE = Asia/Shanghai`）20:00 ⇒ 窗 =
        `2026-09-27 20:00:00` ~ `2026-09-28 20:00:00`。
        ⚠️ 默认视图探完空页后查**同窗口**的「已退单」视图：`orderSta` 传**中文标签**；
        ⛔ **「全部」不是这么传的**（传中文 `全部` ⇒ 0 行）——那是「不带 `orderSta`」。
        ⚠️ **失败方向（本用例的判别力）**：窗口若用 `timezone.now()`（真实当刻）或**模块级常量**
        （import 时算一次）来算，那两个字面量就对不上（夹具的 `now` 是，与真实当刻不同）
        ⇒ 本用例红 —— 「⛔ 不写模块级常量」这条判据**就在这里**。
        """
        stub = _WindowStub(default=[self.my_order_row(order_id="20260928000000000901")])
        jidanpu_upstream.sync_my_orders(binding=self.binding, client=stub, now=self.now)
        self.assertEqual(stub.calls, [
            ("GET", MY_ORDERS_PATH, {
                "stime": "2026-09-27 20:00:00", "etime": "2026-09-28 20:00:00", "page": 1,
            }),
            ("GET", MY_ORDERS_PATH, {
                "stime": "2026-09-27 20:00:00", "etime": "2026-09-28 20:00:00", "page": 2,
            }),
            ("GET", MY_ORDERS_PATH, {
                "stime": "2026-09-27 20:00:00", "etime": "2026-09-28 20:00:00",
                "orderSta": "已退单", "page": 1,
            }),
        ])

    def test_the_refund_view_is_unioned_and_each_order_id_is_handled_once(self):
        """⭐ 计划 §2-2：② 「已退单」视图与① 按 `orderId` **取并集**；同一 `orderId` **只处理一次**
        （⛔ 不重复建单）；② 独有的那一行照门槛不录 + 一条人话。

        ⚠️ 夹具刻意让②里出现**同一个** `orderId`（真机两轮读数里退单视图要么多一行、要么与①
        逐字相同）⇒ 少了并集去重就会建**第二张**单 —— 那正是本用例要红的那一条。
        """
        shared = "20260928000000000902"
        refund_only = "20260928000000000903"
        stub = _WindowStub(
            default=[self.my_order_row(order_id=shared, order_sta="待上号")],
            refund=[
                self.my_order_row(order_id=shared, order_sta="待上号"),
                self.my_order_row(order_id=refund_only, order_sta="已退单"),
            ],
        )
        outcome = jidanpu_upstream.sync_my_orders(
            binding=self.binding, client=stub, now=self.now,
        )
        self.assertEqual(outcome.pulled, 2)      # 并集 = shared + refund_only
        self.assertEqual(outcome.recorded, 1)    # ⛔ shared 只录一次
        self.assertEqual(outcome.skipped, 1)     # refund_only 不在在途范围
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(JidanpuUpstreamOrderLink.objects.count(), 1)
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(
                kind="upstream.record_skipped", reason__contains="已退单",
            ).exists(),
        )

    def test_no_call_goes_out_without_a_window_except_the_key1_single_lookup(self):
        """⭐ 计划 §2-6：⛔ **不再有整表拉取**。

        判据：这一轮的**每一发**要么带 `stime`/`etime`（窗口①②），要么带 `key1`（③ 的单查）；
        ⛔ 不存在「既不带窗口、也不带 `key1`」的那一发 —— 那正是旧的整表拉取（1000 行硬顶的来源）。
        ⚠️ 同时钉住③ **真的走了**同一支 `_pull_row`（判据打在**数据库行**上：内容被补上了）。
        """
        remote_id = "20260928000000000904"
        order = Order.objects.create(
            game=self.game_a, order_no="20260928000000000914",
            status=OrderStatus.IN_PROGRESS, total_price=Decimal("100.00"),
            project_text="",
        )
        link = JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=order, upstream_order_id=remote_id,
            last_in_at=self.now - timedelta(hours=30),
        )
        stub = _WindowStub(
            by_key={remote_id: self.my_order_row(order_id=remote_id, order_sta="代练中")},
        )
        jidanpu_upstream.sync_my_orders(binding=self.binding, client=stub, now=self.now)
        self.assertEqual(len(stub.calls), 3)     # ① + ② + ③ 的单查
        for method, path, query in stub.calls:
            self.assertEqual((method, path), ("GET", MY_ORDERS_PATH))
            self.assertTrue(
                ("stime" in query and "etime" in query) or "key1" in query,
                f"这一发既没有时间窗、也不是 key1 单查（= 整表拉取）：{query}",
            )
        self.assertEqual(
            [query.get("key1") for _m, _p, query in stub.calls], [None, None, remote_id],
        )
        #: ③ 真的走了 `_pull_row`：内容回拉把这一格补上了（⛔ 不是「有一发请求就算」）。
        order.refresh_from_db()
        self.assertEqual(order.project_text, "合成项目")
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        link.refresh_from_db()
        self.assertEqual(link.last_in_at, self.now)


class UpstreamAssignedOrderSourceTests(TenantTestCase):
    """⭐ D-0261：上家指派单录池**自动标注「订单来源」**（计划钉 ①③④⑥）。

    ⚠️ 直打**枚举出的建单写点** `record_assigned_order`（E-0232：上家单的建单点恰好
    录池 / 转正两处；本类钉录池这一处，转正那一半在 `test_jidanpu_upstream_consume.py`）。
    ⚠️ 判据打**数据库行**（`refresh_from_db()` 之后读列，⛔ 不看内存对象）。
    ⚠️ 夹具一律**合成值**：来源名「竹屿 / 叶枫」（业务方口径里的示例字典项）、
    单号 `20261019…` 全零段。
    """

    def setUp(self):
        self.now = datetime(2026, 10, 19, 12, 0, tzinfo=dt_timezone.utc)
        self.user = User.objects.create_user(username="kefu-src-a", password=PASSWORD)
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account="acc-src-a",
            password=PASSWORD, enabled=True,
        )
        self.game = Game.objects.create(name="测试游戏甲")

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def source_row(self, *, order_id: str) -> dict:
        """一行「上家指派给我」的合成行（形状照 `UpstreamAssignedOrderRecordTests`）。"""
        return {
            "orderId": order_id,
            "orderIdView": f"VIEW-{order_id}",
            "orderSta": "待上号",
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

    def record_one(self, *, order_id: str = "20261019000000000201") -> Order:
        return jidanpu_upstream.record_assigned_order(
            self.binding, self.source_row(order_id=order_id), game=self.game, now=self.now,
        )

    def attach_source(self, name: str) -> OrderSource:
        source = OrderSource.objects.create(name=name)
        self.binding.order_source = source
        self.binding.save(update_fields=["order_source"])
        return source

    # ── 钉① / ③ / ④ / ⑥ ────────────────────────────────────────────────

    def test_pin1_the_recorded_order_carries_the_bindings_source_and_name(self):
        """⭐ 钉①：配了来源的绑定走录池 ⇒ 本地单 `order_source` + `source` 名字快照都带上。

        修前红：绑定表还没有 `order_source` 列（`save(update_fields=…)` 抛 `FieldError`）。
        """
        source = self.attach_source("竹屿")
        order = self.record_one()
        order.refresh_from_db()
        self.assertEqual(order.order_source_id, source.pk)
        self.assertEqual(order.source, "竹屿")

    def test_pin3_a_binding_without_a_source_records_orders_with_no_source(self):
        """⭐ 钉③：未配来源 ⇒ 两列照旧全空（fail-soft：不挡录单、⛔ 不回落到别的来源）。"""
        self.assertIsNone(self.binding.order_source)
        order = self.record_one()
        order.refresh_from_db()
        self.assertIsNone(order.order_source_id)
        self.assertEqual(order.source, "")

    def test_pin4_deleting_the_source_keeps_old_snapshot_and_later_orders_empty(self):
        """⭐ 钉④：字典项被删（两处 `SET_NULL`）⇒ 已有单**保留名字快照**、后续单两列全空。

        ⚠️ `Order.order_source` 本身也是 `SET_NULL` ⇒ 删除后**已有单的 FK 同样置空**；
        「已有单不受影响」落地的正是 `source` 那格**文本快照**——快照列就是为这一刻存在的。
        """
        self.attach_source("叶枫")
        order_a = self.record_one(order_id="20261019000000000201")
        OrderSource.objects.get(name="叶枫").delete()
        self.binding.refresh_from_db()
        self.assertIsNone(self.binding.order_source, "绑定行的 FK 随删除置空（SET_NULL）")
        order_b = self.record_one(order_id="20261019000000000202")
        order_a.refresh_from_db()
        self.assertEqual(order_a.source, "叶枫", "已有单的名字快照必须留下")
        self.assertIsNone(order_a.order_source_id)
        self.assertIsNone(order_b.order_source_id)
        self.assertEqual(order_b.source, "", "删除之后的单照旧无来源")

    def test_pin6_each_snapshot_is_the_name_at_its_own_record_time(self):
        """⭐ 钉⑥：`source` = **当时**的字典项名——改名不追溯、每单各拍各的快照。

        ⚠️ 上家路没有「换绑」形态 ⇒ 与手工录单 `source_snapshot_overrides` 的
        「同一 FK 不重拍名字」**不是**一回事（⛔ 不要把两条口径合并）。
        """
        source = self.attach_source("旧名甲")
        order_a = self.record_one(order_id="20261019000000000201")
        source.name = "新名乙"
        source.save(update_fields=["name"])
        order_b = self.record_one(order_id="20261019000000000202")
        order_a.refresh_from_db()
        order_b.refresh_from_db()
        self.assertEqual(order_a.source, "旧名甲")
        self.assertEqual(order_b.source, "新名乙")


class MyOrdersWindowTests(SimpleTestCase):
    """⭐ 计划 §2：算窗口的**纯函数** `my_orders_window`（值 / 格式 / 「按调用当刻算」）。"""

    def test_the_window_is_the_last_n_hours_in_the_business_timezone(self):
        """`now` 是 aware（UTC）⇒ 先转业务时区再格式化（⛔ 直接拿 UTC 会少 8 小时的新单）。"""
        self.assertEqual(
            jidanpu_upstream.my_orders_window(
                now=datetime(2026, 9, 28, 12, 0, tzinfo=dt_timezone.utc), hours=24,
            ),
            {"stime": "2026-09-27 20:00:00", "etime": "2026-09-28 20:00:00"},
        )

    def test_the_window_is_computed_from_the_injected_instant(self):
        """⭐ 纯函数的值判据：换一个 `now` ⇒ 窗跟着挪。

        ⚠️ ⛔ **本条不是**「不写模块级常量」的判据：函数**显式收 `now`** ⇒ 就算实现里另外
        缓存了一份 import 时算的常量，本条照样绿（那种假保证比不写更糟，E-0011）。
        那条判据在 `…the_window_parameters_are_sent_verbatim`（期望值是**字面量**，
        而夹具的 `now` 是 2026-09-28、与跑测试的真实当刻不同）。
        """
        self.assertEqual(
            jidanpu_upstream.my_orders_window(
                now=datetime(2026, 12, 31, 18, 30, 15, tzinfo=dt_timezone.utc), hours=24,
            ),
            {"stime": "2026-12-31 02:30:15", "etime": "2027-01-01 02:30:15"},
        )
