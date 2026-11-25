"""`apps/realname/vendor.py`（本线**唯一出站点**）的判据 —— ⛔ 全程**零真实网络**。

## 打桩手法

照 `tests/_openapi_fixtures.py` 的 `_OpenApiUpstream`（本仓既有形态）：把 `httpx.Client` 换成
「走 `httpx.MockTransport` 的真客户端」，**只换 socket 那一层** —— URL、query、body、
`Authorization` 头都是**被测代码自己拼的**，所以判据能钉"发出去的形状"，而不只是"我们解包解得对"。

## 本文件覆盖的逐支清单（计划 T3）

1. `code:200` + `result` 为 `1`/`2`/`3`；2. `code:400`（**HTTP 200**）；3. `code:1000`；4. `code:9999`；
5. 超时；6. **非 JSON**（且**不抛**）；7. **HTTP 500 但 body 是合法信封**（按 body 的码判，两向都钉）；
8. ⭐ 签名与请求形状（**测试里独立实现一次 HMAC-SHA1**，⛔ 不用被测函数算期望值）；
9. ⭐ **不接桩对照**（`AGENTS.md` §5-E / E-0029：桩接没接上本身要有判据）；
10. ⭐ **`timeout` 真的递给了 `httpx.Client`**；
11. ⭐ **`code:200` 但 `data.result` 不可判定**（缺失 / 未登记的 int 如 `9` / 浮点 `2.0` / 布尔 /
    未登记的字符串）⇒ `ok=True` 但 `result_usable=False` 且 `vendor_result` 归一到 `""`
    ；
12. `code` 是 JSON `true`（布尔）⇒ 走 `network`，⛔ 不许因 `True == 1` 的巧合被当成 `code==200`；
13. `repr` 的**逐字全等**白名单；
14. ⭐⭐ **2026-10-10 实测契约：厂商 `data.result` 是 JSON 数字（int `1`/`2`/`3`）**——
    int `1`/`2`/`3` ⇒ `result_usable=True` 且 `vendor_result` **归一化成字符串**（下游 `services._decide`
    比的是 `== "1"`）；`true`/`false`（JSON bool）必须**单独排除**（`True == 1` 的陷阱，本判据的杀手判据）；
    `2.0`（float）不可用；字符串 `"1"/"2"/"3"` 作为**向后兼容**仍认。
    ⚠️ 证据等级 = **实测**：2026-10-10 在生产机上用**库里存的密钥真调厂商**抓到的原始回包
    （`data.result` 是 int），⛔ 不是推断、也⛔ 不是"官方文档这么写"。

⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：`*-synthetic-t3-*` 的凭据 + 一个不指向任何真人的
身份证号形状串；⛔ 里面没有一处真实密钥 / 真实证件号。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from unittest import mock

import httpx
from django.test import SimpleTestCase

from apps.realname import vendor

#: 真 `httpx.Client`：模块级取好 ⇒ 换掉 `httpx.Client` 之后仍造得出**真的**客户端。
_REAL_HTTPX_CLIENT = httpx.Client

#: 合成凭据（⛔ 一个真值都没有）。
KEY_ID = "key-id-synthetic-t3-0001"
KEY_SECRET = "key-secret-synthetic-t3-0001"
NAME = "测试姓名甲"
#: 合成证件号（18 位形状，⛔ 不指向任何真人）。
ID_CARD = "110101199003070000"


class _VendorStub:
    """零网络假厂商：`httpx.MockTransport` 只决定"对方回什么"，请求本身是真的。

    ⚠️ 记录**每一次**请求对象（含 raw content 与 headers）⇒ 用例能对真实发出的形状下断言。
    ⚠️ 也记录**每一次 `httpx.Client(...)` 收到的 kwargs**（`self.client_kwargs`）—— 那是"超时到底有没有
    真的递下去"唯一能下断言的地方（被测代码把超时交给**构造函数**，不会出现在请求对象上）。
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.client_kwargs: list[dict] = []
        self._reply: tuple[int, str, object] | None = None
        self._error: Exception | None = None

    def json_reply(self, payload, *, status: int = 200) -> "_VendorStub":
        self._reply = (status, "json", payload)
        return self

    def text_reply(self, text: str, *, status: int = 200) -> "_VendorStub":
        self._reply = (status, "text", text)
        return self

    def boom(self, exc: Exception) -> "_VendorStub":
        self._error = exc
        return self

    # ── httpx 接线 ────────────────────────────────────────────────────────────
    def factory(self, **kwargs):
        # ⚠️ 先记下来再动手：被测代码交给**构造函数**的东西（超时就是其一）只有这里看得到。
        self.client_kwargs.append(dict(kwargs))
        kwargs.pop("transport", None)
        return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(self.handler), **kwargs)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        status, kind, payload = self._reply
        if kind == "json":
            return httpx.Response(status, json=payload)
        return httpx.Response(status, text=str(payload))


class _StubbedVendorCase(SimpleTestCase):
    """接桩的用例基类（`httpx.Client` 换成走假厂商的真客户端）。"""

    def setUp(self):
        self.stub = _VendorStub()
        patcher = mock.patch.object(vendor.httpx, "Client", self.stub.factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def verify(self, **kwargs) -> vendor.VendorResult:
        return vendor.verify(NAME, ID_CARD, key_id=KEY_ID, key_secret=KEY_SECRET, **kwargs)


class VendorBranchTests(_StubbedVendorCase):
    """逐支覆盖 1–7：厂商的每一个失败档都要**返回**结果，⛔ 不许抛、⛔ 不许静默当成功。"""

    def test_code_200_result_1_2_3_are_all_ok(self):
        """`code==200` 是**唯一可计费**的档（一致 1 / 不一致 2 / 库无 3 都算"查得"）。

        ⚠️ 本用例走的是**字符串**形状（`"1"/"2"/"3"`，厂商**将来**若改回字符串的兼容面）；
        ⭐ 厂商**今天实测**回的是 **int** ⇒ 那一面由 `VendorResultUsabilityTests` 的
        `…int_result_1/2/3_is_usable` 三条各自独立钉住（改准，⛔ 别把两边合成一条）。
        """
        for result in ("1", "2", "3"):
            with self.subTest(result=result):
                self.stub.json_reply({
                    "msg": "查询成功", "requestid": f"req-synthetic-{result}", "code": 200,
                    "data": {"result": result, "message": f"结果描述{result}"},
                })
                got = self.verify()
                self.assertIs(got.ok, True)
                self.assertEqual(got.error_kind, "ok")
                self.assertEqual(got.vendor_code, 200)
                self.assertEqual(got.vendor_result, result)
                self.assertEqual(got.vendor_message, f"结果描述{result}")
                self.assertEqual(got.vendor_request_id, f"req-synthetic-{result}")

    def test_code_400_on_http_200_is_a_failure(self):
        """⭐ 实测：`code:400` 挂在 **HTTP 200** 上 ⇒ 判成败只看 body 的码（⛔ 不看状态码）。

        （`AGENTS.md` §5-E；同类先例 `apps/business/peer_client.py` 的 `_post_envelope`。）
        """
        self.stub.json_reply({"msg": "身份证号格式错误", "requestid": "", "code": 400, "data": None})
        got = self.verify()
        self.assertIs(got.ok, False)
        self.assertEqual(got.error_kind, "invalid")
        self.assertEqual(got.vendor_code, 400)
        self.assertEqual(got.vendor_result, "")
        self.assertEqual(got.vendor_message, "身份证号格式错误")  # `data` 空 ⇒ 退到顶层 `msg`
        self.assertEqual(got.vendor_request_id, "")  # ⚠️ 参数错时厂商回**空串**（实测）

    def test_code_1000_is_vendor_error(self):
        self.stub.json_reply({"msg": "核验中心异常", "requestid": "req-synthetic-1000",
                              "code": 1000, "data": None})
        got = self.verify()
        self.assertIs(got.ok, False)
        self.assertEqual(got.error_kind, "vendor_error")
        self.assertEqual(got.vendor_code, 1000)

    def test_code_9999_is_vendor_error(self):
        self.stub.json_reply({"msg": "服务异常", "requestid": "req-synthetic-9999",
                              "code": 9999, "data": None})
        got = self.verify()
        self.assertIs(got.ok, False)
        self.assertEqual(got.error_kind, "vendor_error")
        self.assertEqual(got.vendor_code, 9999)

    def test_unregistered_non_200_code_is_never_ok(self):
        """未登记的非 200 码（这里用 500）归 `vendor_error`，⛔ **不许**静默当成功（E-0087 族）。"""
        self.stub.json_reply({"msg": "未知码", "requestid": "req-synthetic-unknown",
                              "code": 500, "data": None})
        got = self.verify()
        self.assertIs(got.ok, False)
        self.assertEqual(got.error_kind, "vendor_error")
        self.assertEqual(got.vendor_code, 500)

    def test_timeout_is_network(self):
        self.stub.boom(httpx.ReadTimeout("synthetic read timeout"))
        got = self.verify()
        self.assertIs(got.ok, False)
        self.assertEqual(got.error_kind, "network")
        self.assertEqual(got.vendor_code, 0)  # 什么都没拿到
        self.assertEqual(got.vendor_result, "")
        # ⚠️ 人话里**不许**带 httpx 的原始异常文本（它含完整 URL，query 里有姓名与证件号）。
        self.assertNotIn("synthetic read timeout", got.vendor_message)

    def test_connect_error_is_network(self):
        self.stub.boom(httpx.ConnectError("synthetic dns failure"))
        got = self.verify()
        self.assertIs(got.ok, False)
        self.assertEqual(got.error_kind, "network")

    def test_non_json_response_is_network_and_does_not_raise(self):
        """⛔ `json.JSONDecodeError` 不许漏出去变 500：网关 / WAF 回 HTML 是可预期现象。"""
        self.stub.text_reply("<html>502 Bad Gateway</html>", status=200)
        got = self.verify()  # ⛔ 这一行不抛异常本身就是判据
        self.assertIs(got.ok, False)
        self.assertEqual(got.error_kind, "network")
        self.assertEqual(got.vendor_code, 0)

    def test_non_object_json_is_network(self):
        """回了裸列表 = 形状变了，⛔ 不静默当空信封（`peer_client` 同款口径）。"""
        self.stub.json_reply(["not", "an", "envelope"])
        got = self.verify()
        self.assertIs(got.ok, False)
        self.assertEqual(got.error_kind, "network")

    def test_json_without_judgeable_code_is_network(self):
        """连"哪一档"都判不出来（没有整数 `code`）⇒ 形状不对，⛔ 不许拿 0 假装读到了码。"""
        self.stub.json_reply({"msg": "形状变了", "requestid": "req-synthetic-no-code", "data": None})
        got = self.verify()
        self.assertIs(got.ok, False)
        self.assertEqual(got.error_kind, "network")
        self.assertEqual(got.vendor_code, 0)

    def test_http_500_with_failure_envelope_is_judged_by_the_body_code(self):
        """⭐ HTTP 500 **不是**判据：body 是合法失败信封 ⇒ 按 body 的码判成 `vendor_error`。

        ⛔ 不许因为"HTTP 不是 200"就改判成 `network` —— 那会把"厂商明确告诉你异常了"读成"我们没连上"，
        而这两档在扣费口径上可能不同（设计基线 §6-2b）。
        """
        self.stub.json_reply({"msg": "服务异常", "requestid": "req-synthetic-500",
                              "code": 1000, "data": None}, status=500)
        got = self.verify()
        self.assertIs(got.ok, False)
        self.assertEqual(got.error_kind, "vendor_error")
        self.assertEqual(got.vendor_code, 1000)

    def test_http_500_with_success_envelope_is_still_ok(self):
        """反向也钉一次：判成败**只认 body 的码**，两个方向都不许受 HTTP 状态码影响。"""
        self.stub.json_reply({"msg": "查询成功", "requestid": "req-synthetic-500-ok", "code": 200,
                              "data": {"result": "1", "message": "一致"}}, status=500)
        got = self.verify()
        self.assertIs(got.ok, True)
        self.assertEqual(got.error_kind, "ok")
        self.assertEqual(got.vendor_result, "1")


    def test_code_is_json_true_is_network_not_ok(self):
        """⭐ `"code": true`（JSON 的 `true`）⇒ 走 `network`（fail-closed）。

        ⚠️ Python 里 `True == 1`、且 `isinstance(True, int)` 为真 ⇒ "读到一个整数码"这种宽松判据
        会把这个形状当成**读到了码**；更糟的是如果拿 `code == 200` 之外还顺手做了算术，
        `True` 会悄悄变成 `1`。形状不对时唯一正确的处置是**判不出来就 fail-closed**（`AGENTS.md` §5-E / E-0010）。
        ⛔ 本用例钉的是"不许因为巧合把它当成一个码"，⛔ 不是"布尔码要当什么档"。
        """
        self.stub.json_reply({"msg": "形状变了", "requestid": "req-synthetic-bool-code",
                              "code": True, "data": {}})
        got = self.verify()
        self.assertIs(got.ok, False)
        self.assertEqual(got.error_kind, "network")
        self.assertEqual(got.vendor_code, 0)  # ⛔ 不许把 `True` 当 `1` 留下


class VendorTimeoutWiringTests(_StubbedVendorCase):
    """⭐ 修复轮 R1-1：`timeout` 到底有没有递给 `httpx.Client`。

    ⛔ 为什么需要这一类：超时是**构造参数**，不会出现在请求对象上（`VendorRequestShapeTests` 那一类
    打的是请求形状，看不见它）⇒ 删掉 `httpx.Client(timeout=timeout)` 里的 `timeout=timeout`，
    本文件别的用例**一条都不会红**（httpx 会退回自己的默认值，而我们这一档"算不算失败"正是由超时决定的）。

    打桩位置：`_VendorStub.factory` 记下了每次 `httpx.Client(...)` 收到的 kwargs ⇒ 判据打在"真实传参"上。
    """

    def test_default_timeout_is_passed_to_the_client(self):
        """不显式给超时时，递下去的必须是模块常量 `DEFAULT_TIMEOUT_SECONDS`（10.0 秒）。"""
        self.stub.json_reply({"msg": "查询成功", "requestid": "req-synthetic-timeout-default",
                              "code": 200, "data": {"result": "1", "message": "一致"}})
        self.verify()
        self.assertEqual(len(self.stub.client_kwargs), 1, "一次核验只许造一个客户端")
        self.assertEqual(self.stub.client_kwargs[0].get("timeout"), 10.0)
        # ⛔ 与"我们自己的常量"对齐也要钉一次：有人把常量改成别的值、又忘了它是不是被用上时，上面那条会红。
        self.assertEqual(self.stub.client_kwargs[0].get("timeout"), vendor.DEFAULT_TIMEOUT_SECONDS)

    def test_explicit_timeout_is_passed_through_verbatim(self):
        """显式给 `timeout=3.5` 时，递下去的必须**就是** `3.5`（⛔ 不许被吞掉、⛔ 不许被四舍五入成别的）。"""
        self.stub.json_reply({"msg": "查询成功", "requestid": "req-synthetic-timeout-explicit",
                              "code": 200, "data": {"result": "2", "message": "不一致"}})
        self.verify(timeout=3.5)
        self.assertEqual(len(self.stub.client_kwargs), 1, "一次核验只许造一个客户端")
        self.assertEqual(self.stub.client_kwargs[0].get("timeout"), 3.5)


class VendorResultUsabilityTests(_StubbedVendorCase):
    """⭐ 修复轮 R1-2（2026-10-10 改准）：`code:200` 时 `data.result` 的形状判定。

    口径（spec §3 + §5 第 ⑦ 步 / 模块 docstring 第二节与第六节）：
    `code==200` 时 `data.result` 的**实测**形状是 **int `1`/`2`/`3`**（1 一致 / 2 不一致 / 3 库无）
    ⇒ 这三种 `result_usable=True`（`vendor_result` 归一化成字符串）。
    ⛔ **不是**"必须是字符串"：那是 R1 的错结论，会让真实核验**永远**落 fail 且照扣费。
    其余形状（缺 `result` / 未登记的 int `9` / 浮点 `2.0` / 布尔 / 未登记的字符串）⇒ `ok` 仍是 `True`
    （"查得"是事实、我们已被计费，⛔ **不许谎报成失败**），但 `vendor_result` 归一到 `""`
    且 `result_usable=False`，让上层能区分"查得且拿到结果"与"查得但拿不到可判定的结果"。

    ⚠️ `result_usable` ⛔ **不是成败判据**（成败判据是 `ok`）：上层照 `ok` 扣次数、
    照 `result_usable` 决定能不能把这次读成 pass（`False` ⇒ 落 `result="fail"` 并**算作已消耗**）。

    ⚠️ **本类的每一条各自独立**（各自一条用例、各自一条回包）：变异掉 `vendor.py` 里对应的那一支时，
    只有对应那条（或那几条）红，⛔ 不许把它们并成一条 `toContain` 式的宽断言。
    """

    def test_code_200_without_result_is_ok_but_unusable(self):
        """`{"code":200,"data":{}}`：`data` 在、但没有 `result` 键。"""
        self.stub.json_reply({"msg": "查询成功", "requestid": "req-synthetic-no-result",
                              "code": 200, "data": {}})
        got = self.verify()
        self.assertIs(got.ok, True)  # ⛔ 我们确实查得了（已被计费）⇒ 不许说成失败
        self.assertEqual(got.error_kind, "ok")
        self.assertIs(got.result_usable, False)
        self.assertEqual(got.vendor_result, "")

    def test_code_200_with_int_result_1_is_usable(self):
        """⭐⭐ **实测形状**：`result: 1`（**int**）⇒ 可用、`vendor_result == "1"`（1 = 一致）。

        ⚠️ 证据等级 = **实测**（生产机真调厂商，`data.result` 是 JSON 数字），⛔ 不是推断。
        ⛔ 这一条与下面那条 `result: 2` 一起，就是"真实核验到底能不能通过"的判据。
        """
        self.stub.json_reply({"msg": "成功", "requestid": "req-synthetic-int-1",
                              "code": 200, "data": {"result": 1, "message": "核验一致"}})
        got = self.verify()
        self.assertIs(got.ok, True)
        self.assertIs(got.result_usable, True)
        self.assertEqual(got.vendor_result, "1")  # ⭐ 归一化成**字符串**（下游比的是 == "1"）
        self.assertIsInstance(got.vendor_result, str)

    def test_code_200_with_int_result_2_is_usable(self):
        """⭐⭐ **实测**：`result: 2`（**int**）⇒ `result_usable is True` 且 `vendor_result == "2"`。

        ⛔ **这条是实测，不是推断**：2026-10-10 在生产机上用**库里存的密钥真调厂商**，抓到的原始回包是
        `{"msg":"成功","requestid":"<厂商流水号，此处占位>","code":200,"data":{"result":2,"message":"核验不一致"}}`
        —— `data.result` 是 **JSON 数字（int `2`）**，⛔ 不是字符串。

        ⚠️ **本用例的前身是 `test_code_200_with_numeric_result_is_ok_but_unusable`**，
        它把"数字一律不可用"这个**错结论锁住了** ⇒ 真实响应永远 `result_usable=False` ⇒ 上层落
        `result="fail"` 且照扣 ⇒ **姓名与证件号完全一致的用户也永远通不过实名**。
        R1 担心的方向（`bool` 是 `int` 的子类、`True == 1`）是对的，但结论错了：正确做法是
        **接受 int 1/2/3 同时单独排除 bool** ⇒ 本用例与 `…json_true_result_is_unusable` 是**一对**，
        缺了任何一边，这个判据就又能被写坏。
        """
        self.stub.json_reply({"msg": "成功", "requestid": "req-synthetic-int-2",
                              "code": 200, "data": {"result": 2, "message": "核验不一致"}})
        got = self.verify()
        self.assertIs(got.ok, True)
        self.assertIs(got.result_usable, True)
        self.assertEqual(got.vendor_result, "2")
        self.assertIsInstance(got.vendor_result, str)  # 归一化的契约：交下游的是字符串

    def test_code_200_with_int_result_3_is_usable(self):
        """⭐ **实测形状**：`result: 3`（**int**）⇒ 可用、`vendor_result == "3"`（3 = 库无）。

        ⚠️ 证据等级 = **实测**（同上）。⚠️ "库无"仍然**可用**：它有确定语义（查无此人），
        与"拿不到可判定的结果"是两回事（后者才是 `result_usable=False`）。
        """
        self.stub.json_reply({"msg": "成功", "requestid": "req-synthetic-int-3",
                              "code": 200, "data": {"result": 3, "message": "库无"}})
        got = self.verify()
        self.assertIs(got.ok, True)
        self.assertIs(got.result_usable, True)
        self.assertEqual(got.vendor_result, "3")

    def test_code_200_with_unregistered_int_result_is_ok_but_unusable(self):
        """`result: 9`（**int**、未登记）⇒ 不可用、`""`（⛔ 白名单之外不猜、不当已知结果用）。"""
        self.stub.json_reply({"msg": "查询成功", "requestid": "req-synthetic-int-9",
                              "code": 200, "data": {"result": 9}})
        got = self.verify()
        self.assertIs(got.ok, True)
        self.assertIs(got.result_usable, False)
        self.assertEqual(got.vendor_result, "")

    def test_code_200_with_float_result_is_ok_but_unusable(self):
        """⭐ `result: 2.0`（**float**，JSON 里的 `2.0`）⇒ 不可用、`""`。

        ⛔ 别让 `2.0` 混进来：它**不是 int**（`isinstance(2.0, int)` 为假），而 `2.0 == 2` 为真
        ⇒ 拿 `==` 直接比数字的白名单判据（或先 `int()` 归一化）会把它当成"不一致"。
        形状变了就 fail-closed（`AGENTS.md` §5-E / E-0087 族）。
        """
        self.stub.json_reply({"msg": "查询成功", "requestid": "req-synthetic-float-2",
                              "code": 200, "data": {"result": 2.0}})
        got = self.verify()
        self.assertIs(got.ok, True)
        self.assertIs(got.result_usable, False)
        self.assertEqual(got.vendor_result, "")

    def test_code_200_with_json_true_result_is_ok_but_unusable(self):
        """⭐⭐ **杀手判据**：`result: true`（JSON 布尔）⇒ 不可用、`""`。

        ⚠️ **为什么它必须单独存在**：Python 里 `bool` 是 `int` 的**子类**、且 `True == 1`、
        `True in {1, 2, 3}` 也**为真** ⇒ 把 `bool` 的排除删掉（只判 `int`），JSON 的 `true`
        会被**静默读成"一致"**。
        ⇒ 归一化里 `isinstance(vendor_result, bool)` **必须排在 int 分支之前**；
        删掉那一支，本用例**必须红**（这也是它与 `…int_result_1_is_usable` 互为对照的原因：
        一条要 `True` 可用、一条要 `True` 不可用，从两边夹住那个判据）。
        """
        self.stub.json_reply({"msg": "成功", "requestid": "req-synthetic-bool-result-true",
                              "code": 200, "data": {"result": True, "message": "核验一致"}})
        got = self.verify()
        self.assertIs(got.ok, True)  # ⛔ 我们确实查得了（已被计费）⇒ 不许说成失败
        self.assertEqual(got.error_kind, "ok")
        self.assertIs(got.result_usable, False)
        self.assertEqual(got.vendor_result, "")  # ⛔ 不许是 "1"、也不许是 "True"

    def test_code_200_with_json_false_result_is_ok_but_unusable(self):
        """⭐ 上面那条的镜像：`result: false`（JSON 布尔）⇒ 不可用、`""`。

        ⚠️ 两个方向都钉：`False == 0`、而 `0` 本来就不在白名单里 ⇒ 只钉 `true` 的话，
        一个"把 bool 转成 int 再判"的写法会在 `false` 这边看起来是对的（恰好落进 `else`）——
        那条路径其实就是 `true` 会被读成"一致"的同一个 bug（本仓已有同款先例：`code: true` 那条用例）。
        """
        self.stub.json_reply({"msg": "成功", "requestid": "req-synthetic-bool-result-false",
                              "code": 200, "data": {"result": False}})
        got = self.verify()
        self.assertIs(got.ok, True)
        self.assertIs(got.result_usable, False)
        self.assertEqual(got.vendor_result, "")

    def test_code_200_with_unregistered_result_is_ok_but_unusable(self):
        """`{"code":200,"data":{"result":"9"}}`：字符串、但不在白名单里（⛔ 不猜、不当已知结果用）。"""
        self.stub.json_reply({"msg": "查询成功", "requestid": "req-synthetic-unregistered-result",
                              "code": 200, "data": {"result": "9"}})
        got = self.verify()
        self.assertIs(got.ok, True)
        self.assertIs(got.result_usable, False)
        self.assertEqual(got.vendor_result, "")

    def test_code_200_with_string_result_2_is_usable(self):
        """⭐ **向后兼容**：`result: "2"`（**字符串**）⇒ 可用、`vendor_result == "2"`。

        ⚠️ 为什么这一条要留着：厂商**今天实测**回的是 int，但"只认字符串"与"只认 int"是**同一个错误的
        两个方向** —— 判据要按实测形状写，同时**别把另一种形状当失败**（厂商哪天改回字符串，
        我们会以"每一次核验都判 fail 且照扣"的形式静默坏掉）。⇒ 两种形状都认，但都过**同一份白名单**。
        """
        self.stub.json_reply({"msg": "成功", "requestid": "req-synthetic-string-2",
                              "code": 200, "data": {"result": "2", "message": "核验不一致"}})
        got = self.verify()
        self.assertIs(got.ok, True)
        self.assertIs(got.result_usable, True)
        self.assertEqual(got.vendor_result, "2")

    def test_code_200_with_registered_result_is_usable(self):
        """正面（字符串形状）：`{"code":200,"data":{"result":"1"}}` ⇒ `result_usable=True`
        （⛔ 否则本类那几条"不可用"的用例恒真、等于没钉）。
        """
        self.stub.json_reply({"msg": "查询成功", "requestid": "req-synthetic-usable-result",
                              "code": 200, "data": {"result": "1", "message": "一致"}})
        got = self.verify()
        self.assertIs(got.ok, True)
        self.assertIs(got.result_usable, True)
        self.assertEqual(got.vendor_result, "1")

    def test_non_200_is_never_result_usable(self):
        """非 200 档（这里 `code:400`）一律 `result_usable=False` —— 它连"查得"都不是。"""
        self.stub.json_reply({"msg": "身份证号格式错误", "requestid": "", "code": 400, "data": None})
        got = self.verify()
        self.assertIs(got.ok, False)
        self.assertIs(got.result_usable, False)
        self.assertEqual(got.vendor_result, "")


class VendorRequestShapeTests(_StubbedVendorCase):
    """⭐ 判据 8：拿**真实发出的那个请求**钉签名与形状（⛔ 不靠"我们解包解得对"）。"""

    def test_request_method_query_body_headers_and_signature(self):
        self.stub.json_reply({"msg": "查询成功", "requestid": "req-synthetic-shape", "code": 200,
                              "data": {"result": "1", "message": "一致"}})
        got = self.verify()
        self.assertIs(got.ok, True)

        self.assertEqual(len(self.stub.requests), 1, "一次核验只许发一个请求")
        request = self.stub.requests[0]

        # ── URL / 方法 / 参数位置 ────────────────────────────────────────────────
        self.assertEqual(request.method, "POST")  # 契约：只能 POST（GET 会 404）
        self.assertEqual(request.url.scheme, "https")
        self.assertEqual(request.url.host, "ap-shanghai.cloudmarket-apigw.com")
        self.assertEqual(request.url.path, "/service-olvzprl7/id_name/check")
        params = dict(request.url.params)
        self.assertEqual(params.get("name"), NAME)
        self.assertEqual(params.get("idcard"), ID_CARD)

        # ── body 是**空表单** ──────────────────────────────────────────────────
        self.assertEqual(request.content, b"")
        self.assertEqual(request.headers.get("content-type"), "application/x-www-form-urlencoded")

        # ── Authorization 是一段 JSON，签名**独立算一遍**来对 ────────────────────
        authorization = json.loads(request.headers["authorization"])  # 解不开就直接红
        self.assertEqual(set(authorization), {"id", "x-date", "signature"})
        self.assertEqual(authorization["id"], KEY_ID)

        x_date = authorization["x-date"]
        # 时间格式逐字钉住（`%a, %d %b %Y %H:%M:%S GMT`，UTC）：strptime 解不开就红。
        self.assertTrue(x_date.endswith(" GMT"))
        datetime.strptime(x_date, "%a, %d %b %Y %H:%M:%S GMT")  # 格式不符 ⇒ 这里抛 ValueError

        # ⛔ **期望值在这里独立实现**（hmac + hashlib + base64 各来一次），
        # ⛔ **不调用 `vendor._signature`** —— 拿被测函数算期望值等于用自己证明自己。
        expected = base64.b64encode(
            hmac.new(KEY_SECRET.encode("utf-8"),
                     f"x-date: {x_date}".encode("utf-8"),
                     hashlib.sha1).digest()
        ).decode("ascii")
        self.assertEqual(authorization["signature"], expected)

        # ── request-id 头在（契约里可带，照官方示例带一个小写 uuid1） ─────────────
        self.assertEqual(request.headers["request-id"], request.headers["request-id"].lower())
        self.assertEqual(len(request.headers["request-id"]), 36)


class VendorConfigGuardTests(SimpleTestCase):
    """编程错误**响亮地抛**，但⛔ 不把凭据带出去。"""

    def test_blank_credentials_raise_without_echoing_the_secret(self):
        with self.assertRaises(vendor.VendorConfigError) as ctx:
            vendor.verify(NAME, ID_CARD, key_id="", key_secret=KEY_SECRET)
        message = str(ctx.exception)
        self.assertNotIn(KEY_SECRET, message)
        self.assertIn("key_id", message)
        # ⛔ 不 `raise … from e`：那会把带签名头的请求详情挂到异常链上（设计基线 §6-3）。
        self.assertIsNone(ctx.exception.__cause__)
        self.assertIsNone(ctx.exception.__context__)

    def test_repr_is_an_explicit_whitelist(self):
        """⭐ `__repr__` 覆写是**给将来加字段用的**（今天本类没有密钥字段，所以它与自动 repr 同形）。

        判据写**逐字全等**：将来有人往 dataclass 里加一个字段、又顺手删掉这个覆写，
        自动 repr 会多出那个字段 ⇒ 本用例立刻红（那正是"密钥被印进日志"的形状）。

        ⚠️ **它的证明力边界（如实登记，别读多）**：
        - 今天 `VendorResult` **没有**任何密钥字段 ⇒「这条用例在防密钥泄漏」**今天不可证**
          （`assertNotIn(KEY_SECRET, …)` 那行今天必然通过：被印出来的字符串里压根不存在密钥）。
        - 它今天真正能抓的只有一件事：**"将来加了字段、又顺手删掉/漏改这个覆写"**；
          抓法是"repr 的字段清单**被逐字钉住**"——覆写一旦消失，自动 repr 会照新字段清单印，
          本用例红。**新增字段时本用例也会红**（那是有意的：加字段必须同一次把白名单与这条断言一起改）。
        - ⛔ 反过来说：**它证明不了"覆写里的白名单是全的"**——白名单是不是恰好该包含这些字段，
          得靠人读（`result_usable` 就是本轮新加进来、由这条断言逼着一起改的）。
        """
        got = vendor.VendorResult(ok=False, result_usable=False, vendor_code=400, vendor_result="",
                                  vendor_message="身份证号格式错误", vendor_request_id="",
                                  error_kind="invalid")
        self.assertEqual(
            repr(got),
            "VendorResult(ok=False, result_usable=False, vendor_code=400, vendor_result='', "
            "vendor_message='身份证号格式错误', vendor_request_id='', error_kind='invalid')",
        )
        self.assertNotIn(KEY_SECRET, repr(got))


class VendorUnstubbedControlTests(SimpleTestCase):
    """⭐ 判据 9：**不接桩对照** —— 桩接没接上本身要有判据（`AGENTS.md` §5-E / E-0029）。

    ⚠️ 本类**显式不接桩**、打的是**真实域名**，但给一个必然超时的 `timeout=0.001`
    ⇒ 它**不依赖外网**（连不上 / 超时都归 `network`，不需要真连上任何东西）。
    它回答的是"没桩的时候它确实会失败"，也就是"上面那些用例不是恒真的"。

    ⚠️ **它不构成"超时被递下去了"的判据**（⚠️ 本轮 R1-1 补的边界，别把它读多）：
    这里给的 `0.001` 只是让真请求尽快收场；即使 `verify` 把 `timeout` 整个吞掉、httpx 退回自己的默认值，
    本用例**照样会绿**（连不上 / 超时都归 `network`）。"超时到底有没有递下去"由
    `VendorTimeoutWiringTests` 那两条钉（判据打在 `httpx.Client(...)` 真实收到的 kwargs 上）。
    """

    def test_without_any_stub_the_real_endpoint_call_fails_as_network(self):
        got = vendor.verify(NAME, ID_CARD, key_id=KEY_ID, key_secret=KEY_SECRET, timeout=0.001)
        self.assertIs(got.ok, False)
        self.assertEqual(got.error_kind, "network")
        self.assertEqual(got.vendor_code, 0)
        self.assertNotEqual(got.vendor_message, "")
