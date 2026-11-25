"""鸡蛋铺 SDK 传输层的**重试幂等标记**（审计批 C8 / Z21，2026-10-25）。

## 这张网守什么

旧行为对所有 `httpx.HTTPError` **无差别重试** ⇒ 非幂等写（下单 / 改价 / 改状态）在
「请求已到对方、响应没回来」（读超时 / 读中断）时被重发——同一张单可能下两次。
分流判据（不能按 HTTP 方法判：鸡蛋铺**读**接口走 POST）：

- **连接期错误**（`ConnectError` / `ConnectTimeout` / `PoolTimeout`）：请求确定没出门
  ⇒ **任何端点都重试**（`retry_on_sent=False` 也拦不住）；
- **已发出错误**（读 / 写超时、读 / 写中断、协议断裂）：只看**调用点的幂等标记**
  `retry_on_sent`——读端点 `True`（保持既有可重试）、写端点 `False`（保守，⛔ 不赌）；
- 解析口径在 `client.call()`：显式参数 > 清单 `risk`（`write` ⇒ `False`，其余与未登记
  ⇒ `True`）。

⚠️ 全部走 `httpx.MockTransport`（只换 socket 那一层，零真实网络）；退避睡眠一律打桩。
"""
from __future__ import annotations

import httpx
from django.test import SimpleTestCase

from apps.business.jidanpu import ASSIGN_PATH, assign_order
from jidanpu.client import JidanpuClient
from jidanpu.config import JidanpuConfig
from jidanpu.errors import AuthError, JidanpuTimeoutError, RateLimitError, SessionExpiredError, TransportError
from jidanpu.transport import Transport

BASE = "https://jdp-synth-c8.test.invalid"

#: 清单里**登记过**的一个写端点与一个**POST 形状的读端点**（实查 `spec/endpoints.json`）。
WRITE_PATH = "/CommonPermissionAjax/DelSingleImg"
READ_POST_PATH = "/EmpAjax/GetOrderConseTypeByCondition"


def _attempts_recorder(exc: Exception, *, succeed_on: int | None = None,
                       payload: dict | None = None):
    """MockTransport handler：每次请求记一笔、抛 `exc`；`succeed_on` 次起改回正常壳。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if succeed_on is not None and len(calls) >= succeed_on:
            return httpx.Response(200, json=payload or {"code": 0, "msg": "", "data": None})
        raise exc

    return calls, handler


def _transport(handler: httpx.Response | object, *, max_retries: int = 2) -> Transport:
    config = JidanpuConfig(
        base_url=BASE, account="synth-c8-a", password="pw-synth-c8",
        max_retries=max_retries,
    )
    return Transport(
        config,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


class _StubAuth:
    """最小假认证：`ensure_login` 只拿标记，不发请求。"""

    def get_token(self) -> str:
        return "synthetic-session-marker-c8"

    def invalidate(self) -> None:
        return None


def _client(handler) -> JidanpuClient:
    config = JidanpuConfig(
        base_url=BASE, account="synth-c8-a", password="pw-synth-c8",
        max_retries=2,
    )
    return JidanpuClient(
        config,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        auth=_StubAuth(),
    )


def mock_sleep():
    """把传输层的退避睡眠打桩（零等待，用例只数尝试次数）。"""
    import unittest.mock as _mock

    return _mock.patch("jidanpu.transport.time.sleep")


class TransportSentErrorRetryTests(SimpleTestCase):
    """传输层本体：已发出型错误的开关 + 连接期错误的豁免。"""

    def test_a_write_call_does_not_resend_after_a_read_timeout(self):
        """写端点（`retry_on_sent=False`）遇读超时 ⇒ **只发一次**，绝不重发。"""
        calls, handler = _attempts_recorder(httpx.ReadTimeout("合成：响应没回来"))
        with mock_sleep():
            with self.assertRaises(JidanpuTimeoutError):
                _transport(handler).request(
                    "POST", "/OrderAjax/CreateOrder", retry_on_sent=False,
                )
        self.assertEqual(calls, ["/OrderAjax/CreateOrder"])

    def test_a_write_call_does_not_resend_after_a_read_error_either(self):
        """读中断同样算「可能已发出」⇒ 写端点一次就停。"""
        calls, handler = _attempts_recorder(httpx.ReadError("合成：连接被对端掐断"))
        with mock_sleep():
            with self.assertRaises(TransportError):
                _transport(handler).request(
                    "POST", "/OrderAjax/CreateOrder", retry_on_sent=False,
                )
        self.assertEqual(len(calls), 1)

    def test_a_write_call_does_not_resend_after_a_decoding_error(self):
        """⭐ L-2（2026-10-26）：`DecodingError` 只在**读响应体**时抛 ⇒ 请求已完整送达。

        写端点（`retry_on_sent=False`）必须**只发一次**（旧集合漏了它 ⇒ 会被当成「没出门」
        重发一次下单 / 改价）。这里让对端回一个标了 `Content-Encoding: gzip` 的坏体，
        `httpx` 在 `client.request` 里读体解压时抛 `DecodingError`。
        """
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, content=b"not-a-gzip-body"
            )

        with mock_sleep():
            with self.assertRaises(TransportError):
                _transport(handler).request(
                    "POST", "/OrderAjax/CreateOrder", retry_on_sent=False,
                )
        self.assertEqual(calls, ["/OrderAjax/CreateOrder"], "解码失败被当成没出门、重发了")

    def test_a_read_call_keeps_the_retry_ladder_on_sent_errors(self):
        """读端点（默认 `True`）遇读超时 ⇒ 照旧 1 + max_retries 次尝试（既有行为）。"""
        calls, handler = _attempts_recorder(httpx.ReadTimeout("合成：响应没回来"))
        with mock_sleep():
            with self.assertRaises(JidanpuTimeoutError):
                _transport(handler).request("POST", "/OrderAjax/GetOrderList")
        self.assertEqual(calls, ["/OrderAjax/GetOrderList"] * 3)

    def test_connection_errors_are_retried_even_for_writes(self):
        """连接期错误**不受开关管**：请求没出门，写端点也照旧重试（全部尝试用满）。"""
        calls, handler = _attempts_recorder(httpx.ConnectError("合成：连不上"))
        with mock_sleep():
            with self.assertRaises(TransportError):
                _transport(handler).request(
                    "POST", "/OrderAjax/CreateOrder", retry_on_sent=False,
                )
        self.assertEqual(calls, ["/OrderAjax/CreateOrder"] * 3)

    def test_connect_timeout_is_also_a_never_sent_error(self):
        """连接超时同样是「没出门」⇒ `retry_on_sent=False` 下也重试。"""
        calls, handler = _attempts_recorder(httpx.ConnectTimeout("合成：握手超时"))
        with mock_sleep():
            with self.assertRaises(JidanpuTimeoutError):
                _transport(handler).request(
                    "POST", "/OrderAjax/CreateOrder", retry_on_sent=False,
                )
        self.assertEqual(calls, ["/OrderAjax/CreateOrder"] * 3)

    def test_a_read_call_recovers_when_a_later_attempt_succeeds(self):
        """读端点 + 已发出错误：第一次超时、第二次成功 ⇒ 照常拿到结果（重试不是白重）。"""
        calls, handler = _attempts_recorder(
            httpx.ReadTimeout("合成：第一次没回来"),
            succeed_on=2, payload={"code": 0, "msg": "", "data": []},
        )
        with mock_sleep():
            got = _transport(handler).request("POST", "/OrderAjax/GetOrderList")
        self.assertEqual(len(calls), 2)
        self.assertEqual(got, [])


class ClientRetryResolutionTests(SimpleTestCase):
    """`client.call()` 的解析口径：显式参数 > 清单 `risk`；⛔ 不看 HTTP 方法。"""

    def test_a_registered_write_endpoint_defaults_to_no_resend(self):
        """清单里 `risk="write"` 的端点：POST 读不读得了无所谓——**不重发**。"""
        calls, handler = _attempts_recorder(httpx.ReadTimeout("合成：响应没回来"))
        with mock_sleep():
            with self.assertRaises(JidanpuTimeoutError):
                _client(handler).call("POST", WRITE_PATH)
        self.assertEqual(len(calls), 1)

    def test_a_registered_post_read_endpoint_still_retries(self):
        """**POST 形状的读端点**照旧重试（鸡蛋铺读接口走 POST，方法判不出来——这就是本条的判别力）。"""
        calls, handler = _attempts_recorder(httpx.ReadTimeout("合成：响应没回来"))
        with mock_sleep():
            with self.assertRaises(JidanpuTimeoutError):
                _client(handler).call("POST", READ_POST_PATH)
        self.assertEqual(calls, [READ_POST_PATH] * 3)

    def test_an_exp_true_overrides_the_write_default(self):
        """显式 `retry_on_sent=True` 压过写端点默认（调用方最懂自己的端点）。"""
        calls, handler = _attempts_recorder(httpx.ReadTimeout("合成：响应没回来"))
        with mock_sleep():
            with self.assertRaises(JidanpuTimeoutError):
                _client(handler).call("POST", WRITE_PATH, retry_on_sent=True)
        self.assertEqual(len(calls), 3)

    def test_an_unregistered_endpoint_keeps_the_retryable_default(self):
        """未登记端点保持既有可重试行为（ViewPic 这类不在清单里的读口不回退）。"""
        calls, handler = _attempts_recorder(httpx.ReadTimeout("合成：响应没回来"))
        with mock_sleep():
            with self.assertRaises(JidanpuTimeoutError):
                _client(handler).call("POST", "/OrderAjax/ViewPic")
        self.assertEqual(calls, ["/OrderAjax/ViewPic"] * 3)


class DirectAssignmentReplayTests(SimpleTestCase):
    def test_direct_assignment_does_not_repeat_a_sent_request(self):
        for status, response_body, error, known_reason in (
            (429, {"code": 0, "msg": "请求过于频繁"}, RateLimitError, "请求过于频繁"),
            (401, {"code": 0, "msg": "登录凭证已失效"}, AuthError, "登录凭证已失效"),
            (403, {"code": 0, "msg": "没有指派权限"}, AuthError, "没有指派权限"),
            (302, None, SessionExpiredError, None),
            (200, {"code": 0, "msg": "请先登录"}, SessionExpiredError, "请先登录"),
            (200, {"code": 1, "msg": "请先登录"}, SessionExpiredError, None),
        ):
            with self.subTest(status=status):
                paths = []

                def respond(request):
                    paths.append(request.url.path)
                    if len(paths) == 1:
                        return httpx.Response(status, json=response_body)
                    return httpx.Response(200, json={"code": 1, "msg": "指派成功"})

                with mock_sleep(), self.assertRaises(error) as raised:
                    assign_order(_client(respond), remote_order_id="order-synth", user_id="401", user_name="合成打手", fee=0)

                self.assertEqual(paths, [ASSIGN_PATH])
                self.assertEqual(raised.exception.upstream_message, known_reason)

    def test_ordinary_reads_still_retry_after_rate_limiting(self):
        paths = []

        def respond(request):
            paths.append(request.url.path)
            if len(paths) == 1:
                return httpx.Response(429, json={"msg": "请求过于频繁"})
            return httpx.Response(200, json={"code": 0, "data": []})

        with mock_sleep():
            self.assertEqual(_client(respond).call("POST", READ_POST_PATH), [])
        self.assertEqual(paths, [READ_POST_PATH, READ_POST_PATH])
