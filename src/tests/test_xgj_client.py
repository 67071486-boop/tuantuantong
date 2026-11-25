"""闲管家店铺对接：客户端契约、CRUD、回显纪律、上游失败映射、读单分流、跨租户隔离。

**桩打在哪里**（E-0029：桩要打在「被查找的那个模块」上，不是「定义它的模块」）：
- HTTP 层：`apps.business.xgj._post_json`（客户端内部**唯一**发请求的地方）；
- 视图层：`apps.business.api.xgj_views.order_detail` / `.shop_list`；
- 读单分流：`apps.business.api.platform_order_views.order_detail`（阿奇索）与
  `.xgj_order_detail`（闲管家）。

⚠️ 本文件覆盖的两个端点都是 `urls.py` 里**显式的 `path()`**（不是 `@action`），
且都**只接受 POST** ⇒ `test_no_route_500` 的 explicit-routes 腿会跳过它们
（该腿只发 GET）⇒ 畸形入参覆盖由本文件自己给。
"""
from __future__ import annotations

import hashlib
import json
from unittest import mock

from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import OrderSource, ShopBinding, XgjShopBinding
from apps.business.xgj import (
    DEFAULT_BASE_URL,
    XgjError,
    json_body,
    logistics_dummy_send,
    order_detail,
    shop_list,
    sign,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

#: 店铺对接 CRUD（spec §5，router 注册）。
URL = "/api/business/xgj-bindings/"
#: 查订单详情（显式 `path()`）。
ORDER_DETAIL_URL = "/api/business/xgj/order-detail/"
#: 录单读单（既有端点；本线给它加了闲管家分流）。
READ_URL = "/api/business/read-platform-order/"

#: 租户 A = `TenantTestCase` 的 `test` schema 域名（与 `test_shop_binding_api.py` 同口径）。
DOMAIN_A = "tenant.test.com"
#: 隔离用例的第二个租户。
DOMAIN_B = "xgj-iso-b.test.local"
SCHEMA_B = "xgj_iso_b"


class _FakeResponse:
    """`urllib.request.urlopen` 的最小替身：只让被测代码能 `with ... as r: r.read()`。

    用它而不是 `MagicMock`：`_post_json` 里那一句是 `with urlopen(...) as response:`，
    `MagicMock` 的 `__enter__` 会给回**另一个** mock，`read()` 返回的又是一个 mock
    ⇒ `json.loads(mock)` 抛的是 `TypeError`（不是我们要测的那两条解析失败路径）。
    """

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


# ── 客户端：签名与请求形状 ──────────────────────────────────────────


class XgjSignTests(SimpleTestCase):
    """签名是**契约**：它错了，腾讯侧一个请求都不会成功，而本地测试全绿。

    期望值是**手算的字面量**（不是用 `sign()` 自己算的——那是自指断言，E-0024）：
    向量 = `app_key="AK"` / `app_secret="SK"` / `timestamp=1700000000` / body `{"order_no":"123"}`。
    """

    def test_sign_matches_the_hand_computed_vector(self):
        got = sign(
            app_key="AK", body_json='{"order_no":"123"}', timestamp=1700000000, app_secret="SK"
        )
        self.assertEqual(got, "937fc0d3596926403447ae06b094cda4")
        # 空体那一支（`{}`）单独钉：官方 SDK 的空体是 `'{}'`，不是 `''`。
        self.assertEqual(
            sign(app_key="AK", body_json="{}", timestamp=1700000000, app_secret="SK"),
            "97a4615ade81b55f0eec620024e2c97c",
        )

    def test_body_serialisation_is_part_of_the_signature_contract(self):
        """body 的序列化参数**决定**那个 MD5，所以它是契约的一部分（spec §2.3）。"""
        # 空字典 ⇒ `'{}'`（不是 `''`、不是 `'null'`）
        self.assertEqual(json_body({}), "{}")
        # 紧凑分隔符（不留空格）+ 中文原样（`ensure_ascii=False`）
        self.assertEqual(json_body({"a": 1, "b": "小"}), '{"a":1,"b":"小"}')
        # 与手算的 body_md5 对上：证明上面那个字符串就是被摘要的东西
        self.assertEqual(
            hashlib.md5(json_body({"order_no": "123"}).encode()).hexdigest(),
            "c14b21095d7b26ed6bf05cf497b2041f",
        )

    def test_request_carries_appid_timestamp_and_sign_in_the_query(self):
        """真实请求必须带上 `appid` / `timestamp` / `sign`，且 `sign` 是**那个 body** 的签名。"""
        captured = {}

        def fake_post(*, url, body, headers, timeout):
            captured.update(url=url, body=body, headers=headers)
            return {"code": 0, "msg": "OK", "data": {"order_no": "123"}}

        with mock.patch("apps.business.xgj._post_json", side_effect=fake_post):
            order_detail("123", app_key="AK", app_secret="SK")

        self.assertTrue(captured["url"].startswith(DEFAULT_BASE_URL + "/api/open/order/detail?"))
        self.assertIn("appid=AK", captured["url"])
        self.assertEqual(captured["body"], '{"order_no":"123"}')
        self.assertIn("Content-Type", captured["headers"])
        # 签名与这个 body / 这次 timestamp 对得上（timestamp 从 url 里取，不猜）
        timestamp = int(captured["url"].split("timestamp=")[1].split("&")[0])
        self.assertIn(
            sign(app_key="AK", body_json=captured["body"], timestamp=timestamp, app_secret="SK"),
            captured["url"],
        )
        # ⚠️ `app_secret` **绝不**出现在请求里（它只以派生值参与签名）。
        self.assertNotIn("SK", captured["url"].replace("sign=", ""))


# ── 客户端：信封与异常契约 ──────────────────────────────────────────


class XgjEnvelopeTests(SimpleTestCase):
    """上游信封 → `XgjError` 的映射（spec §7）。**判据是「抛 XgjError」，不是「抛了异常」**。"""

    def _call_with(self, payload):
        with mock.patch("apps.business.xgj._post_json", return_value=payload):
            return order_detail("123", app_key="AK", app_secret="SK")

    def _assert_xgj_error(self, payload, *, code):
        with self.assertRaises(XgjError) as ctx:
            self._call_with(payload)
        self.assertEqual(ctx.exception.upstream_code, code)

    def test_success_returns_the_order_object(self):
        self.assertEqual(
            self._call_with({"code": 0, "msg": "OK", "data": {"order_no": "123", "total_amount": 1}}),
            {"order_no": "123", "total_amount": 1},
        )

    def test_auth_error_code_is_mapped(self):
        """`100001` 是**复合码**（实测：AppID 无效 / 签名错 / 时间戳越界 / 缺 AppID 都回它）。"""
        self._assert_xgj_error({"code": 100001, "msg": "签名错误", "data": {}}, code=100001)

    def test_param_error_code_is_mapped(self):
        self._assert_xgj_error(
            {"code": 100004, "msg": '请求参数错误, field "sign" is not set'}, code=100004
        )

    def test_non_numeric_code_does_not_escape(self):
        """上游把 `code` 回成字符串 / 非数字时**不得**抛 `ValueError`（那会变成 500）。"""
        self._assert_xgj_error({"code": "abc", "msg": "怪"}, code=-1)

    def test_missing_code_is_refused(self):
        self._assert_xgj_error({"msg": "没有 code"}, code=-1)

    def test_non_dict_payload_is_refused(self):
        for payload in ([], "ok", 123, None):
            with self.subTest(payload=payload):
                self._assert_xgj_error(payload, code=-1)

    def test_data_must_be_a_dict(self):
        self._assert_xgj_error({"code": 0, "msg": "OK", "data": []}, code=-1)
        self._assert_xgj_error({"code": 0, "msg": "OK"}, code=-1)

    def test_empty_data_is_refused_not_treated_as_an_empty_order(self):
        """「上游没给」与「上游给了一个空的」是两件事（spec §7 的同款纪律）。

        静默兜成 `{}` 会让录单页显示空价格而**没有人知道为什么**。
        """
        self._assert_xgj_error({"code": 0, "msg": "OK", "data": {}}, code=-1)

    def test_order_detail_only_accepts_the_measured_shape(self):
        """spec §11-T1 **已实测闭合**：`data` 就是订单对象本身。

        2026-09-22 用真实凭据实测（`tests/test_xgj_live.py` 的
        `test_order_detail_reads_a_real_order`）：/api/open/order/detail 的 `data` 是
        **33 个键的订单对象**，不是 `{list:[...]}` 包装。

        ⚠️ 所以本函数**只认一种形状**，`list[0]` 那一支已被删除（原来是 T1 未实测时的临时兜底）。
        这条测试钉的正是"**别再把它加回来**"：加回来等于把「上游换了包装」从响亮的失败
        降级成可能取错对象（E-0011 同族）。
        """
        order = {"order_no": "123", "total_amount": 100}
        self.assertEqual(self._call_with({"code": 0, "data": order}), order)
        # 包装形状（`{list:[...]}`）**不再被接受** ⇒ 抛错，而不是静默取第一个
        self._assert_xgj_error({"code": 0, "data": {"list": [order]}}, code=-1)
        # 空 list 同样不算「有订单」
        self._assert_xgj_error({"code": 0, "data": {"list": []}}, code=-1)

    def test_network_errors_become_xgj_error(self):
        """网络层异常必须**全部**翻译（spec §7：网络异常 → 1017，不得 500）。

        ⚠️ **这里打的是 `urllib.request.urlopen`，不是 `xgj._post_json`**：翻译逻辑住在
        `_post_json` **内部**，把它整个桩掉就等于把要测的那段代码也一起桩掉了
        ——那样测出来的「异常逃逸」是测试自己造的，不是实现的形状。

        `JSONDecodeError` 那一支是**实测**路径：未知路径上游回的是
        HTTP 404 纯文本 `404 page not found`（spec §2.1）。
        """
        import urllib.error

        # ① 直接抛的几族
        for exc in (urllib.error.URLError("boom"), TimeoutError("慢"), OSError("断了")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch("urllib.request.urlopen", side_effect=exc):
                    with self.assertRaises(XgjError) as ctx:
                        order_detail("123", app_key="AK", app_secret="SK")
                self.assertEqual(ctx.exception.upstream_code, -1)

        # ② 拿到了响应、但读出来解析不了（非 UTF-8 / 非 JSON 纯文本）
        for raw, label in ((b"\xff\xfe", "非 UTF-8"), (b"404 page not found", "非 JSON 纯文本")):
            with self.subTest(body=label):
                with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(raw)):
                    with self.assertRaises(XgjError) as ctx:
                        order_detail("123", app_key="AK", app_secret="SK")
                self.assertEqual(ctx.exception.upstream_code, -1)

    def test_http_error_response_is_translated(self):
        import urllib.error

        err = urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(XgjError) as ctx:
                order_detail("123", app_key="AK", app_secret="SK")
        # 上游 4xx/5xx 按**上游失败**处理（`-1`），不是我们的 500；msg 里带上状态码。
        self.assertEqual(ctx.exception.upstream_code, -1)
        self.assertIn("404", ctx.exception.upstream_msg)

    def test_shop_list_returns_rows_and_refuses_a_missing_list(self):
        payload = {"code": 0, "data": {"list": [{"shop_name": "店1"}, {"shop_name": "店2"}]}}
        with mock.patch("apps.business.xgj._post_json", return_value=payload):
            rows = shop_list(app_key="AK", app_secret="SK")
        self.assertEqual([r["shop_name"] for r in rows], ["店1", "店2"])

        # ⚠️ **空列表是合法结果**（应用有效但一个店都没授权），**不是**错误
        with mock.patch("apps.business.xgj._post_json", return_value={"code": 0, "data": {"list": []}}):
            self.assertEqual(shop_list(app_key="AK", app_secret="SK"), [])

        # 「上游没给 list」才是错误
        with mock.patch("apps.business.xgj._post_json", return_value={"code": 0, "data": {}}):
            with self.assertRaises(XgjError):
                shop_list(app_key="AK", app_secret="SK")

        # 混进非 dict 的行 ⇒ 抛错，**不静默丢弃**（少显示一家店而没人知道）
        with mock.patch(
            "apps.business.xgj._post_json",
            return_value={"code": 0, "data": {"list": [{"a": 1}, "x"]}},
        ):
            with self.assertRaises(XgjError):
                shop_list(app_key="AK", app_secret="SK")


# ── 客户端：「无需物流发货」端点（spec §3.2）────────────────────────────


class XgjLogisticsDummySendTests(SimpleTestCase):
    """`logistics_dummy_send` 自己的契约（spec §3.2）：请求形状 + **空 `data` 是成功**。

    ⚠️ 这两条不是 `order_detail` / `shop_list` 那两条的复述：那两个端点的空 `data`
    各有别的含义（「没这单」/「没给 list」）⇒ 它们**各自**额外拒绝了空 data；
    本端点的成功响应**就是** `data: {}`（spec §3.2 的官方文档口径）。
    ⇒ 第 1 条用例是「本端点放行空 data」与「误学那两处去拒绝空 data」之间的**区分器**：
    实现哪天加上「空 data = 失败」，它必须**红**（`XgjError` 会从这里逃出去）。
    """

    #: 合成单号：形状仿官方的 19 位纯数字（⛔ 不是任何真实订单号，E-0077）。
    TB_NO = "22000000000000000009"

    def test_request_shape_is_pinned_and_empty_data_is_a_success(self):
        captured = {}

        def fake_post(*, url, body, headers, timeout):
            captured.update(url=url, body=body, headers=headers)
            return {"code": 0, "msg": "ok", "data": {}}

        with mock.patch("apps.business.xgj._post_json", side_effect=fake_post):
            result = logistics_dummy_send(
                self.TB_NO, app_key="AK", app_secret="SK", send_way=1
            )

        # ⭐ 区分器：空 data **原样放行**成 `{}`（⛔ 不是抛错、也不是兜成别的形状）。
        self.assertEqual(result, {})
        # URL 路径逐字（⛔ 不拿常量比自己——那是自指断言，E-0024）。
        self.assertTrue(
            captured["url"].startswith(
                DEFAULT_BASE_URL + "/api/open/trade/logistics/dummy/send?"
            ),
            captured["url"],
        )
        # 请求体 JSON：业务参数**只有**这两个（spec §3.2），且 `send_way` 就是传进去的那个。
        self.assertEqual(captured["body"], '{"order_no":"22000000000000000009","send_way":1}')
        # query 里三件套齐（签名参数，spec §2.3）。
        for needle in ("appid=AK", "timestamp=", "sign="):
            self.assertIn(needle, captured["url"])
        # ⛔ `app_secret` 绝不进请求（与上面 `order_detail` 那条同口径）。
        self.assertNotIn("SK", captured["url"].replace("sign=", ""))

    def test_a_business_failure_envelope_carries_the_upstream_text(self):
        """`code != 0` ⇒ `XgjError`，且 `upstream_msg` 是**上游原文**（⛔ 不加我们的前缀）。"""
        payload = {"code": 100004, "msg": "店铺无此订单信息", "data": {}}
        with mock.patch("apps.business.xgj._post_json", return_value=payload):
            with self.assertRaises(XgjError) as ctx:
                logistics_dummy_send(
                    self.TB_NO, app_key="AK", app_secret="SK", send_way=1
                )
        self.assertEqual(ctx.exception.upstream_code, 100004)
        self.assertEqual(ctx.exception.upstream_msg, "店铺无此订单信息")
