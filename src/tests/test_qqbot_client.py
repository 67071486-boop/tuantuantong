"""`qqbot_api` 客户端判据（批次A）。

⭐ 核心：**E-0087** —— QQ 的业务错误走响应体 `code`、HTTP 仍是 200 ⇒
「HTTP 200 + code≠0」**必须判失败**；「没抛异常」永远不构成成功判据。
另一条钉死的是**脱敏**：secret / token 不出现在任何日志与异常文案里
（`assertLogs` 捕获后逐字断言）。

HTTP 层用 `httpx.MockTransport`（真 httpx 栈、假传输），不 mock 客户端本身。
"""
import json

import httpx
from django.test import TestCase

from apps.qqbot import qqbot_api

SECRET = "SUPER-secret-app-01"
TOKEN = "QQ-token-abcdef-01"


def _tm(**kw) -> qqbot_api.TokenManager:
    defaults = dict(app_id="APPID-1", client_secret=SECRET)
    defaults.update(kw)
    return qqbot_api.TokenManager(**defaults)


def _json_transport(responses, calls=None) -> httpx.MockTransport:
    """按序回放的传输层：`responses` 是 `(status, body_dict | Exception)` 列表。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        status, body = responses.pop(0)
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


class TokenManagerTests(TestCase):
    """token 换取 / 缓存 / 续期窗口 / 重试一次。"""

    def test_fetches_and_caches_until_near_expiry(self):
        calls = []
        clock = [1000.0]
        tm = _tm(
            transport=_json_transport(
                [(200, {"access_token": TOKEN, "expires_in": 7200})], calls
            ),
            clock=lambda: clock[0],
        )
        self.assertEqual(tm.get_token(), TOKEN)
        # 未到续期窗口 ⇒ 直接用缓存，不再发请求。
        clock[0] += 6000  # 距过期还有 1200s > 60s 余量
        self.assertEqual(tm.get_token(), TOKEN)
        self.assertEqual(len(calls), 1)
        # 进入「过期前 60s」窗口（余量 60s）⇒ 换新。
        clock[0] = 1000.0 + 7200 - 30  # 距过期 30s
        calls2 = []
        tm._transport = _json_transport(
            [(200, {"access_token": "QQ-token-2", "expires_in": 7200})], calls2
        )
        self.assertEqual(tm.get_token(), "QQ-token-2")
        self.assertEqual(len(calls2), 1)

    def test_failure_retries_once_then_raises(self):
        calls = []
        tm = _tm(
            transport=_json_transport(
                [(500, {"message": "boom"}), (500, {"message": "boom"})], calls
            )
        )
        with self.assertRaises(qqbot_api.TokenError):
            tm.get_token()
        self.assertEqual(len(calls), 2)  # 失败重试一次，共两次

    def test_token_error_message_never_contains_secret(self):
        tm = _tm(
            transport=_json_transport(
                [
                    (401, {"message": f"bad credentials {SECRET}"}),
                    (401, {"message": f"bad credentials {SECRET}"}),
                ]
            )
        )
        with self.assertRaises(qqbot_api.TokenError) as ctx:
            tm.get_token()
        self.assertNotIn(SECRET, str(ctx.exception))
        self.assertIn(qqbot_api.REDACTED, str(ctx.exception))

    def test_token_endpoint_request_shape(self):
        calls = []
        tm = _tm(
            transport=_json_transport(
                [(200, {"access_token": TOKEN, "expires_in": 7200})], calls
            )
        )
        tm.get_token()
        request = calls[0]
        self.assertEqual(request.url.path, "/app/getAppAccessToken")
        self.assertEqual(
            json.loads(request.content),
            {"appId": "APPID-1", "clientSecret": SECRET},
        )
        # 该接口本身不需要 Authorization。
        self.assertNotIn("Authorization", request.headers)


class AuthorizationHeaderTests(TestCase):
    """业务 API 的 header 形状：`Authorization: QQBot <access_token>`。"""

    def test_business_call_carries_qqbot_scheme_header(self):
        calls = []
        tm = _tm(
            transport=_json_transport(
                [
                    (200, {"access_token": TOKEN, "expires_in": 7200}),
                    (200, {"url": "wss://gate.example/"}),
                ],
                calls,
            )
        )
        ok, data, code, _message = qqbot_api.fetch_gateway(tm)
        self.assertTrue(ok, code)
        self.assertEqual(data["url"], "wss://gate.example/")
        self.assertEqual(calls[1].url.path, "/gateway")
        self.assertEqual(calls[1].headers["Authorization"], f"QQBot {TOKEN}")


class SuccessCriteriaTests(TestCase):
    """⭐ E-0087：成败只认 code / 关键字段，绝不认「HTTP 200」或「没抛异常」。"""

    def test_http200_with_nonzero_code_must_be_failure(self):
        """⭐ 本条的判别力：QQ 业务错误（HTTP 仍 200）——拿「没抛异常」判成功就是 E-0087 复刻。"""
        tm = _tm(
            transport=_json_transport(
                [
                    (200, {"access_token": TOKEN, "expires_in": 7200}),
                    (200, {"code": 40034100, "message": "频率超限"}),
                ]
            )
        )
        ok, data, code, message = qqbot_api.send_group_message(
            tm, "GROUP-1", msg_type=0, content="hi"
        )
        self.assertFalse(ok)
        self.assertEqual(code, "40034100")
        self.assertEqual(message, "频率超限")

    def test_http200_with_code_zero_is_success(self):
        tm = _tm(
            transport=_json_transport(
                [
                    (200, {"access_token": TOKEN, "expires_in": 7200}),
                    (200, {"code": 0, "data": {"id": "MSG-1"}}),
                ]
            )
        )
        ok, data, code, _message = qqbot_api.send_group_message(
            tm, "GROUP-1", msg_type=0, content="hi"
        )
        self.assertTrue(ok, code)
        self.assertEqual(data["id"], "MSG-1")

    def test_send_without_code_needs_the_id_key(self):
        # 发群消息成功形状是 `{"id", "timestamp"}`（无统一 code）⇒ 有 id 才算成功。
        tm = _tm(
            transport=_json_transport(
                [
                    (200, {"access_token": TOKEN, "expires_in": 7200}),
                    (200, {"id": "MSG-9", "timestamp": "2026-10-19T10:00:00+08:00"}),
                ]
            )
        )
        ok, data, _code, _message = qqbot_api.send_group_message(
            tm, "GROUP-1", msg_type=0, content="hi"
        )
        self.assertTrue(ok)
        self.assertEqual(data["id"], "MSG-9")

        tm2 = _tm(
            transport=_json_transport(
                [
                    (200, {"access_token": TOKEN, "expires_in": 7200}),
                    (200, {"timestamp": "2026-10-19T10:00:00+08:00"}),  # 缺 id
                ]
            )
        )
        ok, _data, code, _message = qqbot_api.send_group_message(
            tm2, "GROUP-1", msg_type=0, content="hi"
        )
        self.assertFalse(ok)
        self.assertEqual(code, "MISSING_KEY")

    def test_non_2xx_is_failure_with_http_code(self):
        tm = _tm(
            transport=_json_transport(
                [
                    (200, {"access_token": TOKEN, "expires_in": 7200}),
                    (503, {"message": "overloaded"}),
                ]
            )
        )
        ok, _data, code, _message = qqbot_api.fetch_gateway(tm)
        self.assertFalse(ok)
        self.assertEqual(code, "HTTP503")

    def test_network_error_maps_to_retryable_network_code(self):
        tm = _tm(
            transport=_json_transport(
                [
                    (200, {"access_token": TOKEN, "expires_in": 7200}),
                    (200, httpx.ConnectError("refused")),
                ]
            )
        )
        ok, _data, code, message = qqbot_api.fetch_gateway(tm)
        self.assertFalse(ok)
        self.assertEqual(code, "NETWORK")
        self.assertEqual(message, "ConnectError")

    def test_send_group_message_body_shape(self):
        calls = []
        tm = _tm(
            transport=_json_transport(
                [
                    (200, {"access_token": TOKEN, "expires_in": 7200}),
                    (200, {"id": "MSG-2"}),
                ],
                calls,
            )
        )
        qqbot_api.send_group_message(
            tm,
            "GROUP-7",
            msg_type=2,
            markdown="# 标题",
            msg_id="IN-1",
            msg_seq=3,
        )
        body = json.loads(calls[1].content)
        self.assertEqual(calls[1].url.path, "/v2/groups/GROUP-7/messages")
        self.assertEqual(
            body,
            {
                "msg_type": 2,
                "markdown": {"content": "# 标题"},
                "msg_id": "IN-1",
                "msg_seq": 3,
            },
        )


class RedactTests(TestCase):
    """脱敏：secret / token 永不进日志（assertLogs 捕获后逐字断言）。"""

    def test_redact_replaces_by_value_equality(self):
        text = f"凭据 {SECRET} 不对；token={TOKEN} 失效"
        redacted = qqbot_api._redact(text, qqbot_api._usable((SECRET, TOKEN)))
        self.assertNotIn(SECRET, redacted)
        self.assertNotIn(TOKEN, redacted)
        self.assertIn(qqbot_api.REDACTED, redacted)

    def test_redact_ignores_too_short_secrets(self):
        # ≤5 字的「密钥」做替换只会误伤正常文案（_scrub 同款口径）⇒ 不处理。
        self.assertEqual(qqbot_api._redact("abc 出现在文案里", ("abc",)), "abc 出现在文案里")

    def test_failed_call_log_does_not_leak_secret_or_token(self):
        tm = _tm(
            transport=_json_transport(
                [
                    (200, {"access_token": TOKEN, "expires_in": 7200}),
                    (
                        200,
                        {
                            "code": 50055001,
                            "message": f"echo {SECRET} {TOKEN}",
                        },
                    ),
                ]
            )
        )
        with self.assertLogs("apps.qqbot.qqbot_api", level="WARNING") as captured:
            ok, _data, _code, _message = qqbot_api.send_group_message(
                tm, "GROUP-1", msg_type=0, content="hi"
            )
        self.assertFalse(ok)
        joined = "\n".join(captured.output)
        self.assertNotIn(SECRET, joined)
        self.assertNotIn(TOKEN, joined)
        self.assertIn("50055001", joined)
