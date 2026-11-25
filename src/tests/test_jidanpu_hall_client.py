"""`apps.business.jidanpu` 的**P2 加法**：10 个上游操作 + `key1` 的逐字校验（零网络）。

⚠️ 桩打在**被查找的那个模块**上（`AGENTS.md` §5-E / E-0029）：领域层用
`apps.business.jidanpu.open_client`，所以这里 patch 的就是它。
⚠️ 文件尾另有一组把桩下移到 **HTTP 边界**（`_StubHttp` + 真 `JidanpuClient`）的用例
⇒ SDK `0.1.1` 真的判一次成败，这是 **E-0087**（写端点的失败信封）的落点。
⚠️ 本文件**不重写 SDK 自己的测试**：登录表单 / cookie 会话 / 会话失效重登归上游。
这里只回答四件事：**调哪个端点、带哪些参数、`key1` 结果怎么校验、失败时抛什么**。
⚠️ 夹具一律**合成值**（E-0077）：uid 用 `100001` / `100002` 这类显眼值、游戏 ID 用 `21700`、
条数用 `42`、单号用 `JDP-…` / `SYNTH-SRC-…`、上游回包文案写成「合成…」。
⛔ **不抄 `.tools/recon/p2/` 的实测值**——评审轮 1 点出过：本文件曾把几个 recon 里能找到
原始出处的值（内部单键 / 回包文案 / 游戏 ID / 人员 uid）抄成夹具。⚠️ 这里**不复述那几个值**
（复述等于又把它们写回 `src/**`，而真值守卫正是扫 `src/**`）。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import httpx
from django_tenants.test.cases import TenantTestCase
from jidanpu.client import JidanpuClient as _RealJidanpuClient
from jidanpu.errors import ApiError, ConfigError

from apps.business import jidanpu

BASE_URL = "https://jidanpu-a.test.invalid"
ACCOUNT = "bot-account-a"
PASSWORD = "pw-canary-a"
SYNTH_SRC = "SYNTH-SRC-00000001"
REMOTE_ORDER_ID = "20260925000000000901"
#: 录单端点**成功回包**里的内部单键 `oid`（合成值；⛔ 不抄 recon 实测的那个）。
REMOTE_OID = 73000123
#: 窗口钉子用的**固定当天**（注入用；⛔ 不用 `date.today()`——那样断言会随日历漂）。
TODAY = date(2026, 9, 25)
#: `TODAY - ORDER_LIST_LOOKBACK_DAYS`（90 天）= 2026-06-27；`TODAY + 1` = 2026-09-26。
WINDOW_START = "2026-06-27 00:00:00"
WINDOW_END = "2026-09-26 23:59:59"

#: ⭐ 加固轮 T1：`HALL_ORDER_SEARCH_DEFAULTS` 的 **9 个键值**，在测试里**逐字硬编码**
#: （⛔ 不从被测常量派生期望，E-0065 / E-0024）。
#: 为什么必须独立：只与常量自比时，把价格窗 `seachMutPrice2` 从 `99999999` 改小 ⇒
#: **价格窗被收窄** ⇒ 服务端按价静默过滤、`count` 变小 ⇒ 轮次仍判「拉全」⇒
#: **可能把还在厅里的单误撤**（比「缺键」更险：它不报错、只是结果变少）。
HALL_SEARCH_DEFAULTS_LITERAL: dict[str, object] = {
    "showPriceType": 0,
    "selectSeach1": 0,
    "key4Type": 0,
    "bussType": 0,
    "kefuType": 0,
    "seachMutPrice1": -999999,
    "seachMutPrice2": 99999999,
    "seachMutPrice3": -999999,
    "seachMutPrice4": 99999999,
}

#: ⭐ 加固轮 T2：`HALL_ORDER_EMPTY_FILTER_KEYS` 的 **20 个字面键名**（同上，独立来源）。
#: 为什么必须独立：只遍历同一个常量时，把 `orderLabel` 改成 `orderLable` ⇒ 真键缺失、
#: 请求里多一个错键，而服务端对**漏传不报错、静默返回空集**（本模块实测硬限）
#: ⇒ 与「大厅真的空了」同形（静默少单）。
HALL_EMPTY_FILTER_KEYS_LITERAL: tuple[str, ...] = (
    "key1", "key2", "key3", "key4",
    "gameArea", "gameServer", "gameJob", "project",
    "recUserId", "customerServiceUserId", "orderSoure", "setSta",
    "groupName", "isJiaji", "isLeakOrder", "operatorName",
    "projectTypeName", "batchOrderId", "keytid", "orderLabel",
)


def _span_days(window: dict) -> int:
    """窗口的**跨度天数**（含首尾）= `etime` 那天 − `stime` 那天 + 1。"""
    start = datetime.strptime(window["stime"], "%Y-%m-%d %H:%M:%S").date()
    end = datetime.strptime(window["etime"], "%Y-%m-%d %H:%M:%S").date()
    return (end - start).days + 1


class _FakeClient:
    """`JidanpuClient` 的可控替身：只记「被怎么调用」，并按 `replies` 依次回包。"""

    def __init__(self, replies=None):
        self.calls: list[tuple[str, str, dict]] = []
        self._replies = list(replies or [])
        self.last_envelope: dict = {}

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        reply = self._replies.pop(0) if self._replies else []
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, tuple):  # (payload, envelope)
            payload, envelope = reply
            self.last_envelope = dict(envelope)
            return payload
        self.last_envelope = {"code": 0, "msg": "success", "data": reply}
        return reply


@contextmanager
def _client_context(client):
    yield client


class _StubHttp:
    """假的 `httpx.Client`（**HTTP 边界**的桩）：登录回 `code=1` + 会话 cookie，
    其余请求按 `envelopes` 脚本依次回包。

    ⚠️ 与 `_FakeClient` 的分工：那个桩在 `open_client` 上、只证明「我们怎么调」；
    这个桩在 HTTP 边界上，让 SDK 0.1.1 真的跑一遍 `Transport.unwrap_response`
    ⇒ **成败判据仍然只有 SDK 那一个落点**（spec §5.7 / D15 / E-0087）。
    """

    def __init__(self, envelopes):
        self.cookies = httpx.Cookies()
        self.envelopes = list(envelopes)
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        request = httpx.Request(method, url)
        if url.endswith("/Login/Login"):
            # 登录成功 = 严格 `code == 1` **且** jar 里真的拿到会话 cookie。
            self.cookies.set("sl-session", "synth-session-a")
            return httpx.Response(
                200, json={"code": 1, "msg": "ok", "data": "ok"}, request=request
            )
        payload = self.envelopes.pop(0)
        if isinstance(payload, str):
            return httpx.Response(200, text=payload, request=request)
        return httpx.Response(200, json=payload, request=request)

    def close(self):
        return None


def _real_client_with(stub):
    """把 `open_client` 里的 `JidanpuClient(config, http_client=…)` 换成「真 SDK + 假 HTTP 边界」。

    ⚠️ `open_client` 现在**自己**建 httpx 客户端（挂执行租约心跳钩子， 3 / B6）
    ⇒ 这里要吞掉它多传的 `http_client=` 关键字（本档的判据在配置与载荷上，不在心跳）。
    """
    return lambda config, **kwargs: _RealJidanpuClient(config, http_client=stub)


#: 4 个**写**端点：`(名字, 调用, 端点路径)`。失败信封必须由 SDK 的 `positive_code` 判失败。
#: ⚠️ 写成函数、⛔ 不做模块级常量表：模块级取 `jidanpu.ADD_ORDER_PATH` 会让「实现还没落地」
#: 的那一版直接**收集失败**，掩盖掉「每条用例各自红在哪」。
def _write_operations():
    return (
        (
            "add_order",
            lambda c: jidanpu.add_order(
                c,
                form_payload={"souresOrderID": SYNTH_SRC, "orderType": "0"},
                top_level={"gameName": "测试游戏甲"},
                source_order_no=SYNTH_SRC,
            ),
            jidanpu.ADD_ORDER_PATH,
        ),
        (
            "throw_order",
            lambda c: jidanpu.throw_order(
                c, remote_order_id=REMOTE_ORDER_ID, fee=Decimal("12.34")
            ),
            jidanpu.THROW_PATH,
        ),
        (
            "assign_order",
            lambda c: jidanpu.assign_order(
                c, remote_order_id=REMOTE_ORDER_ID, user_id="100001",
                user_name="测试员工甲", fee=Decimal("0"), hours="720",
                project="测试项目",
            ),
            jidanpu.ASSIGN_PATH,
        ),
        (
            "clear_order_taker",
            lambda c: jidanpu.clear_order_taker(c, remote_order_id=REMOTE_ORDER_ID),
            "/OrderAjax/CleanOrderZhiPaiByIDX",
        ),
        (
            "grab_order",
            lambda c: jidanpu.grab_order(c, remote_order_id=REMOTE_ORDER_ID),
            jidanpu.GRAB_PATH,
        ),
        (
            "set_order_status",
            lambda c: jidanpu.set_order_status(
                c, remote_order_id=REMOTE_ORDER_ID,
                status_label=jidanpu.ORDER_REFUNDED_LABEL, remark="",
            ),
            jidanpu.SET_STATUS_PATH,
        ),
    )


class JidanpuHallClientTests(TenantTestCase):
    def test_recorder_lookup_keeps_exact_order_key(self):
        client = _FakeClient([([], {"code": 0, "count": 0})])
        self.assertIsNone(jidanpu.fetch_order_recorded_by(client, remote_order_id=REMOTE_ORDER_ID, user_id="100001"))
        params = client.calls[0][2]["data"]
        self.assertEqual(params["key1"], REMOTE_ORDER_ID)
        self.assertEqual(params["customerServiceUserId"], "100001")
        self.assertEqual(params["kefuType"], 5)

    def setUp(self):
        # ⚠️ 夹具用 `SimpleNamespace` 而不查库：租户级鸡蛋铺线的绑定表已随该线整条拆除
        # （计划 §2.2 / §10.4）。换得掉的理由：`jidanpu.py` 本来就是**鸭子类型**的——
        # `credentials_of` 只读 `base_url` / `account` / `password` 三个属性、⛔ 不查库。
        self.binding = SimpleNamespace(
            base_url=BASE_URL, account=ACCOUNT, password=PASSWORD,
            account_uid="100001", account_name="测试员工甲", source_name="",
        )

    def _patch(self, client):
        return mock.patch.object(
            jidanpu, "open_client", lambda binding: _client_context(client)
        )

    # ── 建连 ────────────────────────────────────────────────────────────────

    def test_open_client_raises_config_error_when_credentials_are_incomplete(self):
        self.binding.password = ""
        with self.assertRaises(ConfigError):
            with jidanpu.open_client(self.binding):
                pass

    def test_open_client_builds_the_sdk_config_from_the_binding_row(self):
        """凭据只从**本租户那一行**取（S3）。

        ⚠️ `JidanpuClient` 是**惰性登录**的（`__init__` 不发请求，首次 `call()` 才登录）
        ⇒ 本用例只钉「配置来自哪一行」，登录那一跳归 SDK 自己的测试。
        """
        with jidanpu.open_client(self.binding) as client:
            self.assertEqual(client.config.base_url, BASE_URL)
            self.assertEqual(client.config.account, ACCOUNT)

    # ── 单查 / 慢路取数（⭐ D-0366：大厅整厅支已删；本节钉**幸存的**共享参数集与窗口）──

    def _hall_request(self, client):
        """取那一次**带 `orderSta` 筛选**的 `ORDER_LIST_PATH` 请求体（慢路对账那一支）。

        ⚠️ 单查（key1）不带 `orderSta`、慢路带 ⇒ 靠请求体里有没有 `orderSta` 认。
        """
        for _method, path, kwargs in client.calls:
            data = kwargs.get("data") or {}
            if path == jidanpu.ORDER_LIST_PATH and "orderSta" in data:
                return data
        raise AssertionError(f"没有慢路请求：{client.calls}")

    def test_reconcile_page_posts_the_measured_filters(self):
        """⭐ U1（D-0366 改准）：慢路对账打 **`POST /OrderAjax/GetOrderList`**，核心筛选是
        `orderSta=<中文标签>` + `gameName='全部游戏'`，且依赖的筛选键**一律显式给值**。

        ⚠️ 为什么空串键也要逐个钉：本模块的实测硬限是「**漏传不报错、静默返回空集**」
        （spec §17.6.1）——漏一个键就是「那一页静默回 0 行」，与「真的没有单」**同形**
        （`AGENTS.md` §5-E / E-0004）。⭐ 大厅整厅支已随 D-0366 删除（它原是这组键的
        另一个消费者）⇒ **慢路对账是这组键今天的唯一活消费者**，本用例就是那面镜子。
        失败方向：去掉 `orderSta`、或去掉任意一个空串筛选键 ⇒ 本用例红。
        """
        client = _FakeClient(replies=[([{"orderId": "JDP-1"}], {"code": 0, "count": 42})])
        window = {"stime": "2026-08-01 00:00:00", "etime": "2026-09-01 23:59:59"}
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                page = jidanpu.fetch_reconcile_orders_page(
                    c, status_label="代练中", window=window, page=2, limit=50,
                )
        self.assertEqual(client.calls[0][0], "POST")
        self.assertEqual(client.calls[0][1], jidanpu.ORDER_LIST_PATH)
        sent = self._hall_request(client)
        self.assertEqual(sent["orderSta"], "代练中")
        self.assertEqual(sent["gameName"], "全部游戏")
        self.assertEqual(sent["timeType"], 10)
        self.assertEqual(
            sent["minPrice"], jidanpu.ORDER_LIST_KEY1_MANDATORY_PARAMS["minPrice"]
        )
        self.assertEqual(
            sent["maxPrice"], jidanpu.ORDER_LIST_KEY1_MANDATORY_PARAMS["maxPrice"]
        )
        # 「价格窗给足」的独立判据（⛔ 不只是与常量自指相等）：它真的横跨 0。
        self.assertLess(sent["minPrice"], 0)
        self.assertGreater(sent["maxPrice"], 0)
        self.assertEqual(sent["page"], 2)
        self.assertEqual(sent["limit"], 50)
        # 调用方给的窗**逐字透传**（慢路的 180 天窗由调用方算，本层不改写）。
        self.assertEqual(sent["stime"], window["stime"])
        self.assertEqual(sent["etime"], window["etime"])
        # 依赖的筛选键一个不落地**显式给空串**。
        # ⭐ 加固轮 T2：期望是**测试里的字面量**（⛔ 不从 `HALL_ORDER_EMPTY_FILTER_KEYS` 派生）——
        #    只遍历同一个常量时，改一个键名（如 `orderLabel` → `orderLable`）请求与断言会**一起**
        #    变、全绿；而服务端漏传真键是「静默返回空集」⇒ 静默少单。
        self.assertEqual(
            set(jidanpu.HALL_ORDER_EMPTY_FILTER_KEYS),
            set(HALL_EMPTY_FILTER_KEYS_LITERAL),
        )
        self.assertEqual(len(HALL_EMPTY_FILTER_KEYS_LITERAL), 20)
        for name in HALL_EMPTY_FILTER_KEYS_LITERAL:
            self.assertEqual(sent[name], "", name)
        # 抓包里出现、我们一个都不依赖的检索项：照抓包默认值补上。
        # ⭐ 加固轮 T1：**键与值都逐字硬编码** + 与常量做集合对拍
        #    （改值 / 改名 / 多键 / 少键 四种都红）。
        self.assertEqual(
            dict(jidanpu.HALL_ORDER_SEARCH_DEFAULTS), HALL_SEARCH_DEFAULTS_LITERAL
        )
        for name, value in HALL_SEARCH_DEFAULTS_LITERAL.items():
            self.assertEqual(sent[name], value, name)
        # 价格窗**真的横跨 0 两侧**（与上面 `minPrice/maxPrice` 那条同源的独立钉子：
        # 收窄任一侧都会让服务端按价静默过滤 ⇒ `count` 变小 ⇒ 可能误撤还在厅里的单）。
        self.assertLess(sent["seachMutPrice1"], 0)
        self.assertGreater(sent["seachMutPrice2"], 0)
        self.assertLess(sent["seachMutPrice3"], 0)
        self.assertGreater(sent["seachMutPrice4"], 0)
        self.assertEqual(page.count, 42)
        self.assertEqual(page.rows, ({"orderId": "JDP-1"},))

    def test_the_key1_window_is_a_rolling_recent_one_within_the_span_limit(self):
        """⭐ U2 上半：单查的时间窗**按调用当天**算（注入固定当天，断确切值）＋ 跨度 ≤ 460。

        为什么必须断**值**：只断键的集合 ⇒ 把窗口改成任何东西（包括写死的
        `2000-01-01 → 2099-12-31` 那个**必被服务端拒**的超宽窗）它都绿。而超宽窗/超跨度的后果是
        `code=0 / count=0` + 一句 msg，SDK 的 `lenient` 又把它判成**成功并回 `[]`**
        ⇒ 单查**恒判「查无」**（与「真的没有单」同形——D-0366 起消失判定的复核也走段查，
        这条窗只剩单查自己在用，但「写死日期」的坑同样致命）。
        失败方向：写死日期 ⇒ 本用例红。
        """
        client = _FakeClient(replies=[([{"orderId": "JDP-1"}], {"code": 0, "count": 1})])
        with mock.patch.object(jidanpu, "_today", lambda: TODAY):
            with self._patch(client):
                with jidanpu.open_client(self.binding) as c:
                    jidanpu.fetch_order_by_key1(
                        c, key="JDP-1", expect_field="orderId", expect_value="JDP-1"
                    )
        sent = client.calls[0][2]["data"]
        self.assertEqual(sent["stime"], WINDOW_START)
        self.assertEqual(sent["etime"], WINDOW_END)
        self.assertLessEqual(_span_days(sent), jidanpu.ORDER_LIST_MAX_SPAN_DAYS)

    def test_the_key1_window_follows_the_call_day(self):
        """⭐ U2 下半：窗口真的随**调用当天**走 —— 同一天两次逐字相同、换一天不同。

        为什么要两段一起断：写死日期 ⇒ 第二段红；写成 import 时算一次的模块级常量
        ⇒ 第一段仍绿、第二段也绿（那种实现**看起来**也「按当天算」）。
        失败方向：把 `_today` 换成模块级常量 ⇒ 本用例红。
        """

        def _window_on(day):
            client = _FakeClient(
                replies=[([{"orderId": "JDP-1"}], {"code": 0, "count": 1})]
            )
            with mock.patch.object(jidanpu, "_today", lambda: day):
                with self._patch(client):
                    with jidanpu.open_client(self.binding) as c:
                        jidanpu.fetch_order_by_key1(
                            c, key="JDP-1", expect_field="orderId", expect_value="JDP-1"
                        )
            sent = client.calls[0][2]["data"]
            return sent["stime"], sent["etime"]

        first = _window_on(TODAY)
        self.assertEqual(first, _window_on(TODAY))
        self.assertNotEqual(first, _window_on(TODAY + timedelta(days=1)))

    def test_reconcile_page_rejects_a_non_list_payload_loudly(self):
        """形状不对是**未预期的上游变更** ⇒ 抛 `ApiError`（1017），⛔ 不静默当成空列表。

        ⚠️ D-0366 起 `gameName` 回落机制随大厅整厅支一并删除 ⇒ 非列表**只发一次**请求
        就响亮抛出（旧行为「回落重试一次」不复存在）。
        """
        client = _FakeClient(replies=[{"not": "a list"}])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                with self.assertRaises(ApiError):
                    jidanpu.fetch_reconcile_orders_page(
                        c, status_label="代练中",
                        window={"stime": "2026-08-01 00:00:00",
                                "etime": "2026-09-01 23:59:59"},
                    )
        self.assertEqual(len(client.calls), 1)

    def test_reconcile_page_reports_an_unusable_count_as_none(self):
        """壳里**没有可用**的 `count` ⇒ `count is None`（= 判不了拉全，调用方按「拉不全」处理）。

        ⛔ 这是 fail-**closed** 契约：`0 >= -1` 那种负数哨兵会让第 1 页就被判成「拉全」，
        而不变量「拉取不完整的轮次一单都不动作」正是被这个形状绕过的（fail-open 的代价
        落在业务上）。四个反例形状一起钉：缺键、`count: true`（`bool` 是 `int` 的子类）、
        字符串、**负数**（ 1 补的守卫，负数与缺键同档）。
        """
        for envelope in (
            {"code": 0, "msg": "success"},
            {"code": 0, "msg": "success", "count": True},
            {"code": 0, "msg": "success", "count": "42"},
            {"code": 0, "msg": "success", "count": -1},
        ):
            with self.subTest(envelope=envelope):
                client = _FakeClient(
                    replies=[([{"orderId": "JDP-1"}], envelope)]
                )
                with self._patch(client):
                    with jidanpu.open_client(self.binding) as c:
                        page = jidanpu.fetch_reconcile_orders_page(
                            c, status_label="代练中",
                            window={"stime": "2026-08-01 00:00:00",
                                    "etime": "2026-09-01 23:59:59"},
                        )
                self.assertIsNone(page.count)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(page.rows, ({"orderId": "JDP-1"},))

    # ── `key1` 单查：定位 + 逐字校验 ────────────────────────────────────────

    def test_fetch_order_by_key1_returns_the_row_whose_target_field_matches(self):
        rows = [
            {"orderId": "JDP-OTHER", "orderIdView": "SYNTH-SRC-00000001-X"},  # 子串误命中
            {"orderId": REMOTE_ORDER_ID, "orderIdView": SYNTH_SRC},
        ]
        client = _FakeClient(replies=[(rows, {"code": 0, "count": 2})])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                found = jidanpu.fetch_order_by_key1(
                    c, key=SYNTH_SRC, expect_field="orderIdView", expect_value=SYNTH_SRC
                )
        self.assertEqual(found["orderId"], REMOTE_ORDER_ID)

    def test_fetch_order_by_key1_refuses_to_treat_count_one_as_a_hit(self):
        """⭐ 判据的核心（spec §4.5 / §5.2 的 T19 订正块）：`key1` 对 `orderIdView` 是
        **子串**匹配 ⇒ `count == 1` 也可能是**别人的单**。唯一命中的那行若目标字段不逐字相等，
        必须回 `None`，⛔ 不许把它当成「查到了」。"""
        rows = [{"orderId": "JDP-SOMEONE-ELSE", "orderIdView": "XX-SYNTH-SRC-00000001-XX"}]
        client = _FakeClient(replies=[(rows, {"code": 0, "count": 1})])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                found = jidanpu.fetch_order_by_key1(
                    c, key=SYNTH_SRC, expect_field="orderIdView", expect_value=SYNTH_SRC
                )
        self.assertIsNone(found)

    def test_fetch_order_by_key1_returns_none_for_an_empty_result(self):
        client = _FakeClient(replies=[([], {"code": 0, "count": 0})])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                self.assertIsNone(
                    jidanpu.fetch_order_by_key1(
                        c, key="JDP-NOPE", expect_field="orderId", expect_value="JDP-NOPE"
                    )
                )

    def test_fetch_order_by_key1_sends_the_eight_mandatory_params(self):
        """`GetOrderList` 的 8 个必带参数（spec §17.6.1 第 1 行：**漏传不报错、静默返回空集**）。

        6 个非时间项在常量里、`stime`/`etime` 由 `order_list_window` 按当天算 ⇒ 这里断的是
        **8 个名字一个不少**；**值**由下面那条窗口钉子管。
        """
        client = _FakeClient(replies=[([], {"code": 0, "count": 0})])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                jidanpu.fetch_order_by_key1(
                    c, key="JDP-X", expect_field="orderId", expect_value="JDP-X"
                )
        sent = client.calls[0][2]["data"]
        self.assertEqual(
            set(sent.keys()) - {"key1"},
            set(jidanpu.ORDER_LIST_KEY1_MANDATORY_PARAMS) | {"stime", "etime"},
        )
        self.assertEqual(client.calls[0][2].get("query"), None)

    def test_the_key1_window_is_a_rolling_recent_one_within_the_span_limit(self):
        """⭐ 窗口钉子（探针报告 §4.4-1）：**注入固定当天**，断言确切的 `stime`/`etime` 值
        ＋ 跨度 ≤ `ORDER_LIST_MAX_SPAN_DAYS`。

        为什么必须断**值**：上面那条只断键的集合 ⇒ 把窗口改成任何东西（包括写死的
        `2000-01-01 → 2099-12-31` 那个**必被服务端拒**的超宽窗）它都绿。而超宽窗的后果是
        `code=0 / count=0` + 一句 msg，SDK 的 `lenient` 又把它判成**成功并回 `[]`**
        ⇒ 单查对所有单静默回 `None`（实测：今天 / 31 / 80 / 425 天前的四张单逐字无一命中）。
        """
        client = _FakeClient(replies=[([], {"code": 0, "count": 0})])
        with mock.patch.object(jidanpu, "_today", lambda: TODAY):
            with self._patch(client):
                with jidanpu.open_client(self.binding) as c:
                    jidanpu.fetch_order_by_key1(
                        c, key="JDP-X", expect_field="orderId", expect_value="JDP-X"
                    )
        sent = client.calls[0][2]["data"]
        self.assertEqual(sent["stime"], WINDOW_START)
        self.assertEqual(sent["etime"], WINDOW_END)
        self.assertLessEqual(_span_days(sent), jidanpu.ORDER_LIST_MAX_SPAN_DAYS)
        self.assertEqual(
            date.fromisoformat(sent["stime"][:10]),
            TODAY - timedelta(days=jidanpu.ORDER_LIST_LOOKBACK_DAYS),
        )

    def test_order_list_window_is_computed_from_the_injected_day(self):
        """窗口是**纯函数 + 注入当天**（⛔ 不是 import 时算一次的模块级常量：那样会随日历漂）。"""
        window = jidanpu.order_list_window(today=date(2026, 3, 1))
        self.assertEqual(window["stime"], "2025-12-01 00:00:00")  # 2026-03-01 − 90 天
        self.assertEqual(window["etime"], "2026-03-02 23:59:59")  # 2026-03-01 + 1 天
        self.assertLessEqual(_span_days(window), jidanpu.ORDER_LIST_MAX_SPAN_DAYS)

    # ── 写端点：失败信封必须响亮 ────────────────────────────────────────────

    def test_add_order_records_the_oid_from_the_envelope_code(self):
        """实测：`AddOrderAjax` 成功时回包 `code` = 新单的**内部主键 `oid`**（T19 订正块）。"""
        client = _FakeClient(
            replies=[("", {"code": REMOTE_OID, "msg": "合成回包：录单已受理", "data": ""})]
        )
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                result = jidanpu.add_order(
                    c, form_payload={}, top_level={}, source_order_no=SYNTH_SRC
                )
        self.assertEqual(result.remote_oid, REMOTE_OID)
        self.assertEqual(result.message, "合成回包：录单已受理")

    def test_add_order_raises_on_a_failure_envelope(self):
        """⭐ 失败信封（`code=0` + 非空 `msg` + 空 `data`）**必须被判失败**（E-0087）。

        写端点由 SDK 的 `positive_code` 判（spec §5.7 / D15）⇒ 这里断言的是
        「我们不吞异常」这一半；「事件落 failed」那一半在 Task 7 的行为用例里。
        """
        client = _FakeClient(
            replies=[ApiError("系统要求来源单号不能为空", code=0)]
        )
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                with self.assertRaises(ApiError):
                    jidanpu.add_order(
                        c, form_payload={}, top_level={}, source_order_no=""
                    )

    def test_add_order_double_encodes_the_form_json_into_strParameter(self):
        """旧 spec §2.8-1：`strParameter` = **整张录单表单的 JSON 再 URL 编码一次**。

        判据：拆开 `strParameter` 后 `json.loads(urllib.parse.unquote(...))` 能还原成原字典，
        且**不能**是两次编码（那样 `json.loads` 会失败）。
        ⚠️ 顶层参数与表单 JSON 是**两份**：顶层 `gameName` 是**游戏名**、表单里的是**游戏 ID**
        （旧 spec §2.8-2 实测的「同名不同义」）——所以本函数收两个字典，⛔ 不合成一个。
        """
        import json
        import urllib.parse

        client = _FakeClient(replies=[("", {"code": 1, "msg": "合成回包：录单已受理", "data": ""})])
        form_payload = {"souresOrderID": SYNTH_SRC, "orderType": "0", "gameName": "21700"}
        top_level = {"gameName": "测试游戏甲", "timeType": "10"}
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                jidanpu.add_order(
                    c, form_payload=form_payload, top_level=top_level,
                    source_order_no=SYNTH_SRC,
                )
        sent = client.calls[0][2]["data"]
        raw = sent["strParameter"]
        # ⚠️ 任务书只给还原断言；实测**它抓不到「只编一次」**（`unquote` 对没有 `%` 的串
        # 是恒等变换 ⇒ 裸 JSON 也能还原）⇒ 补一条方向性的：单次 `quote` 会把 JSON 里的
        # `"` 写成 `%22`，裸引号还在就说明**根本没编码**（两次编码则由下面的还原断言抓）。
        self.assertNotIn('"', raw)
        self.assertEqual(json.loads(urllib.parse.unquote(raw)), form_payload)
        self.assertEqual(sent["souresOrderID"], SYNTH_SRC)
        self.assertEqual(sent["gameName"], "测试游戏甲")   # 顶层是**游戏名**
        self.assertEqual(sent["timeType"], "10")

    def test_throw_order_payload_matches_the_measured_shape(self):
        """spec §5.3：`fee` / `action=10` / `insureFee=0` / `pushPriceMode=10`（实测）。"""
        client = _FakeClient(replies=[("", {"code": 1, "msg": "合成回包：抛单已受理", "data": ""})])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                jidanpu.throw_order(
                    c, remote_order_id=REMOTE_ORDER_ID, fee=Decimal("12.34")
                )
        sent = client.calls[0][2]["data"]
        self.assertEqual(sent["orderId"], REMOTE_ORDER_ID)
        self.assertEqual(sent["action"], 10)
        self.assertEqual(sent["insureFee"], 0)
        self.assertEqual(sent["pushPriceMode"], 10)
        self.assertEqual(str(sent["fee"]), "12.34")

    def test_assign_order_sends_the_vendor_shaped_payload_with_a_fee_key(self):
        """⭐ `jidanpu.assign_order` 的**载荷形状**单测（⛔ 它**不再**是「占住」那条路）。

        ⚠️ **2026-09-29 改准**：入方向「占住」已改用**抢单**（`D-0169`）⇒ 本函数**全仓已无生产调用方**
        （只剩本用例），保留它是为了将来真做「客服手动指派」。旧名与旧 docstring 写「占住 = 指派给同步账号 + fee=0」，
        那两句现在都不成立（见 `E-0167` 那条废注释的先例：⛔ 别把历史口径留在用例名里）。
        """
        import json

        client = _FakeClient(replies=[("", {"code": 1, "msg": "合成回包：指派已受理", "data": ""})])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                jidanpu.assign_order(
                    c, remote_order_id=REMOTE_ORDER_ID, user_id="100001",
                    user_name="测试员工甲", fee=Decimal("0"), hours="720", project="测试项目",
                )
        sent = client.calls[0][2]["data"]
        self.assertEqual(sent["isAutoAss"], "N")
        self.assertEqual(sent["sphone"], "")
        self.assertEqual(sent["isOpenThrowOrderPricePerce"], "N")
        self.assertEqual(sent["taskNeedJobId"], "")
        params = json.loads(sent["strParameter"])
        self.assertEqual(params[0]["userId"], "100001")
        # ⚠️ 任务书此处写 `str(params[0]["fee"]) == "0"`，与它自己给的实现
        # （`float(Decimal("0"))` ⇒ JSON 数字 `0.0`）和实测载荷（spike-1 §Q1-b /
        # spec §6.1：`"fee":0.0`）都对不上（`str(0.0) == "0.0"`）⇒ 按**实测形状**
        # 钉「发出去的是 JSON 数字 0」，⛔ 不为了迁就断言把载荷改成字符串
        # （`strParameter` 是强类型 JSON，改类型是改行为，不是改测试）。
        self.assertEqual(params[0]["fee"], 0)
        self.assertIsInstance(params[0]["fee"], (int, float))
        # ⭐ **外部契约（真机实测，`E-0164`）**：`strParameter[].fee` 这个**键必须存在**
        #    ——把整个键去掉，上游对 `SetOrderZhiPaiByIDX` **稳定回 HTTP 500**；填数字或字符串
        #    （对标 `(tichen/100*price).toFixed(2)` 发的是字符串）都能成功。
        #    ⚠️ 对标在 `price<=0` 那一支恰好**不带**这个键（那是上游自己的坑）⇒ ⛔ 别照抄。
        #    判据钉「键存在」而不是钉某个值：值由调用方给，形状是本函数对上游的承诺。
        self.assertIn("fee", params[0])
        self.assertEqual(params[0]["hours"], "720")
        self.assertEqual(params[0]["project"], "测试项目")

    def test_grab_order_sends_only_the_order_id(self):
        """⭐ 占住改用**抢单**：只打 `RecOrderByInternal`、载荷**只有 `orderId`**（一个键不多不少）。

        对标 `insiderecorderlist.js` L755-766 的 `recThisOrder`（`type:"Post"` +
        `data:{orderId:…}`）；2026-09-29 本机真机实测：只带 `orderId`
        （**不带** `__RequestVerificationToken`）⇒ `code=1`「接单成功…」、
        回读 `recFee` **原样保留**（⛔ 不再把远端发单价写成 0）。
        失败方向：给载荷补一个 `fee` / `userId` / `__RequestVerificationToken` ⇒ 本用例红
        ——多写的那一笔金额正是本线要根除的 0 的来源；多带令牌则偏离实测载荷。
        """
        client = _FakeClient(
            replies=[("", {"code": 1, "msg": "合成回包：接单成功", "data": ""})]
        )
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                jidanpu.grab_order(c, remote_order_id=REMOTE_ORDER_ID)
        method, path, kwargs = client.calls[0]
        self.assertEqual((method, path), ("POST", jidanpu.GRAB_PATH))
        self.assertEqual(kwargs["data"], {"orderId": REMOTE_ORDER_ID})

    def test_set_order_status_sends_the_mandatory_change_source_type(self):
        """⭐ spec §5.7：`changeSoureType=20` **必带**（缺了被服务端判「请勿尝试跨权限提交」）。"""
        client = _FakeClient(replies=[("", {"code": 1, "msg": "合成回包：状态已提交", "data": ""})])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                jidanpu.set_order_status(
                    c, remote_order_id=REMOTE_ORDER_ID,
                    status_label=jidanpu.ORDER_REFUNDED_LABEL,
                    remark="订单已被发单方打手接取",
                )
        sent = client.calls[0][2]["data"]
        self.assertEqual(sent["sta"], "已退单")
        self.assertEqual(sent["changeSoureType"], 20)
        self.assertEqual(sent["isSyncTaoBao"], "N")
        self.assertEqual(sent["remark"], "订单已被发单方打手接取")

    def test_set_order_status_refuses_a_remark_over_the_page_limit(self):
        """`remark` 页面上限 50 字（spec §5.7）：**50 字放行 / 51 字抛 `ValueError`**，
        且超长那次**一个请求都不发**（⛔ 不静默截断、也不先发再截）。

        ⚠️ 「用户输入的退单原因要过校验、回业务码 `1000`」是**入口 / 视图**那一层的职责
        （Task 8 / 17）；这里是**程序错误的响亮守卫**（P2-A 唯一调用方传固定字符串）。
        """
        ok = "字" * jidanpu.HALL_REMARK_MAX
        too_long = "字" * (jidanpu.HALL_REMARK_MAX + 1)
        client = _FakeClient(
            replies=[("", {"code": 1, "msg": "合成回包：状态已提交", "data": ""})]
        )
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                jidanpu.set_order_status(
                    c, remote_order_id=REMOTE_ORDER_ID,
                    status_label=jidanpu.ORDER_REFUNDED_LABEL, remark=ok,
                )
                with self.assertRaises(ValueError):
                    jidanpu.set_order_status(
                        c, remote_order_id=REMOTE_ORDER_ID,
                        status_label=jidanpu.ORDER_REFUNDED_LABEL, remark=too_long,
                    )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][2]["data"]["remark"], ok)

    def test_fetch_ass_logs_calls_the_unregistered_endpoint_and_returns_rows(self):
        """`GetOrderAssLogs` **不在** vendor 清单里（旧 spec §12-14）：走未登记端点路径。

        ⚠️ 这条用例同时钉**我们的决定**：⛔ **不传** `success_rule=`（读端点的默认就是
        `lenient`，与已登记读端点同形；自造第二层判据正是 P1 明令禁止的）。
        """
        rows = [{"userId": 100002, "userName": "测试员工乙", "fee": 0.0}]
        client = _FakeClient(replies=[(rows, {"code": 0, "msg": "success"})])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                found = jidanpu.fetch_ass_logs(c, remote_order_id=REMOTE_ORDER_ID)
        self.assertEqual(client.calls[0][1], jidanpu.ASS_LOGS_PATH)
        self.assertEqual(client.calls[0][2]["query"], {"orderId": REMOTE_ORDER_ID})
        self.assertNotIn("success_rule", client.calls[0][2])
        self.assertEqual(found, tuple(rows))

    def test_fetch_sources_and_assignable_users_return_rows(self):
        sources = [{"name": "d-团团通系统", "value": "团团通系统"}]
        # ⚠️ 可指派名单那一行的字段名照**真机实测**来（`isValid`；⛔ 这个端点没有 `sta`，
                #    ）：夹具沿用错字段名会让下一个人以为这里的形状是对的。
        users = [{"id": 100001, "userName": "测试员工甲", "isValid": "Y"}]
        client = _FakeClient(replies=[
            (sources, {"code": 0, "msg": "success"}),
            (users, {"code": 0, "msg": "success"}),
        ])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                self.assertEqual(jidanpu.fetch_sources(c), tuple(sources))
                self.assertEqual(
                    jidanpu.fetch_assignable_users(c, page_size=100), tuple(users)
                )
        self.assertEqual(client.calls[0][1], jidanpu.SOURE_LIST_PATH)
        self.assertEqual(client.calls[1][1], jidanpu.ASSIGNABLE_USERS_PATH)

    # ── 本线：**登录账号**的身份（同步账号 = 我们登录的那个账号）──────────

    def test_fetch_login_user_calls_the_unregistered_endpoint_and_returns_the_object(self):
        """`GetUserInfo` **不在** vendor 清单里：走未登记端点路径（形状由我们自己校验）。

        ⚠️ 与 `fetch_ass_logs` 同一条决定：⛔ **不传** `success_rule=`（未登记 ⇒ `lenient`，
        与已登记读端点同形）；夹具是**合成**回包（`Id` 是醒目值，⛔ 不是任何真实 uid）。
        """
        info = {"Id": 9002, "Account": "bot-synth-x", "IsSuper": True}
        client = _FakeClient(replies=[(info, {"code": 1, "msg": "success"})])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                self.assertEqual(jidanpu.fetch_login_user(c), info)
        method, path, kwargs = client.calls[0]
        self.assertEqual((method, path), ("GET", jidanpu.LOGIN_USER_INFO_PATH))
        self.assertNotIn("success_rule", kwargs)
        self.assertIsNone(kwargs.get("query"))

    def test_fetch_login_user_rejects_a_non_object_payload_loudly(self):
        """载荷不是对象 ⇒ 响亮抛 `ApiError`（⛔ 不静默当成空身份：那会让「读不出来」与
        「身份是空的」同形，E-0004）。"""
        for bad in ([], "不是对象", 42, None):
            with self.subTest(bad=bad):
                client = _FakeClient(replies=[bad])
                with self._patch(client):
                    with jidanpu.open_client(self.binding) as c:
                        with self.assertRaises(ApiError):
                            jidanpu.fetch_login_user(c)

    def test_resolve_login_identity_names_the_uid_from_the_assignable_list(self):
        """uid 取 `Id`、显示名取**可指派名单**里同 `id` 那一行的 `userName`；找到就不再退化。"""
        login = {"Id": 9002, "Account": "bot-synth-x", "IsSuper": True}
        users = [
            {"id": 9003, "userName": "测试同步账号乙", "isValid": "Y"},
            {"id": 9002, "userName": "测试同步账号甲", "isValid": "Y"},
        ]
        client = _FakeClient(replies=[login, users])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                self.assertEqual(
                    jidanpu.resolve_login_identity(c),
                    ("9002", "测试同步账号甲"),
                )
        self.assertEqual(
            [call[1] for call in client.calls],
            [jidanpu.LOGIN_USER_INFO_PATH, jidanpu.ASSIGNABLE_USERS_PATH],
        )

    def test_resolve_login_identity_falls_back_to_the_user_list_then_to_an_empty_name(self):
        """退化顺序是判据：可指派名单里没有他 ⇒ 退到**人员页**（字段 `uid`）；
        两处都没有 ⇒ 回**空串**（= 「这个名字没查到」）。

        ⭐ **2026-09-29 去回落**：原实现会回落到调用方给的 `fallback_name`（= 绑定现在的旧名）。
        那条退路让「没查到」与「查到了」**同形**（都回一个字符串）⇒ 调用方会拿旧名当新名写回、
        还留一条「已按登录账号写回」的假痕迹（独立评审 PROBE3）。现在空串是唯一且明确的
        「没查到」信号，由调用方按「不写、不留痕」处理。
        失败方向：把最后那行改回返回一个非空回落值 ⇒ 本用例最后一条断言红。
        """
        login = {"Id": 9002}
        staff_page = [{"uid": 9002, "userName": "测试同步账号甲", "sta": "Y"}]
        client = _FakeClient(replies=[
            login, [],
            (staff_page, {"code": 0, "msg": "success", "count": 1}),
        ])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                self.assertEqual(
                    jidanpu.resolve_login_identity(c),
                    ("9002", "测试同步账号甲"),
                )
        self.assertEqual(
            [call[1] for call in client.calls],
            [
                jidanpu.LOGIN_USER_INFO_PATH,
                jidanpu.ASSIGNABLE_USERS_PATH,
                jidanpu.USERS_PATH,
            ],
        )

        nowhere = _FakeClient(replies=[
            login, [], ([], {"code": 0, "msg": "success", "count": 0}),
        ])
        with self._patch(nowhere):
            with jidanpu.open_client(self.binding) as c:
                self.assertEqual(
                    jidanpu.resolve_login_identity(c),
                    ("9002", ""),
                )

    def test_resolve_login_identity_accepts_a_numeric_string_uid_but_refuses_the_rest(self):
        """uid 只收**十进制正整数**（数字或数字串）；`0` / 负数 / 小数串 / 文本 / 布尔
        一律**响亮失败**——那是「读不出来」，⛔ 不是「找不到」，也⛔ 不许退化成空身份。"""
        client = _FakeClient(replies=[{"Id": "9002"}, [{"id": 9002, "userName": "测试同步账号甲"}]])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                self.assertEqual(
                    jidanpu.resolve_login_identity(c), ("9002", "测试同步账号甲")
                )

        for bad in (0, -1, "0", "-1", "551.0", "uid=551", "不是数字", None, True, ""):
            with self.subTest(bad=bad):
                client = _FakeClient(replies=[{"Id": bad}])
                with self._patch(client):
                    with jidanpu.open_client(self.binding) as c:
                        with self.assertRaises(ApiError):
                            jidanpu.resolve_login_identity(c)
                # 失败要**当场**抛：⛔ 不再去打名单（那是「找不到」那一支的事）
                self.assertEqual(len(client.calls), 1)

    def test_a_non_dict_row_is_a_loud_failure_not_a_silent_skip(self):
        """⭐ 修复轮 2 裁定：载荷是列表、但**混了非 dict 元素** ⇒ 响亮抛 `ApiError`。

        ⛔ 静默过滤（`row for row in rows if isinstance(row, dict)`）的后果与「远端真的没有」
        **同形**（E-0004 / E-0010 族）：单查会回 `None`、拉取会少单，而且**零留痕**。
        代价方向是故意的：一行畸形 ⇒ 本轮中止 + 台账可见 + 下一轮重试。
        """
        for bad in (None, "JDP-X", 42, ["nested"]):
            with self.subTest(bad=bad):
                client = _FakeClient(
                    replies=[
                        ([{"name": "合成来源甲"}, bad], {"code": 0, "msg": "success"})
                    ]
                )
                with self._patch(client):
                    with jidanpu.open_client(self.binding) as c:
                        with self.assertRaises(ApiError) as ctx:
                            jidanpu.fetch_sources(c)
                # `code=None`：**我们**报的形状错，⛔ 不伪造上游业务码。
                self.assertIsNone(ctx.exception.code)
                self.assertIn("第 1 行", str(ctx.exception))  # 指认坏行的下标

    def test_all_object_rows_and_a_legit_empty_list_pass_through(self):
        """阳性对照：**全是 dict** ⇒ 一行不少；**合法空列表** ⇒ 回空。

        ⛔ 别把「空」当畸形（那会把「远端今天没有来源」变成一轮失败）；
        上面的阴性用例靠这一条对照才说明「抛的是畸形行、不是列表本身」。
        """
        rows = [{"name": "合成来源甲"}, {"name": "合成来源乙"}]
        client = _FakeClient(replies=[
            (rows, {"code": 0, "msg": "success"}),
            ([], {"code": 0, "msg": "success"}),
        ])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                self.assertEqual(jidanpu.fetch_sources(c), tuple(rows))
                self.assertEqual(jidanpu.fetch_sources(c), ())

    def test_no_operation_hands_the_sdk_a_second_verdict(self):
        """spec §5.7 / D15：10 个操作**一个都不传** `success_rule=`。

        判据在 SDK 那一个落点（读 `lenient` / 写 `positive_code`）；自造第二层判据会
        **误杀读端点**（spec §12-2.3 实测过 4 个）。这条网覆盖全部 10 个调用点——
        包括上面那些写端点用例**不看** kwargs 的那几个。
        """
        client = _FakeClient(replies=[
            # 慢路对账那一支（D-0366 起顶替已删的大厅整厅支占第 1 位）。
            ([], {"code": 0, "count": 0}),
            ([], {"code": 0, "count": 0}),
            ("", {"code": 1, "msg": "ok", "data": ""}),
            ("", {"code": 1, "msg": "ok", "data": ""}),
            ("", {"code": 1, "msg": "ok", "data": ""}),
            ("", {"code": 1, "msg": "ok", "data": ""}),
            ("", {"code": 1, "msg": "ok", "data": ""}),
            ([], {"code": 0, "msg": "success"}),
            ([], {"code": 0, "msg": "success"}),
            ([], {"code": 0, "msg": "success"}),
        ])
        with self._patch(client):
            with jidanpu.open_client(self.binding) as c:
                jidanpu.fetch_reconcile_orders_page(
                    c, status_label="代练中",
                    window={"stime": "2026-08-01 00:00:00",
                            "etime": "2026-09-01 23:59:59"},
                )
                jidanpu.fetch_order_by_key1(
                    c, key="JDP-X", expect_field="orderId", expect_value="JDP-X"
                )
                jidanpu.add_order(
                    c, form_payload={}, top_level={}, source_order_no=SYNTH_SRC
                )
                jidanpu.throw_order(
                    c, remote_order_id=REMOTE_ORDER_ID, fee=Decimal("12.34")
                )
                jidanpu.assign_order(
                    c, remote_order_id=REMOTE_ORDER_ID, user_id="100001",
                    user_name="测试员工甲", fee=Decimal("0"), hours="720",
                    project="测试项目",
                )
                jidanpu.grab_order(c, remote_order_id=REMOTE_ORDER_ID)
                jidanpu.set_order_status(
                    c, remote_order_id=REMOTE_ORDER_ID,
                    status_label=jidanpu.ORDER_REFUNDED_LABEL, remark="",
                )
                jidanpu.fetch_ass_logs(c, remote_order_id=REMOTE_ORDER_ID)
                jidanpu.fetch_sources(c)
                jidanpu.fetch_assignable_users(c, page_size=100)

        self.assertEqual(len(client.calls), 10)
        for method, path, kwargs in client.calls:
            with self.subTest(path=path):
                self.assertNotIn("success_rule", kwargs)

    # ── E-0087 的落点：写端点的失败信封由 SDK 判失败（桩下移到 HTTP 边界）────

    def test_every_write_endpoint_raises_on_a_failure_envelope(self):
        """⭐ 每个写端点各一条：桩回**实测的写失败壳**
        `{"code": 0, "msg": "<原因>", "data": ""}` ⇒ 必须抛 `ApiError`。

        为什么不能只靠上面那条 `_FakeClient` 的用例：那个桩自己抛异常，证明的只是
        「我们不吞异常」；**判成败那一步在 SDK**（写端点 `positive_code`：宽松启发式会把
        「有 `data` 键」判成成功 ⇒ 正是 E-0087 要消灭的静默失败）。这里让真 SDK 跑一遍
        `Transport.unwrap_response`，规则没接上就必红。
        """
        for name, invoke, path in _write_operations():
            with self.subTest(operation=name):
                stub = _StubHttp(
                    [{"code": 0, "msg": "系统要求来源单号不能为空", "data": ""}]
                )
                with mock.patch.object(
                    jidanpu, "JidanpuClient", _real_client_with(stub)
                ):
                    with jidanpu.open_client(self.binding) as c:
                        with self.assertRaises(ApiError):
                            invoke(c)
                # 桩接没接上本身要有判据：登录 + 那个写端点都真的发出去了。
                self.assertEqual(
                    [url for _, url in stub.calls],
                    [BASE_URL + "/Login/Login", BASE_URL + path],
                )

    def test_clear_order_taker_rejects_conflicting_or_unconfirmed_success_codes(self):
        for value in (0, 2, True):
            with self.subTest(value=value):
                stub = _StubHttp([{"success": True, "code": value, "data": ""}])
                with mock.patch.object(jidanpu, "JidanpuClient", _real_client_with(stub)):
                    with jidanpu.open_client(self.binding) as client:
                        with self.assertRaises(ApiError):
                            jidanpu.clear_order_taker(client, remote_order_id=REMOTE_ORDER_ID)
                self.assertEqual(stub.calls[-1][1], BASE_URL + "/OrderAjax/CleanOrderZhiPaiByIDX")

    def test_the_same_stub_with_a_success_code_does_not_raise(self):
        """**对照组**：同一个桩把 `code` 换成 `1` 就不抛 ⇒ 上面那条不是在别的原因上红的。"""
        for name, invoke, path in _write_operations():
            with self.subTest(operation=name):
                stub = _StubHttp([{"code": 1, "msg": "成功", "data": ""}])
                with mock.patch.object(
                    jidanpu, "JidanpuClient", _real_client_with(stub)
                ):
                    with jidanpu.open_client(self.binding) as c:
                        invoke(c)
                self.assertEqual(stub.calls[-1][1], BASE_URL + path)

    def test_a_read_endpoint_keeps_the_sdk_lenient_default(self):
        """读端点判 `code == 0`：**空结果是成功**（`lenient`），不是失败。

        ⚠️ 载体 = 慢路对账（D-0366 起顶替已删的大厅整厅支）：空列表 + `count=0` 由 SDK 的
        `lenient` 判成**成功**并回 `OrderListPage(rows=(), count=0)`——⭐ `game_fallback`
        标记位随大厅整厅支一并删除，回落机制不复存在（对照旧断言 `assertTrue(page.game_fallback)`）。

        ⇄ 与上面两条互为对照：把读端点误标成写口径，这条红；把写端点留在宽松口径，
        失败信封那条红。
        """
        stub = _StubHttp([
            {"code": 0, "msg": "success", "data": [], "count": 0},
        ])
        with mock.patch.object(jidanpu, "JidanpuClient", _real_client_with(stub)):
            with jidanpu.open_client(self.binding) as c:
                page = jidanpu.fetch_reconcile_orders_page(
                    c, status_label="代练中",
                    window={"stime": "2026-08-01 00:00:00",
                            "etime": "2026-09-01 23:59:59"},
                )
        self.assertEqual(page.rows, ())
        self.assertEqual(page.count, 0)
