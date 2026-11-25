"""下游鸡蛋铺线 **Task A**：`status_pull_at` 水位列 + 「按完成时间拉已完成」的取数函数。

被钉住的契约（plan `2026-10-03-member-status-scope` §1.1 / §4 Task A）：

- `JidanpuMemberBinding.status_pull_at` 是**内部状态**：「上次**成功**拉完并把结果应用完的时刻」，
  下一次取数窗 = `max(1 天, now − 本列)`（窗口那一段由 Task B 实现）
  ⇒ ⛔ 不进序列化器、⛔ 不进 `_PUT_WRITE_FIELDS`、⛔ 不上页面（本文件钉前两条；
  第三条没有后端判据——该列表在 `src/frontend/**`，不属本任务）。
- `jidanpu.fetch_completed_orders_page`：一页「**已完成**」的单，时间维度是**完成时间**
  （`timeType=30`，`/Order/orderlist` 的 `<select id="dateType">` 原文 = `30 完成时间`，
  见 `docs/JIDANPU-API.md` §4.1）——⛔ 不是录单时间（`10`，`ORDER_LIST_KEY1_MANDATORY_PARAMS`
  的默认值，那条单查路径一个字都不许动）。
- 参数模板**只此一处**：本函数走既有的 `_order_list_search_params`（含抓包里那 20 个空筛选键
  与 9 个检索默认值），⛔ 不另拼第二份。
- 回包形状与 `fetch_reconcile_orders_page` **同形**：`OrderListPage(rows, count)`，
  且 `count` 沿用那条 fail-closed 口径（没有可用的 `count` ⇒ `None`，⛔ 不许拿它比大小）。

⚠️ 桩的形状照 `tests/test_jidanpu_reconcile.py` 的 `_OrderListStub`（`client.call` +
`client.last_envelope` 两个出口），⛔ 不另创一套假 client 形状。
⚠️ 期望值一律**硬编码字面量**（`30` / `已完成` / `1000` / 键名清单），
⛔ 不从被测常量派生（自指断言在被断言的常量被改坏时不会红，E-0024 / E-0065）。
⚠️ 夹具一律**合成值**（E-0077）、零真实网络。
"""
from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase
from jidanpu.errors import ApiError

from apps.business import jidanpu
from apps.business.api import jidanpu_member_views
from apps.business.models import JidanpuMemberBinding
from tests.p5_auth_helpers import make_logged_in_client
#: 整表参数集那一条判据的期望值**不在这里另抄一份**：直接引大厅那份**独立硬编码**的字面量
#: （`test_jidanpu_hall_client` 里为的就是「⛔ 不从被测常量派生」，E-0065 / E-0024）。
from tests.test_jidanpu_hall_client import (
    HALL_EMPTY_FILTER_KEYS_LITERAL,
    HALL_SEARCH_DEFAULTS_LITERAL,
)

# ── 合成夹具值（E-0077）─────────────────────────────────────────────────────────

MEMBER_ACCOUNT = "member-watermark-a"
BASE_URL = "https://jidanpu-watermark-a.test.invalid"
PASSWORD = "pw-canary-watermark-a"
REMOTE_ID = "20261003000000000001"
VIEW_NO = "JDP-VIEW-0000000000000001"

#: 取数窗（合成值；窗本身怎么算由 Task B 钉，这里只钉它**被原样发出去**）。
WINDOW = {"stime": "2026-10-02 00:00:00", "etime": "2026-10-03 12:00:00"}

#: ⭐ 期望值一律字面量（⛔ 不从 `jidanpu.ORDER_TIME_TYPE_COMPLETED` / `ORDER_LIST_PAGE_SIZE`
#: 派生）：派生出来的断言在常量被改坏时**不会红**，等于没钉（E-0024）。
TIME_TYPE_COMPLETED = 30
ORDER_STA_COMPLETED = "已完成"
PAGE_LIMIT_LITERAL = 1000
#: `GetOrderList` 的 **8 个每次必带参数**（取证位置 = `docs/JIDANPU-API.md` §4.1 的页面原文清单；
#: 漏传是「静默返回空集」，与「真的没有单」同形）。
MANDATORY_PARAM_KEYS_LITERAL = (
    "page", "limit", "stime", "etime", "minPrice", "maxPrice", "gameName", "timeType",
)

#: 绑定端点（伪造列那一组用例要用）。
BINDING_URL = "/api/business/member/jidanpu-binding/"


def _row(order_id: str = REMOTE_ID, status: str = ORDER_STA_COMPLETED) -> dict:
    """一行**真实形状**的订单列表行（字段名取 `docs/JIDANPU-API.md` §7 的字段分组；合成值）。"""
    return {
        "orderId": order_id,
        "orderIdView": VIEW_NO,
        "orderSta": status,
        "isThrowOrder": "N",
        "gameName": "测试游戏甲",
        "project": "合成项目甲",
        "roleName": "合成角色甲",
        "recFee": 0.0,
        "createTime": "2026-10-02 10:00:00",
        "finishTime": "2026-10-03 09:00:00",
    }


class _OrderListStub:
    """`POST /OrderAjax/GetOrderList` 的替身（形状照 `test_jidanpu_reconcile._OrderListStub`）。

    ⛔ 不另创一套假 client：本函数只跟 `client.call(...)` 与 `client.last_envelope`
    两个出口打交道（换形状的后果是「测试自己造了一个不存在的客户端」）。
    `count` 由夹具**显式给**（替身自己算 `len(rows)` 就永远对得上 ⇒ 那几条用例会恒真）。
    """

    def __init__(self, *, rows=None, count: int = 0, envelope=None):
        self.rows: list[dict] = list(rows or [])
        self.count = count
        #: `None` = 用 `{"code": 0, "msg": "success", "count": self.count}`；
        #: 显式给一个 dict ⇒ 原样当回包壳（用来构造「没有可用的 count」）。
        self.envelope = envelope
        self.calls: list[dict] = []
        self.last_envelope: dict = {}

    def call(self, method, path, **kwargs):
        assert path == jidanpu.ORDER_LIST_PATH, f"未预期的端点：{method} {path}"
        self.calls.append(dict(kwargs.get("data") or {}))
        self.last_envelope = (
            self.envelope if self.envelope is not None
            else {"code": 0, "msg": "success", "count": self.count}
        )
        return [dict(row) for row in self.rows]


class FetchCompletedOrdersPageTests(SimpleTestCase):
    """`jidanpu.fetch_completed_orders_page` 的参数与回包形状（纯函数，零 DB、零网络）。"""

    # ── 参数：只看已完成、按完成时间框窗 ───────────────────────────────────────

    def test_the_page_asks_for_completed_orders_only(self):
        """⭐ 筛选值 = 中文标签 `已完成`（本端点的 `orderSta` 就是中文名本身）。

        失败方向：写成 `WFP` 那种码 / 漏掉这一键 ⇒ 本用例红（漏键的后果是**静默返回空集**）。
        """
        stub = _OrderListStub(rows=[_row()], count=1)
        jidanpu.fetch_completed_orders_page(stub, window=WINDOW)
        sent = stub.calls[0]
        self.assertEqual(sent.get("orderSta"), ORDER_STA_COMPLETED)

    def test_the_page_frames_the_window_by_completion_time(self):
        """⭐⭐ 时间维度 = **完成时间**（`timeType=30`），⛔ 不是录单时间（`10`）。

        出处 = 对标页 `/Order/orderlist` 的 `<select id="dateType">` 原文
        （`docs/JIDANPU-API.md` §4.1：`10 录单时间` / `30 完成时间`）+ 2026-10-03 真机实测
        （plan §2：1 天窗 + 已完成 = 1,279 行，深页 150 行里 100 行**录单时间在窗外**）。

        ⚠️ 类型口径照既有模板：上游要的是**数字** `30`（`ORDER_LIST_KEY1_MANDATORY_PARAMS`
        里 `timeType: 10` 就是 int）⇒ 字符串 `"30"` 不算通过。
        失败方向：沿用模板默认的 `10`（= 录单时间）⇒ 本用例红。
        """
        stub = _OrderListStub(rows=[_row()], count=1)
        jidanpu.fetch_completed_orders_page(stub, window=WINDOW)
        sent = stub.calls[0]
        self.assertEqual(sent.get("timeType"), TIME_TYPE_COMPLETED)
        self.assertIsInstance(sent.get("timeType"), int)
        self.assertNotIsInstance(sent.get("timeType"), bool)
        self.assertNotIsInstance(sent.get("timeType"), str)
        # 窗本身原样发出（语义是完成时间由上面那一键承载）。
        self.assertEqual(sent.get("stime"), WINDOW["stime"])
        self.assertEqual(sent.get("etime"), WINDOW["etime"])

    def test_the_page_carries_the_page_number_and_the_measured_page_size(self):
        """`page` / `limit` 由调用方给；不传时 `limit` 用那个**实测过**的页大小，`page=1`。"""
        stub = _OrderListStub(rows=[_row()], count=1)
        jidanpu.fetch_completed_orders_page(stub, window=WINDOW)
        self.assertEqual(stub.calls[0].get("page"), 1)
        self.assertEqual(stub.calls[0].get("limit"), PAGE_LIMIT_LITERAL)

        stub2 = _OrderListStub(rows=[_row()], count=1)
        jidanpu.fetch_completed_orders_page(stub2, window=WINDOW, page=3, limit=250)
        self.assertEqual(stub2.calls[0].get("page"), 3)
        self.assertEqual(stub2.calls[0].get("limit"), 250)

    def test_the_page_carries_every_mandatory_param(self):
        """⭐ 8 个必带参数一个都不能少（漏传 = **静默返回空集**，与「真的没有单」同形）。

        ⚠️ 期望的键名是**独立字面量**（取自 `docs/JIDANPU-API.md` §4.1 的原文清单），
        ⛔ 不遍历被测常量（那样改坏了也不红）。"""
        stub = _OrderListStub(rows=[_row()], count=1)
        jidanpu.fetch_completed_orders_page(stub, window=WINDOW)
        sent = stub.calls[0]
        for name in MANDATORY_PARAM_KEYS_LITERAL:
            self.assertIn(name, sent, name)
        self.assertEqual(sent.get("gameName"), "全部游戏")
        self.assertEqual(sent.get("key1"), "")

    def test_the_page_carries_the_full_hall_param_set(self):
        """⭐ 抓包里那 20 个空筛选键 + 9 个检索默认值都要在（与整表对账**共用**同一份模板）。

        失败方向：本函数另拼一份「精简参数」⇒ 本用例红（少键的后果同样是静默空集）。"""
        stub = _OrderListStub(rows=[_row()], count=1)
        jidanpu.fetch_completed_orders_page(stub, window=WINDOW)
        sent = stub.calls[0]
        for name in HALL_EMPTY_FILTER_KEYS_LITERAL:
            self.assertEqual(sent.get(name), "", name)
        for name, value in HALL_SEARCH_DEFAULTS_LITERAL.items():
            self.assertEqual(sent.get(name), value, name)

    # ── 回包形状：与 `fetch_reconcile_orders_page` 同形 ───────────────────────

    def test_rows_and_count_come_from_the_order_list_page(self):
        """一页 = `OrderListPage(rows=<元组>, count=<壳里的 count>)`（同形，⛔ 不是别的容器）。"""
        stub = _OrderListStub(rows=[_row()], count=1)
        page = jidanpu.fetch_completed_orders_page(stub, window=WINDOW)
        self.assertIsInstance(page, jidanpu.OrderListPage)
        self.assertIsInstance(page.rows, tuple)
        self.assertEqual(page.rows, (_row(),))
        self.assertEqual(page.rows[0]["orderId"], REMOTE_ID)
        self.assertEqual(page.rows[0]["orderSta"], ORDER_STA_COMPLETED)
        self.assertEqual(page.count, 1)

    def test_an_empty_page_is_a_legitimate_result(self):
        """真的没有单 ⇒ `count=0` + 空元组（⛔ 不是 `None`、⛔ 不抛——`0` 是**合法**值）。"""
        page = jidanpu.fetch_completed_orders_page(
            _OrderListStub(rows=[], count=0), window=WINDOW
        )
        self.assertEqual(page.count, 0)
        self.assertEqual(page.rows, ())

    def test_a_negative_envelope_count_reads_as_unusable(self):
        """⭐ 负数不是条数 ⇒ `None`（放行会让「累计行数 ≥ count」**恒真** ⇒ 半份数据判成拉全）。

        失败方向：去掉那道 `count < 0` ⇒ 本用例红（`page.count` 会是 `-1`）。"""
        page = jidanpu.fetch_completed_orders_page(
            _OrderListStub(rows=[_row()], count=-1), window=WINDOW
        )
        self.assertIsNone(page.count)
        self.assertEqual(len(page.rows), 1)

    def test_a_missing_or_boolean_count_reads_as_none(self):
        """壳里没有 `count` / `count: true` ⇒ `None`（判不了拉全，⛔ 不许拿它比大小）。"""
        for envelope in ({"code": 0, "msg": "success"}, {"code": 0, "count": True}):
            with self.subTest(envelope=envelope):
                page = jidanpu.fetch_completed_orders_page(
                    _OrderListStub(rows=[_row()], envelope=envelope), window=WINDOW
                )
                self.assertIsNone(page.count)
                self.assertEqual(len(page.rows), 1)

    def test_a_malformed_payload_is_loud(self):
        """载荷不是列表 ⇒ 响亮抛（形状异常与「真的没有单」不许同形，`_as_rows` 的口径）。"""
        class _NotAList:
            last_envelope = {"code": 0, "msg": "success", "count": 0}

            def call(self, method, path, **kwargs):
                return {"rows": []}

        with self.assertRaises(ApiError):
            jidanpu.fetch_completed_orders_page(_NotAList(), window=WINDOW)


class MemberStatusPullWatermarkColumnTests(TenantTestCase):
    """⭐ 水位列是**内部状态**：库里真有这一列，而 API 两头都摸不到它。"""

    def test_the_column_exists_on_the_model_and_in_the_database(self):
        """两份读数：模型侧（`_meta`）+ 库侧（introspection）——光看模型挡不住「迁移没跑」。"""
        field = JidanpuMemberBinding._meta.get_field("status_pull_at")
        self.assertTrue(field.null, "空 = 还没成功拉过一轮")
        self.assertTrue(field.blank)
        with connection.cursor() as cursor:
            columns = {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor, JidanpuMemberBinding._meta.db_table
                )
            }
        self.assertIn("status_pull_at", columns)
        # 哨兵：这条读的确实是那张表（否则一个空集合上的「含」也不可能成立，但先钉住出口）。
        self.assertIn("reconcile_seconds", columns)

    def test_the_column_is_not_in_the_serializer_nor_in_the_put_write_list(self):
        """⛔ 不进序列化器字段清单、⛔ 不进 `_PUT_WRITE_FIELDS`（两处都在视图模块里）。

        ⚠️ 每半条都带一个**哨兵**：没有哨兵时，「不在某个空集合里」是恒真的。
        """
        serializer_fields = set(
            jidanpu_member_views.JidanpuMemberBindingSerializer().fields
        )
        self.assertNotIn("status_pull_at", serializer_fields)
        self.assertIn("reconcile_seconds", serializer_fields)

        self.assertNotIn("status_pull_at", jidanpu_member_views._PUT_WRITE_FIELDS)
        self.assertIn("reconcile_seconds", jidanpu_member_views._PUT_WRITE_FIELDS)

    def test_a_forged_status_pull_at_in_the_request_body_is_ignored(self):
        """终态判据：请求体里塞 `status_pull_at` ⇒ 库里那一格仍是 `None`、响应里也没有这个键。

        失败方向：把它加进序列化器 `fields` ⇒ 本用例红（用户能把内部水位伪造 / 清掉）。
        """
        client = make_logged_in_client(username=MEMBER_ACCOUNT)
        payload = {
            "jidanpu_base_url": BASE_URL,
            "account": "测试账号甲",
            "password": PASSWORD,
            "source_name": "团团通系统",
            "enabled": False,
            "status_pull_at": "2026-10-03T00:00:00Z",
        }
        response = client.put(
            BINDING_URL, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.json()["code"], 0, response.content)
        row = JidanpuMemberBinding.objects.get(user=User.objects.get(username=MEMBER_ACCOUNT))
        self.assertIsNone(row.status_pull_at)
        data = response.json()["data"]
        self.assertNotIn("status_pull_at", data)
        # 哨兵：响应里确实有配置列（否则「不含」是在一个空 dict 上成立的）。
        self.assertIn("reconcile_seconds", data)
