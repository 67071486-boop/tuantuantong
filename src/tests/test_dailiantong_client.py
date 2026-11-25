"""`apps.business.dailiantong_client` 的协议层网（**零网络**：全部走 `httpx.MockTransport`）。

三件要钉住的事：

1. **签名 / 请求体**按实测真相来（`docs/DAILIANTONG-API.md` §2.1：值按发送顺序拼、
   `Action` 的值在最前、不含参数名、32 位小写），并有**固定输入 → 固定 md5** 的自证向量；
2. **三种响应信封**都能吃下（`Result` 数字 / 字符串、裸对象、列表的空字符串 vs 数组、
   二次转义 Unicode）；
3. **⛔ 重试红线**：只读动作遇到连接失败 / 超时最多重试 1 次；**发单等写动作一次都不重试**
   （实测外部单号查不回来，重试 = 真钱重复建单）。

⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：AppID / 密钥 / 支付密码 md5 / 单号 /
标题都是编的，⛔ 不抄 `.tools/recon/` 里的真实凭据与真实单号。真实凭据从不进 git。
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qsl

import httpx
from django.test import SimpleTestCase

from apps.business.dailiantong_client import (
    ACTION_ADD_BLACKLIST,
    ACTION_ADD_ORDER,
    ACTION_CANCEL_ORDER,
    ACTION_ORDER_LOCK,
    ACTION_ORDER_STAT,
    ACTION_ORDER_VALUE,
    ACTION_OFF_SELL,
    ACTION_REQUIRE_TEMP_ADD,
    ACTION_REQUIRE_TEMP_DELETE,
    ACTION_REQUIRE_TEMP_LIST,
    ACTION_REQUIRE_TEMP_SET_DEFAULT,
    ACTION_REQUIRE_TEMP_UPDATE,
    ACTION_REQUEST_ARBITRATION,
    ACTION_DELETE_ORDER,
    ACTION_FINISH_ORDER,
    ACTION_MESSAGE_ADD,
    ACTION_MESSAGE_LIST,
    ACTION_MODIFY_ORDER,
    ACTION_MODIFY_PRICE,
    ACTION_MODIFY_SPECIAL,
    ACTION_MONEY_CHANGE_LIST,
    ACTION_ORDER_DETAIL,
    ACTION_ORDER_STATUS_LIST,
    ACTION_PROGRESS_ADD,
    ACTION_PROGRESS_LIST,
    ACTION_PUBLISH_LIST,
    ACTION_GET_STS,
    ACTION_RECOMMEND_PRICE,
    ACTION_UP_PRICE,
    ACTION_USER_MONEY,
    ACTION_ZONES,
    BLACKLIST_FLAG_REMOVE,
    CANCEL_FLAG_AGREE,
    CANCEL_FLAG_APPLY,
    CANCEL_FLAG_CANCEL,
    DEFAULT_BASE_URL,
    EVAL_TYPE_VERY_SATISFIED,
    LOCK_FLAG_UNLOCKED,
    OFF_SELL_FLAG_ON,
    READ_ONLY_ACTIONS,
    SPECIAL_KIND_AMOUNT,
    SPECIAL_KIND_TIME,
    WRITE_ACTIONS,
    DailiantongClient,
    DailiantongConfig,
    DailiantongError,
    DailiantongTransportError,
    build_sign,
    decode_escaped_unicode,
)

# ── 合成凭据（⛔ 全部不是真实值）────────────────────────────────────────────────
APP_ID = "app-id-canary"
APP_SECRET = "app-secret-canary-not-real"
USER_ID = "USR0000000000001"
#: 假想支付密码 `pw-canary-a` 的 MD5（第 1 层）。客户端只收这一层。
PAY_PASSWORD_MD5 = "0123456789abcdef0123456789abcdef"
#: `MD5(PAY_PASSWORD_MD5 + USER_ID)` 的**预先算好**的值（独立于实现，自己算的）。
PAY_PASS = "ce9c12f77177fa86ad63db415f5d68b3"
#: 合成订单号：20 位纯数字（与实测格式一致），⛔ 不是任何真实单号。
SERIAL_NO = "10000000000000000001"
#: 注入的固定时间戳（秒），让签名可复现。
TIMESTAMP = 1700000000


def _config(**overrides) -> DailiantongConfig:
    fields = {
        "app_id": APP_ID,
        "app_secret": APP_SECRET,
        "user_id": USER_ID,
        "pay_password_md5": PAY_PASSWORD_MD5,
    }
    fields.update(overrides)
    return DailiantongConfig(**fields)


def _json_response(payload, *, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )


def _form(request: httpx.Request) -> list[tuple[str, str]]:
    """按**发送顺序**解析表单（`parse_qsl` 保序；签名就依赖这个顺序）。"""
    return parse_qsl(request.content.decode("utf-8"), keep_blank_values=True)


def _form_dict(request: httpx.Request) -> dict[str, str]:
    return dict(_form(request))


def _expected_sign(action: str, business_values: list[str]) -> str:
    """按文档公式独立算一遍（值串 + appid + 时间戳 + ver + secret）。"""
    raw = "".join([action, *business_values, APP_ID, str(TIMESTAMP), "1.0", APP_SECRET])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class _Capture:
    """记下每个请求，并按调用序号返回可编排的响应。"""

    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request, len(self.requests))


class _ClientTestCase(SimpleTestCase):
    def make_client(self, handler) -> DailiantongClient:
        client = DailiantongClient(
            _config(),
            transport=httpx.MockTransport(handler),
            clock=lambda: TIMESTAMP,
        )
        self.addCleanup(client.close)
        return client

    def capture(self, responder) -> _Capture:
        return _Capture(responder)


# ── 1. 签名向量 ─────────────────────────────────────────────────────────────


class SigningVectorTests(SimpleTestCase):
    def test_fixed_input_has_fixed_md5(self):
        """固定输入 → 固定 md5：实现一旦漂移（顺序 / 拼接 / 大小写）这条就红。"""
        self.assertEqual(
            build_sign(
                ["gamezoneserver"],
                app_id="app-id-vector",
                app_secret="app-secret-vector",
                timestamp=1450774491,
            ),
            "6ce2ce07f4601e6b19118a9d9edae610",
        )

    def test_values_order_matters_and_names_are_not_signed(self):
        """⛔ 顺序敏感、⛔ 不含参数名：换序会换签名，值相同但顺序不同结果不同。"""
        first = build_sign(
            ["a", "b"],
            app_id="app-id-vector",
            app_secret="app-secret-vector",
            timestamp=1450774491,
        )
        reversed_order = build_sign(
            ["b", "a"],
            app_id="app-id-vector",
            app_secret="app-secret-vector",
            timestamp=1450774491,
        )
        self.assertEqual(first, "86182e2386b7488ee5340582df66862b")
        self.assertNotEqual(first, reversed_order)

    def test_pay_pass_is_second_md5(self):
        config = _config()
        self.assertEqual(config.pay_pass, PAY_PASS)
        self.assertNotEqual(config.pay_pass, PAY_PASSWORD_MD5)

    def test_pay_password_md5_must_not_be_plaintext(self):
        with self.assertRaises(ValueError) as caught:
            _config(pay_password_md5="pw-canary-a")
        # ⛔ 报错里只有字段名，不回显传进来的值（可能是明文密码）。
        self.assertNotIn("pw-canary-a", str(caught.exception))

    def test_pay_password_md5_must_be_lowercase_hex(self):
        with self.assertRaises(ValueError):
            _config(pay_password_md5=PAY_PASSWORD_MD5.upper())

    def test_empty_credentials_are_rejected_without_echoing_values(self):
        with self.assertRaises(ValueError) as caught:
            _config(app_secret="")
        self.assertIn("app_secret", str(caught.exception))

    def test_default_base_url_is_https(self):
        self.assertTrue(DEFAULT_BASE_URL.startswith("https://"))

    def test_base_url_must_be_https(self):
        """⛔ 明文链路里有 AppSecret 派生值与账号资料 ⇒ 构造期就拒掉。"""
        with self.assertRaises(ValueError) as caught:
            _config(base_url="http://dailiantong.test.invalid/API/OpenService.ashx")
        self.assertIn("https://", str(caught.exception))


# ── 2. 请求体形状 ───────────────────────────────────────────────────────────


class RequestShapeTests(_ClientTestCase):
    def test_post_form_and_public_parameters(self):
        capture = self.capture(
            lambda request, call: _json_response(
                {"SumBal": 0.0, "FreezeBal": 0.0, "ActiveBal": 0.0}
            )
        )
        self.make_client(capture).user_money()

        self.assertEqual(len(capture.requests), 1)
        request = capture.requests[0]
        self.assertEqual(request.method, "POST")
        # ⛔ 凭据只在 body：URL 上连查询串都不许有。
        self.assertEqual(str(request.url), DEFAULT_BASE_URL)
        self.assertEqual(request.url.query, b"")
        self.assertTrue(
            request.headers["Content-Type"].startswith(
                "application/x-www-form-urlencoded"
            )
        )

        form = _form(request)
        self.assertEqual(
            [name for name, _ in form], ["Action", "AppID", "TimeStamp", "Ver", "Sign"]
        )
        values = dict(form)
        self.assertEqual(values["Action"], ACTION_USER_MONEY)
        self.assertEqual(values["AppID"], APP_ID)
        self.assertEqual(values["TimeStamp"], str(TIMESTAMP))
        self.assertEqual(values["Ver"], "1.0")
        self.assertEqual(values["Sign"], _expected_sign(ACTION_USER_MONEY, []))

    def test_business_parameters_keep_order_and_are_signed(self):
        capture = self.capture(
            lambda request, call: _json_response({"Result": 1, "Err": "修改成功."})
        )
        self.make_client(capture).modify_price(SERIAL_NO, 3)

        request = capture.requests[0]
        form = _form(request)
        self.assertEqual(
            [name for name, _ in form],
            [
                "Action",
                "ODSerialNo",
                "Price",
                "PayPass",
                "AppID",
                "TimeStamp",
                "Ver",
                "Sign",
            ],
        )
        values = _form_dict(request)
        self.assertEqual(values["ODSerialNo"], SERIAL_NO)
        self.assertEqual(values["Price"], "3")
        self.assertEqual(values["PayPass"], PAY_PASS)
        self.assertEqual(
            values["Sign"],
            _expected_sign(
                ACTION_MODIFY_PRICE, [SERIAL_NO, "3", PAY_PASS]
            ),
        )

    def test_add_order_sends_documented_field_order_and_drops_none(self):
        capture = self.capture(
            lambda request, call: _json_response({"Result": 1, "Err": SERIAL_NO})
        )
        actors = "acct-placeholder|*|pass-placeholder|*|占位角色|*|其它:无|*|请勿接单"
        self.make_client(capture).add_order(
            zone_server_id="100100010000000",
            title="合成测试标题",
            time_limit=24,
            price=5,
            ensure1=0,
            ensure2=0,
            actors=actors,
            ex_order_no="TT-SYNTHETIC-1",
        )

        request = capture.requests[0]
        names = [name for name, _ in _form(request)]
        self.assertEqual(
            names,
            [
                "Action",
                "ZoneServerID",
                "Title",
                "TimeLimit",
                "Price",
                "Ensure1",
                "Ensure2",
                "Actors",
                "PayPass",
                "ExOrderNo",
                "AppID",
                "TimeStamp",
                "Ver",
                "Sign",
            ],
        )
        values = _form_dict(request)
        self.assertEqual(values["PayPass"], PAY_PASS)
        self.assertEqual(values["ExOrderNo"], "TT-SYNTHETIC-1")
        self.assertEqual(
            values["Sign"],
            _expected_sign(
                ACTION_ADD_ORDER,
                [
                    "100100010000000",
                    "合成测试标题",
                    "24",
                    "5",
                    "0",
                    "0",
                    actors,
                    PAY_PASS,
                    "TT-SYNTHETIC-1",
                ],
            ),
        )
        # ⛔ 密钥 / 支付密码 md5 不进 body（body 里只有第二层 PayPass）。
        body = request.content.decode("utf-8")
        self.assertNotIn(APP_SECRET, body)
        self.assertNotIn(PAY_PASSWORD_MD5, body)

    def test_pay_pass_is_only_sent_where_the_protocol_requires_it(self):
        """补款 / 申请撤销 / 同意撤销 / 完单 / 加价 / 改价带 PayPass；补时 / 取消撤销不带。"""
        cases = [
            ("special_amount", lambda client: client.modify_special(
                SERIAL_NO, kind=SPECIAL_KIND_AMOUNT, val=1
            ), True),
            ("special_time", lambda client: client.modify_special(
                SERIAL_NO, kind=SPECIAL_KIND_TIME, val=2
            ), False),
            ("special_game_password", lambda client: client.modify_special(
                SERIAL_NO, kind=2, val="新密码占位"
            ), False),
            ("cancel_apply", lambda client: client.cancel_order(
                SERIAL_NO, CANCEL_FLAG_APPLY
            ), True),
            ("cancel_withdraw", lambda client: client.cancel_order(
                SERIAL_NO, CANCEL_FLAG_CANCEL
            ), False),
            ("cancel_agree", lambda client: client.cancel_order(
                SERIAL_NO, CANCEL_FLAG_AGREE
            ), True),
            ("finish", lambda client: client.finish_order(SERIAL_NO), True),
            ("up_price", lambda client: client.up_price(SERIAL_NO, 1), True),
            ("modify_price", lambda client: client.modify_price(SERIAL_NO, 1), True),
        ]
        for label, call, expect_pay_pass in cases:
            with self.subTest(label):
                capture = self.capture(
                    lambda request, call_index: _json_response(
                        {"Result": 1, "Err": "成功."}
                    )
                )
                call(self.make_client(capture))
                values = _form_dict(capture.requests[0])
                if expect_pay_pass:
                    self.assertEqual(values["PayPass"], PAY_PASS)
                else:
                    self.assertNotIn("PayPass", values)

    def test_agree_cancel_carries_pay_pass_but_withdraw_does_not(self):
        """⭐ 同意撤销（`Flag=2`）也要 `PayPass`——文档只写了申请撤销要（2026-10-27 真机实测）。

        不带回 `{"Result":-2,"Err":"支付密码错误."}`；带上才 `{"Result":1,"Err":"撤销成功."}`
        并把 `CancelStatus` 从 11 推到 12（协商已处理）。`Flag=1`（取消撤销）不需要。
        """
        cases = [
            ("agree", CANCEL_FLAG_AGREE, True),
            ("withdraw", CANCEL_FLAG_CANCEL, False),
        ]
        for label, flag, expect_pay_pass in cases:
            with self.subTest(label):
                capture = self.capture(
                    lambda request, call_index: _json_response(
                        {"Result": 1, "Err": "撤销成功."}
                    )
                )
                self.make_client(capture).cancel_order(SERIAL_NO, flag)
                values = _form_dict(capture.requests[0])
                self.assertEqual(values["Flag"], str(flag))
                if expect_pay_pass:
                    self.assertEqual(values["PayPass"], PAY_PASS)
                else:
                    self.assertNotIn("PayPass", values)


# ── 3. 三种响应信封 ─────────────────────────────────────────────────────────


class ResponseEnvelopeTests(_ClientTestCase):
    def _client_with_payload(self, payload):
        capture = self.capture(lambda request, call: _json_response(payload))
        return self.make_client(capture), capture

    def test_result_envelope_numeric_success_returns_order_no(self):
        """实测发单：`Result` 是**数字** 1，订单号在 `Err` 里。"""
        client, _ = self._client_with_payload({"Result": 1, "Err": SERIAL_NO})
        self.assertEqual(
            client.add_order(
                zone_server_id="100100010000000",
                title="合成测试标题",
                time_limit=24,
                price=5,
                ensure1=0,
                ensure2=0,
                actors="acct|*|pass|*|角色|*|其它:无|*|请勿接单",
            ),
            SERIAL_NO,
        )

    def test_result_envelope_string_success_returns_upstream_message(self):
        """同一个字段在别的动作上是字符串 `"1"`，`Err` 是上游原话。"""
        client, _ = self._client_with_payload(
            {"Result": "1", "Err": "添加完成."}
        )
        self.assertEqual(client.message_add(SERIAL_NO, "合成留言"), "添加完成.")

    def test_result_float_one_is_accepted_as_success(self):
        """⭐ P3-15：上游可能回 `1.0`（旧实现 `str(1.0) != "1"` 会误判失败）。"""
        client = self.make_client(
            lambda request: _json_response({"Result": 1.0, "Err": "成功."})
        )

        self.assertEqual(client.up_price(SERIAL_NO, 5), "成功.")

    def test_malformed_surrogate_pair_does_not_emit_a_lone_surrogate(self):
        """⭐ P3-15：孤立代理对不能再 `chr()` 出孤立代理字符。"""
        decoded = decode_escaped_unicode("\\ud800x\\udc00")

        self.assertNotIn("\ud800", decoded)
        for char in decoded:
            self.assertFalse(0xD800 <= ord(char) <= 0xDFFF, repr(decoded))
        self.assertIn("\ufffd", decoded)

    def test_failure_envelope_numeric_code(self):
        payload = {"Result": -2, "Err": "可操作资金不足，不能发布."}
        client, _ = self._client_with_payload(payload)
        with self.assertRaises(DailiantongError) as caught:
            client.add_order(
                zone_server_id="100100010000000",
                title="合成测试标题",
                time_limit=24,
                price=5,
                ensure1=0,
                ensure2=0,
                actors="acct|*|pass|*|角色|*|其它:无|*|请勿接单",
            )
        exc = caught.exception
        self.assertEqual(exc.code, "-2")
        self.assertEqual(exc.message, "可操作资金不足，不能发布.")
        self.assertEqual(exc.action, ACTION_ADD_ORDER)
        self.assertEqual(exc.raw, payload)

    def test_failure_envelope_string_code_keeps_upstream_text(self):
        payload = {"Result": "-1", "Err": "没有找到该订单！"}
        client, _ = self._client_with_payload(payload)
        with self.assertRaises(DailiantongError) as caught:
            client.order_detail(SERIAL_NO)
        self.assertEqual(caught.exception.code, "-1")
        # ⛔ message 必须逐字等于上游 Err（不自造文案）。
        self.assertEqual(caught.exception.message, "没有找到该订单！")

    def test_bare_object_envelope(self):
        payload = {"SumBal": 10.0, "FreezeBal": 5.0, "ActiveBal": 5.0}
        client, _ = self._client_with_payload(payload)
        self.assertEqual(client.user_money(), payload)

    def test_bare_object_recommend_price(self):
        payload = {
            "BasePrice": 195.0,
            "Ensure1": 40.0,
            "Ensure2": 40.0,
            "TimeLimit": 99,
        }
        client, _ = self._client_with_payload(payload)
        self.assertEqual(
            client.recommend_price(
                title="合成标题", zone_server_id="100100010000000"
            ),
            payload,
        )

    def test_list_envelope_array_with_double_escaped_field(self):
        """列表形态：数组 + `RecordCount`；`Actor` 是字面量 `\\u5f85\\u5b9a`。"""
        payload = {
            "LevelOrderPubLishList": [
                {
                    "SerialNo": SERIAL_NO,
                    "Title": "合成标题",
                    "Actor": "\\u5f85\\u5b9a",
                    "Status": 11,
                }
            ],
            "RecordCount": 1,
        }
        client, _ = self._client_with_payload(payload)
        page = client.publish_list(s_date="2026-10-01 00:00:00", e_date="2026-10-31 23:59:59")
        self.assertEqual(page.record_count, 1)
        self.assertEqual(len(page.rows), 1)
        self.assertEqual(page.rows[0]["Title"], "合成标题")
        # ⭐ 兜底解码只动「像 \uXXXX」的值：Actor 还原，Title 原样。
        self.assertEqual(page.rows[0]["Actor"], "待定")
        self.assertEqual(page.rows[0]["SerialNo"], SERIAL_NO)

    def test_list_value_that_is_itself_a_json_string(self):
        """兜底分支：列表值不是数组而是「JSON 数组字符串」（§3 第 3 条末句）。"""
        inner = json.dumps(
            [{"SerialNo": SERIAL_NO, "Title": "合成标题"}], ensure_ascii=False
        )
        payload = {"LevelOrderPubLishList": inner, "RecordCount": 1}
        client, _ = self._client_with_payload(payload)
        page = client.publish_list(
            s_date="2026-10-01 00:00:00", e_date="2026-10-31 23:59:59"
        )
        self.assertEqual(page.record_count, 1)
        self.assertEqual(page.rows, ({"SerialNo": SERIAL_NO, "Title": "合成标题"},))

    def test_list_envelope_empty_result_is_empty_string(self):
        """⭐ 实测：空结果的值是**空字符串**，不是空数组。"""
        payload = {"LevelOrderPubLishList_Status": "", "RecordCount": 0}
        client, _ = self._client_with_payload(payload)
        page = client.order_status_list(
            s_date="2026-10-01 00:00:00", e_date="2026-10-31 23:59:59"
        )
        self.assertEqual(page.rows, ())
        self.assertEqual(page.record_count, 0)

    def test_list_envelope_without_record_count_reports_none(self):
        """实测有动作连 `RecordCount` 都不回 ⇒ `None`，⛔ 不用 0 冒充「查过且为空」。"""
        payload = {"LevelOrderMessageList": ""}
        client, _ = self._client_with_payload(payload)
        page = client.message_list(SERIAL_NO)
        self.assertEqual(page.rows, ())
        self.assertIsNone(page.record_count)

    def test_zones_uses_lowercase_envelope_key_and_has_no_count(self):
        payload = {
            "gamezoneserver": [
                {"ZoneServerID": "100100010000000", "ServerName": "英雄联盟/电信/艾欧尼亚"},
            ]
        }
        client, _ = self._client_with_payload(payload)
        page = client.zones()
        self.assertEqual(len(page.rows), 1)
        self.assertEqual(page.rows[0]["ZoneServerID"], "100100010000000")
        self.assertIsNone(page.record_count)

    def test_body_that_is_itself_json_encoded_once_more(self):
        """实测记录里出现过「整个回包再被 JSON 编码一层」的形态。"""
        payload = {"SumBal": 1.0, "FreezeBal": 0.0, "ActiveBal": 1.0}
        client = self.make_client(
            lambda request: httpx.Response(
                200, content=json.dumps(json.dumps(payload)).encode("utf-8")
            )
        )
        self.assertEqual(client.user_money(), payload)

    def test_unexpected_shape_is_loud(self):
        client, _ = self._client_with_payload({"SumBal": 1.0})
        with self.assertRaises(DailiantongTransportError):
            client.publish_list(
                s_date="2026-10-01 00:00:00", e_date="2026-10-31 23:59:59"
            )

    def test_non_json_body_is_loud(self):
        client = self.make_client(
            lambda request: httpx.Response(200, content=b"<html>WAF</html>")
        )
        with self.assertRaises(DailiantongTransportError):
            client.user_money()

    def test_unicode_escape_decoder_leaves_normal_text_alone(self):
        self.assertEqual(decode_escaped_unicode("待定"), "待定")
        self.assertEqual(decode_escaped_unicode("a\\nb"), "a\\nb")
        self.assertEqual(decode_escaped_unicode("\\u5f85\\u5b9a"), "待定")


class NewReadEndpointsTests(_ClientTestCase):
    """dlt-d 的两条新只读动作：按日统计（裸对象）/ 模板列表（列表信封）。"""

    def test_order_stat_returns_the_raw_object_and_sends_documented_params(self):
        capture = self.capture(
            lambda request, call_index: _json_response(
                {
                    "LevelOrderStatList": [{"QDate": "2026-10-26", "PubCount": "1"}],
                    "LevelOrderStatTotal": {"PubCount": "1"},
                }
            )
        )
        payload = self.make_client(capture).order_stat(
            is_pub=1, s_date="2026-10-20 00:00:00", e_date="2026-10-26 23:59:59"
        )
        self.assertEqual(payload["LevelOrderStatList"][0]["PubCount"], "1")
        form = _form_dict(capture.requests[0])
        self.assertEqual(form["Action"], ACTION_ORDER_STAT)
        self.assertEqual(form["IsPub"], "1")
        self.assertEqual(form["sDate"], "2026-10-20 00:00:00")
        self.assertEqual(form["eDate"], "2026-10-26 23:59:59")
        self.assertEqual(form["PageIndex"], "1")
        self.assertEqual(form["PageSize"], "50")

    def test_require_temp_list_sends_paging_and_parses_the_envelope(self):
        capture = self.capture(
            lambda request, call_index: _json_response(
                {
                    "OrderRequireTempList": [
                        {"ID": 7001, "Title": "模板甲", "Temp": "内容甲", "IsDefault": "0"}
                    ],
                    "RecordCount": 1,
                }
            )
        )
        page = self.make_client(capture).require_temp_list()
        self.assertEqual(page.record_count, 1)
        self.assertEqual(page.rows[0]["ID"], 7001)
        form = _form_dict(capture.requests[0])
        self.assertEqual(form["Action"], ACTION_REQUIRE_TEMP_LIST)
        self.assertEqual(form["PageIndex"], "1")
        self.assertEqual(form["PageSize"], "50")

    def test_require_temp_list_can_omit_the_inferred_paging_params(self):
        capture = self.capture(
            lambda request, call_index: _json_response(
                {"OrderRequireTempList": "", "RecordCount": 0}
            )
        )
        page = self.make_client(capture).require_temp_list(
            page_index=None, page_size=None
        )
        self.assertEqual(page.rows, ())
        form = _form_dict(capture.requests[0])
        self.assertNotIn("PageIndex", form)
        self.assertNotIn("PageSize", form)


# ── 4. 每个写端点的「失败信封必须判失败」（AGENTS §5-E）──────────────────────


def _write_cases() -> list[tuple[str, str, object]]:
    """(标签, 期望 Action, 收 client 的调用) —— 覆盖**全部 19 个写动作**。"""
    actors = "acct-placeholder|*|pass-placeholder|*|占位角色|*|其它:无|*|请勿接单"
    return [
        (
            "add_order",
            ACTION_ADD_ORDER,
            lambda client: client.add_order(
                zone_server_id="100100010000000",
                title="合成测试标题",
                time_limit=24,
                price=5,
                ensure1=0,
                ensure2=0,
                actors=actors,
            ),
        ),
        (
            "modify_order",
            ACTION_MODIFY_ORDER,
            lambda client: client.modify_order(
                SERIAL_NO,
                title="合成测试标题",
                time_limit=24,
                price=6,
                ensure1=0,
                ensure2=0,
            ),
        ),
        (
            "modify_special",
            ACTION_MODIFY_SPECIAL,
            lambda client: client.modify_special(
                SERIAL_NO, kind=SPECIAL_KIND_AMOUNT, val=1
            ),
        ),
        ("delete_order", ACTION_DELETE_ORDER, lambda client: client.delete_order(SERIAL_NO)),
        (
            "cancel_order",
            ACTION_CANCEL_ORDER,
            lambda client: client.cancel_order(SERIAL_NO, CANCEL_FLAG_APPLY),
        ),
        (
            "finish_order",
            ACTION_FINISH_ORDER,
            lambda client: client.finish_order(SERIAL_NO),
        ),
        ("up_price", ACTION_UP_PRICE, lambda client: client.up_price(SERIAL_NO, 1)),
        (
            "modify_price",
            ACTION_MODIFY_PRICE,
            lambda client: client.modify_price(SERIAL_NO, 1),
        ),
        (
            "message_add",
            ACTION_MESSAGE_ADD,
            lambda client: client.message_add(SERIAL_NO, "合成留言"),
        ),
        (
            "progress_add",
            ACTION_PROGRESS_ADD,
            lambda client: client.progress_add(SERIAL_NO, "合成进度"),
        ),
        # ⭐ dlt-d：这批「有、我们没用上」的动作**全是写动作** ⇒ 一并进红线表。
        (
            "value_order",
            ACTION_ORDER_VALUE,
            lambda client: client.value_order(
                SERIAL_NO,
                other_user_id="USR-SYNTH-TAKER",
                eval_type=EVAL_TYPE_VERY_SATISFIED,
            ),
        ),
        (
            "lock_order",
            ACTION_ORDER_LOCK,
            lambda client: client.lock_order(SERIAL_NO, locked=1),
        ),
        (
            "request_arbitration",
            ACTION_REQUEST_ARBITRATION,
            lambda client: client.request_arbitration(SERIAL_NO),
        ),
        (
            "off_sell",
            ACTION_OFF_SELL,
            lambda client: client.off_sell(SERIAL_NO, flag=OFF_SELL_FLAG_ON),
        ),
        (
            "add_blacklist",
            ACTION_ADD_BLACKLIST,
            lambda client: client.add_blacklist(
                "USR-SYNTH-TAKER", flag=BLACKLIST_FLAG_REMOVE
            ),
        ),
        (
            "add_require_temp",
            ACTION_REQUIRE_TEMP_ADD,
            lambda client: client.add_require_temp(
                title="合成模板", temp="合成内容", is_default=0
            ),
        ),
        (
            "update_require_temp",
            ACTION_REQUIRE_TEMP_UPDATE,
            lambda client: client.update_require_temp(7001, title="合成模板"),
        ),
        (
            "delete_require_temp",
            ACTION_REQUIRE_TEMP_DELETE,
            lambda client: client.delete_require_temp(7001),
        ),
        (
            "set_default_require_temp",
            ACTION_REQUIRE_TEMP_SET_DEFAULT,
            lambda client: client.set_default_require_temp(7001),
        ),
    ]


class WriteEndpointTests(_ClientTestCase):
    def test_every_write_endpoint_rejects_failure_envelope(self):
        for label, action, call in _write_cases():
            with self.subTest(label):
                capture = self.capture(
                    lambda request, call_index: _json_response(
                        {"Result": -1, "Err": "合成失败原因."}
                    )
                )
                with self.assertRaises(DailiantongError) as caught:
                    call(self.make_client(capture))
                self.assertEqual(caught.exception.action, action)
                self.assertEqual(caught.exception.code, "-1")
                self.assertEqual(caught.exception.message, "合成失败原因.")
                self.assertEqual(_form_dict(capture.requests[0])["Action"], action)

    def test_every_write_endpoint_accepts_success_envelope(self):
        for label, action, call in _write_cases():
            with self.subTest(label):
                capture = self.capture(
                    lambda request, call_index: _json_response(
                        {"Result": 1, "Err": "成功."}
                    )
                )
                self.assertEqual(call(self.make_client(capture)), "成功.")
                self.assertEqual(_form_dict(capture.requests[0])["Action"], action)


# ── 5. 重试红线 ─────────────────────────────────────────────────────────────


class RetryPolicyTests(_ClientTestCase):
    def test_whitelist_is_explicit_and_disjoint_from_writes(self):
        self.assertTrue(READ_ONLY_ACTIONS.isdisjoint(WRITE_ACTIONS))
        for action in (
            ACTION_ORDER_DETAIL,
            ACTION_ORDER_STATUS_LIST,
            ACTION_PUBLISH_LIST,
            ACTION_MESSAGE_LIST,
            ACTION_PROGRESS_LIST,
            ACTION_USER_MONEY,
            ACTION_MONEY_CHANGE_LIST,
            ACTION_ZONES,
            ACTION_RECOMMEND_PRICE,
            ACTION_GET_STS,
            ACTION_ORDER_STAT,
            ACTION_REQUIRE_TEMP_LIST,
        ):
            self.assertIn(action, READ_ONLY_ACTIONS)
        for action in (
            ACTION_ADD_ORDER,
            ACTION_MODIFY_ORDER,
            ACTION_MODIFY_SPECIAL,
            ACTION_DELETE_ORDER,
            ACTION_CANCEL_ORDER,
            ACTION_FINISH_ORDER,
            ACTION_UP_PRICE,
            ACTION_MODIFY_PRICE,
            ACTION_MESSAGE_ADD,
            ACTION_PROGRESS_ADD,
            ACTION_ORDER_VALUE,
            ACTION_ORDER_LOCK,
            ACTION_REQUEST_ARBITRATION,
            ACTION_OFF_SELL,
            ACTION_ADD_BLACKLIST,
            ACTION_REQUIRE_TEMP_ADD,
            ACTION_REQUIRE_TEMP_UPDATE,
            ACTION_REQUIRE_TEMP_DELETE,
            ACTION_REQUIRE_TEMP_SET_DEFAULT,
        ):
            self.assertIn(action, WRITE_ACTIONS)
            self.assertNotIn(action, READ_ONLY_ACTIONS)

    def test_add_order_timeout_is_never_retried(self):
        """⛔ 发单超时只调用一次：重试 = 真钱重复建单。"""
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ReadTimeout("timed out")

        client = self.make_client(handler)
        with self.assertRaises(DailiantongTransportError):
            client.add_order(
                zone_server_id="100100010000000",
                title="合成测试标题",
                time_limit=24,
                price=5,
                ensure1=0,
                ensure2=0,
                actors="acct|*|pass|*|角色|*|其它:无|*|请勿接单",
            )
        self.assertEqual(len(calls), 1)

    def test_every_write_action_is_never_retried(self):
        for label, action, call in _write_cases():
            with self.subTest(label):
                calls: list[httpx.Request] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    calls.append(request)
                    raise httpx.ConnectError("connection refused")

                with self.assertRaises(DailiantongTransportError) as caught:
                    call(self.make_client(handler))
                self.assertEqual(len(calls), 1)
                self.assertEqual(caught.exception.action, action)
                self.assertIsNone(caught.exception.code)

    def test_read_only_timeout_retries_exactly_once(self):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ReadTimeout("timed out")
            return _json_response(
                {"SumBal": 1.0, "FreezeBal": 0.0, "ActiveBal": 1.0}
            )

        result = self.make_client(handler).user_money()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["ActiveBal"], 1.0)

    def test_read_only_gives_up_after_one_retry(self):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ConnectTimeout("timed out")

        with self.assertRaises(DailiantongTransportError):
            self.make_client(handler).order_detail(SERIAL_NO)
        self.assertEqual(len(calls), 2)

    def test_get_sts_is_retried_once_on_timeout(self):
        """⭐ P3-15：`GetSTS` 是只读动作 ⇒ 允许重试一次（旧白名单漏了它）。"""
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ReadTimeout("timed out")
            return _json_response(
                {
                    "SecurityToken": "synth-token",
                    "AccessKeyId": "synth-ak",
                    "AccessKeySecret": "synth-secret",
                }
            )

        credentials = self.make_client(handler).get_sts()
        self.assertEqual(len(calls), 2)
        self.assertEqual(credentials.security_token, "synth-token")

    def test_duplicate_parameter_names_are_rejected_before_any_request(self):
        """⭐ P3-15：重复参数名会被 `dict(form)` 吞掉一个 ⇒ 发送前必须拒绝。"""
        capture = self.capture(lambda request, _n: _json_response({"Status": 12}))
        client = self.make_client(capture)

        with self.assertRaises(ValueError) as ctx:
            client._request(
                "LevelOrderDetail",
                [("ODSerialNo", "a"), ("ODSerialNo", "b")],
            )

        self.assertIn("重复", str(ctx.exception))
        self.assertEqual(capture.requests, [])

    def test_http_5xx_is_not_retried_even_for_read_only(self):
        """重试只覆盖「连接失败 / 超时」，⛔ 不覆盖服务端明确回的非业务答复。"""
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(502, content=b"<html>bad gateway</html>")

        with self.assertRaises(DailiantongTransportError):
            self.make_client(handler).user_money()
        self.assertEqual(len(calls), 1)


# ── 6. 凭据不进 repr / 异常 ─────────────────────────────────────────────────


class CredentialHygieneTests(_ClientTestCase):
    def test_config_and_client_repr_hide_secrets(self):
        config = _config()
        client = self.make_client(lambda request: None)
        for text in (repr(config), str(config), repr(client)):
            self.assertNotIn(APP_SECRET, text)
            self.assertNotIn(PAY_PASSWORD_MD5, text)
            # 派生值也不能出现：只挡住上一层 md5 不算数。
            self.assertNotIn(PAY_PASS, text)
            # 打码标记在，证明我们不是「碰巧没拼进去」。
            self.assertIn("***", text)

    def test_business_error_does_not_leak_credentials(self):
        client = self.make_client(
            lambda request: _json_response({"Result": -1, "Err": "合成失败原因."})
        )
        with self.assertRaises(DailiantongError) as caught:
            client.finish_order(SERIAL_NO)
        for text in (str(caught.exception), repr(caught.exception)):
            self.assertNotIn(APP_SECRET, text)
            self.assertNotIn(PAY_PASSWORD_MD5, text)
            self.assertNotIn(PAY_PASS, text)

    def test_retry_warning_does_not_leak_credentials(self):
        """重试日志只记动作名 + 异常类型，⛔ 不记表单（里面有 PayPass）。"""
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ReadTimeout("timed out")
            return _json_response({"SumBal": 1.0, "FreezeBal": 0.0, "ActiveBal": 1.0})

        with self.assertLogs(
            "apps.business.dailiantong_client", level="WARNING"
        ) as captured:
            self.make_client(handler).user_money()
        text = "\n".join(captured.output)
        self.assertNotIn(APP_SECRET, text)
        self.assertNotIn(PAY_PASSWORD_MD5, text)
        self.assertNotIn(PAY_PASS, text)

    def test_transport_error_does_not_leak_credentials(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = self.make_client(handler)
        with self.assertRaises(DailiantongTransportError) as caught:
            client.finish_order(SERIAL_NO)
        for text in (str(caught.exception), repr(caught.exception)):
            self.assertNotIn(APP_SECRET, text)
            self.assertNotIn(PAY_PASSWORD_MD5, text)
            self.assertNotIn(PAY_PASS, text)
