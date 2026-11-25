"""读单端点：成功换算、四类上游失败、入参校验、授权码 strip、凭证不外泄。

HTTP 打桩打在 `platform_order_views` 里**被查找的那个名字**上（E-0029：桩要打在
「被查找的模块」，不是「定义它的模块」）——`order_detail` 在本模块 `from ... import` 进来，
所以桩必须打 `apps.business.api.platform_order_views.order_detail`；打在
`apps.business.agiso.order_detail` 上不会生效。

⚠️ 本端点是 `urls.py` 里**显式的 `path()`**（不是 `@action`），**不在**
`test_no_route_500` 的两条腿覆盖范围内（`_explicit_routes()` 会跳过「不接受 GET」的路由）
⇒ 它的畸形入参覆盖由本文件自己给（spec §10-10）。
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest import mock

from django.test import override_settings
from django_tenants.test.cases import TenantTestCase

from apps.business.models import OrderSource, ShopBinding
from tests.p5_auth_helpers import make_logged_in_client

#: 读单端点。路径是 `/api/business/read-platform-order/`（独立 ViewSet + 显式 path）。
URL = "/api/business/read-platform-order/"


# ⚠️ **凭证必须显式给**：本机 `.env` **没有** AGISO 凭证（`settings.py` 的默认值是空串，
#    `test_agiso_client.py` 也记着这个事实），而「平台级凭证为空 ⇒ 1015」（spec §10-5）是
#    **正确行为** ⇒ 不给这一层，本文件里凡是走成功路径的用例都会拿到 1015。
#    ⚠️ **不能用类级 `@override_settings`**：`django_tenants.test.cases.TenantTestCase.setUpClass`
#    没有转调 `SimpleTestCase.setUpClass`（类级覆盖是在那里 `enable()` 的，本仓实测），
#    所以凭证在 `setUp` 里显式 `enable()` + `addCleanup(disable)`。写法**字面量**、不读环境。
class ReadPlatformOrderTests(TenantTestCase):
    def setUp(self):
        creds = override_settings(AGISO_APP_ID="app", AGISO_APP_SECRET="sec")
        creds.enable()
        self.addCleanup(creds.disable)
        self.client = make_logged_in_client(username="reader")
        self.source = OrderSource.objects.create(name="闲鱼A", shop_type=20)
        self.binding = ShopBinding.objects.create(order_source=self.source, access_token="tok")

    def _post(self, payload):
        # ⚠️ **显式 `json.dumps`**：真实客户端（前端 `JSON.stringify`）就是这么发的。
        #    直接把 Python 对象交给 Django 测试 client 时，**字符串**会被当成裸字节体
        #    （实测 `django/test/client.py:450` 的 `_encode_json` 只编码 dict/list/tuple）
        #    ⇒ 发出去的是**非法 JSON** `x`（DRF ParseError ⇒ 400），而不是「JSON 字符串体」，
        #    那测的就不是本条要测的「请求体本身不是对象 ⇒ 1000」（spec §6.2）。
        return self.client.post(URL, json.dumps(payload), content_type="application/json")

    def _upstream(self, data):
        return mock.patch(
            "apps.business.api.platform_order_views.order_detail", return_value=data
        )

    def _quiet(self):
        """吸收本视图的 WARNING：失败路径用例的判据是响应/日志内容，不是「有没有打日志」，
        别让 WARNING 行混进全量测试输出（测试输出 pristine）。⚠️ 被判据覆盖的失败路径一定会
        打这条 WARNING，所以 `assertLogs` 不会因为「一条都没记」而在退出时误报。
        """
        return self.assertLogs("apps.business.api.platform_order_views", level="WARNING")

    def test_payment_in_cents_is_converted_to_yuan(self):
        for cents, expect in ((1, "0.01"), (1234567, "12345.67"), (0, "0.00")):
            with self.subTest(cents=cents), self._upstream({"payment": cents, "buyer_nick": "小明"}):
                body = self._post({"order_source": self.source.id, "tid": "1520087523676842500"}).json()
                self.assertEqual(body["code"], 0)
                self.assertEqual(body["data"]["payment"], expect)
                self.assertEqual(body["data"]["buyer_nick"], "小明")

    def test_payment_is_a_string_with_two_decimals_not_a_float(self):
        """换算形状：**字符串**两位小数（与 `MONEY_FIELDS` 的序列化形状一致）。

        ⚠️ 判据是「形状」而不是「数值」：浮点实现（`raw / 100`）会回 `0.01`（float）
        ——JSON 里是数字而不是 `"0.01"`，前端 `val()` 回填的形状就变了。
        """
        with self._upstream({"payment": 1234567}):
            payment = self._post({"order_source": self.source.id, "tid": "1"}).json()["data"]["payment"]
        self.assertIsInstance(payment, str)
        self.assertEqual(payment, "12345.67")
        self.assertEqual(Decimal(payment), Decimal("12345.67"))

    def test_credentials_are_read_from_settings_not_from_the_request(self):
        with self._upstream({"payment": 1}) as patched, mock.patch(
            "apps.business.api.platform_order_views.settings"
        ) as settings:
            settings.AGISO_APP_ID, settings.AGISO_APP_SECRET = "app", "sec"
            self._post({"order_source": self.source.id, "tid": "1"})
        self.assertEqual(patched.call_args.kwargs["app_secret"], "sec")
        self.assertEqual(patched.call_args.args[1], "tok")

    def test_no_binding_returns_1015_and_does_not_call_upstream(self):
        other = OrderSource.objects.create(name="没配的", shop_type=20)
        with mock.patch("apps.business.api.platform_order_views.order_detail") as patched:
            body = self._post({"order_source": other.id, "tid": "1"}).json()
        self.assertEqual(body["code"], 1015)
        patched.assert_not_called()

    def test_empty_platform_credentials_return_1015_and_do_not_call_upstream(self):
        with mock.patch("apps.business.api.platform_order_views.settings") as settings, \
             mock.patch("apps.business.api.platform_order_views.order_detail") as patched:
            settings.AGISO_APP_ID = settings.AGISO_APP_SECRET = ""
            body = self._post({"order_source": self.source.id, "tid": "1"}).json()
        self.assertEqual(body["code"], 1015)
        patched.assert_not_called()

    def test_whitespace_only_token_is_treated_as_not_configured(self):
        """授权码是客服从阿奇索页面**复制粘贴**的，可能只剩换行/空格。

        ⚠️ strip 后为空 ⇒ 按「没配置」处理（`1015`），**不发**上游请求：否则
        `http.client.putheader` 会在 header 值里遇到换行时抛 `ValueError`，被
        `agiso._NETWORK_ERRORS` 收成 `AgisoError` ⇒ 白跑一单并回 `1017`（**不是** 500）。
        这条同时钉「不算已配置」与「没发请求」。
        """
        for blank in ("   ", "\r\n", " \t\n "):
            with self.subTest(blank=repr(blank)):
                ShopBinding.objects.filter(pk=self.binding.pk).update(access_token=blank)
                with mock.patch("apps.business.api.platform_order_views.order_detail") as patched:
                    body = self._post({"order_source": self.source.id, "tid": "1"}).json()
                self.assertEqual(body["code"], 1015)
                patched.assert_not_called()

    def test_token_is_stripped_before_being_sent_upstream(self):
        """粘贴进来的授权码带空白 ⇒ 上游拿到的必须是 strip 之后的干净值。

        ⚠️ 换行必须在到 `putheader` 之前就没了：含 `\\n` 的 header 值会让 `http.client`
        抛 `ValueError`，被收成 `AgisoError` ⇒ 回 `1017`（**不是** 500）。
        这里的判据正是「上游收到的值」。
        """
        ShopBinding.objects.filter(pk=self.binding.pk).update(access_token="  tok-e2e\r\n")
        with self._upstream({"payment": 1}) as patched:
            body = self._post({"order_source": self.source.id, "tid": "1"}).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(patched.call_args.args[1], "tok-e2e")

    def test_upstream_token_error_maps_to_1016(self):
        from apps.business.agiso import AgisoError
        with self._quiet(), mock.patch(
            "apps.business.api.platform_order_views.order_detail",
            side_effect=AgisoError(upstream_code=1, upstream_msg="Invalid access token"),
        ):
            body = self._post({"order_source": self.source.id, "tid": "1"}).json()
        self.assertEqual(body["code"], 1016)

    def test_unknown_upstream_error_maps_to_1017(self):
        from apps.business.agiso import AgisoError
        with self._quiet(), mock.patch(
            "apps.business.api.platform_order_views.order_detail",
            side_effect=AgisoError(upstream_code=99, upstream_msg="?"),
        ):
            body = self._post({"order_source": self.source.id, "tid": "1"}).json()
        self.assertEqual(body["code"], 1017)

    def test_malformed_inputs_never_500(self):
        for payload in ([], "x", {"order_source": "abc", "tid": "1"},
                        {"order_source": self.source.id, "tid": "abc"},
                        {"order_source": self.source.id, "tid": "1" * 200}, {"tid": "1"}):
            with self.subTest(payload=payload):
                res = self._post(payload)
                self.assertEqual(res.status_code, 200)
                self.assertIn("code", res.json())

    def test_a_body_that_is_not_json_at_all_is_400_not_500(self):
        """裸的非法 JSON 体 ⇒ **400**（`apps/core/exceptions.py` 有意保留 ParseError 的传输层语义），
        不是 500、也不是业务码。

        ⚠️ 这条与上面那条互补：上面显式 `json.dumps` 之后测的是「合法 JSON、但不是对象」⇒ `1000`；
        本条故意送**裸字节体**，钉住「请求本身不是 JSON」这一层仍然是 400。
        少了它，`_post` 里那个 `json.dumps` 会把这条路径整个从本端点挤出去。
        """
        res = self.client.post(URL, "definitely not json", content_type="application/json")
        self.assertEqual(res.status_code, 400, res.content[:200])
        self.assertEqual(res.json()["code"], 400, res.content[:200])

    def test_non_numeric_or_too_long_tid_never_reaches_upstream(self):
        """`tid` 只允许**ASCII 十进制**数字且 ≤64 位（阿奇索要 `Long`）：非数字/超长/Unicode 数字
        都在序列化器就挡掉，**不发**上游请求（spec §6.2 的入参校验；D8）。

        ⚠️ 最后一个是**阿拉伯-印度数字**（`\\d` 会匹配它、`[0-9]` 不会）：正则若写回 `\\d`，
        它会过校验、白跑一趟上游拿 `1017`，而「只能是数字」那句话也不再为真。
        """
        for tid in ("abc", "12a", "1.5", "1" * 65, "١٢٣"):
            with self.subTest(tid=tid):
                with mock.patch("apps.business.api.platform_order_views.order_detail") as patched:
                    body = self._post({"order_source": self.source.id, "tid": tid}).json()
                self.assertEqual(body["code"], 1000, body)
                patched.assert_not_called()

    def test_multiple_bindings_pick_the_lowest_id(self):
        second = ShopBinding.objects.create(order_source=self.source, access_token="tok-2")
        with self._upstream({"payment": 1}) as patched:
            self._post({"order_source": self.source.id, "tid": "1"})
        self.assertEqual(patched.call_args.args[1], "tok")  # 先建的那条（id 最小）
        self.assertGreater(second.id, self.binding.id)

    def test_empty_upstream_data_returns_the_empty_form_shape(self):
        """上游 `Data == {}` 是**合法**的（Task 1 的契约）：必须回完整形状、不得 500。

        缺省值：`payment` 空串（**后端**照实回空串；前端据它**不覆盖** `#price`，
        所以客服已填的价格不会被清空——判据在前端 `order-addorder.ts` 的
        `if (fields.payment !== '')`，用例见 `order-addorder.spec.ts`
        「价格只在平台侧非空时写」）、`remark` 空串、计数类回 `0`。

        ⚠️ per-source spec §5）出参**多了三个键**：`order_source` /
        `order_source_name` / `platform`——它们是"判定成了哪家店、走的哪条腿"的唯一依据
        （录单页靠 `order_source` 回填下拉）。这条用例按**整份载荷**断言，所以它会在
        这些键被增删时**正好**红——这是有意的（形状变了就有人看一眼）。
        """
        with self._upstream({}):
            body = self._post({"order_source": self.source.id, "tid": "1"}).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            body["data"],
            {"payment": "", "buyer_nick": "", "customer_phone": "",
             "remark": "", "order_status": 0, "pay_time": 0,
             # 手动给了来源 ⇒ 回的就是那个来源；这条腿走的是阿奇索
             "order_source": self.source.pk, "order_source_name": "闲鱼A", "platform": "agiso"},
        )

    def test_non_dict_item_does_not_500(self):
        """上游把 `item` 回成字符串（不是对象）时不得 `AttributeError` ⇒ 500。

        ⚠️ Task 1 的契约只保证 `Data` 是 dict，**不保证嵌套字段的类型**（第三方数据）。
        """
        with self._upstream({"payment": 1, "item": "不是对象"}):
            body = self._post({"order_source": self.source.id, "tid": "1"}).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["remark"], "")

    def test_unparseable_payment_is_1017_not_500(self):
        """上游把 `payment` 回成非数字时不得 `ValueError` ⇒ 500，而是按上游失败 `1017`。

        ⚠️ 这是**第三个**「形状不对就静默变成 500」的位置：Task 1 堵了 `Data` 不是 dict，
        这里堵 `Data` **里面**的标量类型。判据取「非 500 且是上游失败码」——
        不要静默兜成 `0.00`：那会往录单页填一个**错的**价格（算错钱的方向）。
        ⚠️ `float("inf")` / `float("-inf")` 是**独立**的一族：`json.loads` 默认接受非标准的
        `Infinity` / `-Infinity`，而 `int(inf)` 抛的是 `OverflowError`（**不是** `ValueError`）
        ——只捕 `(TypeError, ValueError)` 时它就是一条 500 路径。
        """
        for raw in ("abc", {"a": 1}, [1], float("inf"), float("-inf")):
            with self.subTest(raw=raw):
                with self._quiet(), self._upstream({"payment": raw}):
                    res = self._post({"order_source": self.source.id, "tid": "1"})
                self.assertEqual(res.status_code, 200, res.content[:200])
                self.assertEqual(res.json()["code"], 1017, res.content[:200])

    def test_infinite_pay_time_is_1017_not_500(self):
        """同一族再钉一次计数列（`_int_field` 是共用出口，但调用点必须都接上）。"""
        with self._quiet(), self._upstream({"payment": 1, "pay_time": float("inf")}):
            res = self._post({"order_source": self.source.id, "tid": "1"})
        self.assertEqual(res.status_code, 200, res.content[:200])
        self.assertEqual(res.json()["code"], 1017, res.content[:200])

    def test_pay_time_and_order_status_non_numeric_are_1017_not_500(self):
        for field in ("order_status", "pay_time"):
            with self.subTest(field=field):
                with self._quiet(), self._upstream({"payment": 1, field: "abc"}):
                    res = self._post({"order_source": self.source.id, "tid": "1"})
                self.assertEqual(res.status_code, 200, res.content[:200])
                self.assertEqual(res.json()["code"], 1017, res.content[:200])

    @override_settings(AGISO_APP_ID="app-LEAK-CANARY-1", AGISO_APP_SECRET="sec-LEAK-CANARY-2")
    def test_failure_response_does_not_leak_the_token_or_the_secret(self):
        """失败响应（1016）里**绝不能**出现授权码或平台密钥（spec §9-S3）。"""
        from apps.business.agiso import AgisoError
        ShopBinding.objects.filter(pk=self.binding.pk).update(access_token="tok-LEAK-CANARY-3")
        with self._quiet(), mock.patch(
            "apps.business.api.platform_order_views.order_detail",
            side_effect=AgisoError(upstream_code=1, upstream_msg="Invalid access token"),
        ):
            raw = self._post({"order_source": self.source.id, "tid": "1"}).content.decode()
        self.assertNotIn("tok-LEAK-CANARY-3", raw)
        self.assertNotIn("app-LEAK-CANARY-1", raw)
        self.assertNotIn("sec-LEAK-CANARY-2", raw)

    @override_settings(AGISO_APP_ID="app-LEAK-CANARY-1", AGISO_APP_SECRET="sec-LEAK-CANARY-2")
    def test_upstream_failure_logs_request_id_and_never_the_secrets(self):
        """上游失败要记 `RequestId`（排障时对着客服说的唯一凭据），但**不记**授权码/密钥。"""
        from apps.business.agiso import AgisoError
        ShopBinding.objects.filter(pk=self.binding.pk).update(access_token="tok-LEAK-CANARY-3")
        with mock.patch(
            "apps.business.api.platform_order_views.order_detail",
            side_effect=AgisoError(upstream_code=1, upstream_msg="Invalid access token",
                                   request_id="REQ-CANARY-9"),
        ):
            with self.assertLogs("apps.business.api.platform_order_views", level="WARNING") as captured:
                self._post({"order_source": self.source.id, "tid": "1"})
        text = "\n".join(captured.output)
        self.assertIn("REQ-CANARY-9", text)
        self.assertNotIn("tok-LEAK-CANARY-3", text)
        self.assertNotIn("sec-LEAK-CANARY-2", text)

    def test_token_echoed_in_the_upstream_error_is_redacted_from_the_log(self):
        """路径①：上游把收到的 `Authorization` **逐字**回显进 `Error_Msg` ⇒ 日志不得带它。

        ⚠️ 打桩 `order_detail` 是**对的**：本条要测的正是「视图拿到一个含 token 的
        `AgisoError.upstream_msg` 之后有没有脱敏」，不是 `agiso` 的翻译层。
        """
        from apps.business.agiso import AgisoError
        token = "tok-ECHO-CANARY-7"
        ShopBinding.objects.filter(pk=self.binding.pk).update(access_token=token)
        with mock.patch(
            "apps.business.api.platform_order_views.order_detail",
            side_effect=AgisoError(
                upstream_code=9, upstream_msg=f"auth failed for Bearer {token}",
            ),
        ):
            with self.assertLogs("apps.business.api.platform_order_views", level="WARNING") as captured:
                res = self._post({"order_source": self.source.id, "tid": "1"})
        text = "\n".join(captured.output)
        self.assertIn("Bearer ***", text)     # ★ 自证：脱敏**确实**作用在逐字形态上（否则下面两条空转）
        self.assertNotIn(token, text)
        self.assertNotIn(token, res.content.decode())
        self.assertEqual(res.json()["code"], 1017, res.content[:200])

    def test_token_echoed_in_the_request_id_is_redacted_from_the_log(self):
        """`RequestId` 与 `Error_Msg` **同源**（上游把 `Authorization` 回显进任一处都可能）
        ⇒ 它也必须过 `_redact_token`（本轮复评采纳项）。

        ⚠️ 判据分两层：`assertIn("REQ-***", …)` 先证明脱敏**真的跑到了 request_id 这一路**，
        再断言 token 片段不在日志里——只写后者时，若某天有人把 `_redact_token` 从 request_id
        上摘掉、而它恰好又不含 token，用例照样绿（同本轮修掉的「空转」形态）。
        """
        from apps.business.agiso import AgisoError
        token = "tok-REQID-CANARY-5"
        ShopBinding.objects.filter(pk=self.binding.pk).update(access_token=token)
        with mock.patch(
            "apps.business.api.platform_order_views.order_detail",
            side_effect=AgisoError(
                upstream_code=9, upstream_msg="boom", request_id=f"REQ-{token}",
            ),
        ):
            with self.assertLogs("apps.business.api.platform_order_views", level="WARNING") as captured:
                self._post({"order_source": self.source.id, "tid": "1"})
        text = "\n".join(captured.output)
        self.assertIn("REQ-***", text)        # ★ 自证：request_id 这一路确实脱敏了
        self.assertNotIn(token, text)

    def test_token_with_an_embedded_control_char_is_redacted_from_the_log(self):
        """路径②：token **中间**含裸 `\\n`（首尾的已被 `strip()` 去掉）⇒ `putheader` 抛错，
        而 `%r` 会把换行**转义** ⇒ 整个 token 不是逐字出现，**但换行两侧的片段逐字出现**。

        ⚠️ **不要打桩 `order_detail`**：本条要走的正是真实的 `_post_form` 翻译层
        （`putheader` 在连网之前就抛 `ValueError`，被 `_NETWORK_ERRORS` 收成 `AgisoError`）。
        ⚠️ 判据**必须断言两个片段**：只断言整条 token（`"CANARY-A\\nCANARY-B"`）在本机实测是
        **空转**——`token in msg` 为 `False`（换行被转义），那样写出来的用例永远绿，
        正是 E-0009 / E-0011 那一类假通过。
        ⚠️ 还要**自证这条路径真被走到**：`assertIn("Bearer ***", …)` 钉住「脱敏后的转义形态出现过」。
        少了它，将来 stdlib 若放宽换行判据（`putheader` 不再抛错），这条用例会**静默退化成空转**
        ——上下文里的 token 根本到不了 `ValueError`，否定断言恒真。
        """
        token = "CANARY-A\nCANARY-B"
        ShopBinding.objects.filter(pk=self.binding.pk).update(access_token=token)
        with override_settings(AGISO_API_BASE="https://example.invalid/x"):
            with self.assertLogs("apps.business.api.platform_order_views", level="WARNING") as captured:
                res = self._post({"order_source": self.source.id, "tid": "1"})
        text = "\n".join(captured.output)
        self.assertEqual(res.json()["code"], 1017, res.content[:300])
        self.assertIn("Bearer ***", text)      # ★ 自证：走到的确实是 putheader 的转义形态那条路
        self.assertNotIn("CANARY-A", text)
        self.assertNotIn("CANARY-B", text)
        self.assertNotIn(token, text)                 # 整条也不能在（上游逐字回显的情形）
        self.assertNotIn(token, res.content.decode())
        self.assertNotIn("CANARY-A", res.content.decode())
