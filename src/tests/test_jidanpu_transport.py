"""`jidanpu.transport.unwrap_response` 对「对端 WAF/风控拦截」的分类（egress-pool Task 1）。

## 为什么要有这个分类

2026-10-25 的实测读数（`.tools/recon/rate-h2-*` / `rate-curl-*`，不进 git）：鸡蛋铺对端的
WAF 拒绝形态是 **HTTP 468 + SafeLine 拦截页**，**按来源 IP 全站封禁 5–15 分钟**。这类失败

- **不是**我们的配额问题（那是 429 / `RateLimitError`）；
- **也**不是凭据问题（那是 401/403 / `AuthError`，重登也救不回来）。

它要求上层做的是**换出口**，所以必须在 SDK 里单独分类。混进 `ApiError` 的话，调度层
只会把它当普通 4xx 重试 —— 那正是「被封了还按原节奏白撞」的形态。

## 本文件的判据边界

- ⚠️ **零真实网络**：响应对象全部就地构造（`httpx.Response`），不发请求。
- ⚠️ **文案不含正文**：拦截页里可能带 HTML/JS、也可能回显请求内容，`str(exc)` 不许出现正文。
- ⚠️ **回归钉子**：401 / 429 / 500 的分类**一个字都不许变**（`AGENTS.md` §5-E）；
  2xx 的普通非 JSON 也仍然是 `ApiError`（本线只收窄「拦截页」那一条路径）。
"""
from __future__ import annotations

import httpx
from django.test import SimpleTestCase

import jidanpu
from jidanpu.errors import (
    ApiError,
    AuthError,
    JidanpuError,
    RateLimitError,
    ServerError,
    UpstreamBlockedError,
)
from jidanpu.transport import unwrap_response

ENDPOINT = "https://jidanpu-egress.test.invalid/Login/Login"

#: 实测拦截面（结构值，⛔ 不是真站点的页面全文）：SafeLine 的拦截页。
SAFELINE_PAGE = (
    "<!DOCTYPE html><html><head><title>SafeLine</title></head>"
    "<body>Request blocked by SafeLine WAF</body></html>"
)
#: 不带 SafeLine 字样的普通 HTML（实测形态：2xx 回登录页 = 会话问题，⛔ 不是封禁）。
PLAIN_HTML_PAGE = "<html><body>请登录</body></html>"


def _response(
    status: int,
    *,
    text: str | None = None,
    json: object = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """构造一个带 request 的响应（`unwrap_response` 从 request 取 endpoint）。"""
    request = httpx.Request("POST", ENDPOINT)
    if json is not None:
        return httpx.Response(status, json=json, request=request, headers=headers)
    return httpx.Response(status, text=text or "", request=request, headers=headers)


class UpstreamBlockedTests(SimpleTestCase):
    """468 / 2xx 拦截页 ⇒ `UpstreamBlockedError`。"""

    def test_http_468_is_upstream_blocked_with_its_status_code(self):
        """468 ⇒ `UpstreamBlockedError`，且 `status_code == 468`。

        失败方向：468 落回 `ApiError`（= 本线要消灭的洞：调度层会把它当普通 4xx 重试）。
        """
        with self.assertRaises(UpstreamBlockedError) as caught:
            unwrap_response(_response(468, text=SAFELINE_PAGE))
        exc = caught.exception
        self.assertIsInstance(exc, JidanpuError)
        self.assertEqual(exc.status_code, 468)
        self.assertEqual(exc.endpoint, "POST /Login/Login")

    def test_468_keeps_retry_after_from_the_header(self):
        """`Retry-After` 头照旧解析进 `retry_after`（上层退避要用它）。

        失败方向：属性不加 / 永远 `None` ⇒ 本用例红。
        """
        with self.assertRaises(UpstreamBlockedError) as caught:
            unwrap_response(
                _response(468, text=SAFELINE_PAGE, headers={"Retry-After": "300"})
            )
        self.assertEqual(caught.exception.retry_after, 300.0)

    def test_200_with_a_safeline_page_is_upstream_blocked(self):
        """200 + SafeLine 拦截页 ⇒ `UpstreamBlockedError`（WAF 有时用 200 回拦截页）。

        失败方向：仍抛 `ApiError("服务端返回了非 JSON 响应")` ⇒ 本用例红。
        """
        with self.assertRaises(UpstreamBlockedError) as caught:
            unwrap_response(_response(200, text=SAFELINE_PAGE))
        self.assertEqual(caught.exception.status_code, 200)

    def test_200_with_plain_html_stays_api_error(self):
        """200 + `<html` 但**不自报** SafeLine ⇒ 仍是 `ApiError`（⛔ 不算封禁）。

        依据：2xx 回 HTML 也可能是**登录页**（会话掉了），换出口救不回来。
        失败方向：拿 `<html` 当兜底判据 ⇒ 本用例红。
        """
        with self.assertRaises(ApiError):
            unwrap_response(_response(200, text=PLAIN_HTML_PAGE))

    def test_the_error_text_never_carries_the_response_body(self):
        """`str(exc)` 里**没有**正文（468 与 200 两条路都要守住）。

        正文可能带 HTML/JS、也可能回显提交过的明文 ⇒ 一旦进文案就会漏进日志与告警。
        失败方向：把 `body=` / 页面文本拼进 `_describe` ⇒ 本用例红。
        """
        bodies = (
            (468, SAFELINE_PAGE),
            (200, SAFELINE_PAGE),
        )
        for status, body in bodies:
            with self.subTest(status=status):
                with self.assertRaises(UpstreamBlockedError) as caught:
                    unwrap_response(_response(status, text=body))
                text = str(caught.exception)
                self.assertNotIn("SafeLine", text)
                self.assertNotIn("<html", text)
                self.assertNotIn("Request blocked", text)

    def test_the_package_exports_the_new_error(self):
        """`jidanpu.UpstreamBlockedError` 可导入，且与 `jidanpu.errors` 里的是同一个类。

        失败方向：只在子模块定义、不接进 `__all__` / 顶层导入 ⇒ 调用方得从内部模块
        import（本仓约定调用方只 `import jidanpu`）⇒ 本用例红。
        """
        self.assertIs(jidanpu.UpstreamBlockedError, UpstreamBlockedError)
        self.assertIn("UpstreamBlockedError", jidanpu.__all__)


class UnchangedClassificationTests(SimpleTestCase):
    """回归钉子：既有状态码分类与「普通非 JSON」的语义**一个都不许改**。"""

    def test_401_is_still_an_auth_error(self):
        """401 ⇒ 仍是 `AuthError`（凭据问题，换出口没有用）。"""
        with self.assertRaises(AuthError):
            unwrap_response(_response(401, json={"code": 0, "msg": "未授权"}))

    def test_429_is_still_a_rate_limit_error(self):
        """429 ⇒ 仍是 `RateLimitError`，`retry_after` 照旧解析。"""
        with self.assertRaises(RateLimitError) as caught:
            unwrap_response(
                _response(
                    429,
                    json={"code": 0, "msg": "限流中"},
                    headers={"Retry-After": "7"},
                )
            )
        self.assertEqual(caught.exception.retry_after, 7.0)

    def test_500_is_still_a_server_error(self):
        """500 ⇒ 仍是 `ServerError`。"""
        with self.assertRaises(ServerError) as caught:
            unwrap_response(_response(500, json={"code": 0, "msg": "炸了"}))
        self.assertEqual(caught.exception.status_code, 500)

    def test_a_plain_non_json_body_is_still_an_api_error(self):
        """2xx + 非 JSON、也不是 HTML ⇒ 仍是 `ApiError`（本线只收窄「拦截页」那一条）。

        失败方向：把「非 JSON」整条路径都改成 `UpstreamBlockedError` ⇒ 本用例红
        （那样纯文本故障也会被当成封禁去换出口）。
        """
        with self.assertRaises(ApiError):
            unwrap_response(_response(200, text="plain text from upstream"))

    def test_a_normal_json_envelope_is_unaffected(self):
        """正常业务壳照旧解包（拦截面改动不许碰 JSON 那条路）。"""
        self.assertEqual(
            unwrap_response(_response(200, json={"code": 0, "msg": "success", "data": [1, 2]})),
            [1, 2],
        )
