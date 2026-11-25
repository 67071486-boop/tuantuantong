"""`peer_client` 出站测试的**流式假件**（审计批 C5 改走 `httpx.stream` 后共用）。

## 为什么住一个中立模块（照 `_openapi_fixtures.py` 的先例）

C5 之前各测试文件各自 `mock.patch("apps.business.peer_client.httpx.post")` + 一个只会
`json()` 的 MagicMock；C5 让出站走**流式读 + 大小上限** ⇒ 假件必须会「上下文管理器 +
`iter_bytes()`」。五个测试文件（`test_peer_client` / `test_peer_p3_reconcile` /
`test_peer_hall_pull` / `test_peer_partial_failure` / `test_peer_binding_api`）都要同一形状
⇒ 桩只许有一份（⛔ 各抄一份迟早漂）。

⚠️ 本模块**不是测试模块**（文件名不以 `test` 开头 ⇒ 不被测试发现），只放假件。
⚠️ 桩打在**被查找的那个模块**上（`apps.business.peer_client.httpx.stream`，E-0029）。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from unittest import mock


class FakeStreamResponse:
    """`httpx.Client.stream(...)` 上下文里那个响应的最小假件。

    - `status_code`：路由形状分类（404/405 ⇒ `PeerUnsupported`）在读 body **之前**判；
    - `iter_bytes()`：流式读的出口；`body` 整块给出（真传输会分块，但调用方只逐块累计，
      单块与多块对它无差别）。
    """

    def __init__(self, *, status_code: int = 200, body: bytes | None = None,
                 envelope=None):
        if envelope is not None:
            body = json.dumps(envelope).encode("utf-8")
        self.status_code = status_code
        self._body = body if body is not None else b""
        self.headers = {"Content-Length": str(len(self._body))}
        self.num_bytes_downloaded = 0

    def iter_bytes(self):
        self.num_bytes_downloaded = len(self._body)
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def stream_response(envelope=None, *, status_code: int = 200,
                    body: bytes | None = None) -> FakeStreamResponse:
    """便捷构造：给信封（自动 JSON 编码）或裸字节。"""
    return FakeStreamResponse(status_code=status_code, body=body, envelope=envelope)


@contextmanager
def _enter(resp):
    yield resp


def patch_peer_stream(resp=None, *, side_effect=None):
    """把 `peer_client.httpx.stream` 换成假件；`side_effect`（异常 / 可调用）原样透传。

    返回 `mock._patcher`（`with patch_peer_stream(...)` 使用）。补丁对象本身是
    MagicMock ⇒ `call_args` 照常可用（`args = ("POST", <url>)`，kwargs 含
    `json` / `headers` / `timeout`）。
    """
    kwargs: dict = {}
    if resp is not None:
        kwargs["return_value"] = _enter(resp)
    if side_effect is not None:
        kwargs["side_effect"] = side_effect
    return mock.patch("apps.business.peer_client.httpx.stream", **kwargs)


def fake_sync_claim_upstream(fixture, captured, *, drop_replies=False):
    """⭐ D-0347：把 `peer_client.push_events` 换成「真受理机」假身（同步抢单用）。

    拦截的是**运输层**；判决由**真的** `apply_upstream_events`（对端受理机本体）生成，
    响应用**真端点的同一个组装函数** `_outcome_to_dict` 拼装——响应车键的存缺纪律
    （空 ⇒ 键不出现）在假身里逐字成立，变异与用例才能打到它。

    - `fixture`：双侧夹具（要 `fixture.down_binding` = 上家侧看来的下家绑定）；
    - `captured`：记下每次出站的 `base_url` / `token` / `timeout` / 事件体；
    - `drop_replies=True`：模拟**老版本对端**——响应里没有 `claim_replies` 键
      （上家照样真实受理、`order.assign` 照样排进它的 outbox，正是「迟到接受」的形状）。

    返回 `mock._patcher`（`with fake_sync_claim_upstream(...) as push:` 使用）。
    """
    from apps.business.api.peer_views import _outcome_to_dict
    from apps.business.peer_inbound import apply_upstream_events

    def fake(base_url, token, events, *, timeout=None):
        captured["base_url"] = base_url
        captured["token"] = token
        captured["timeout"] = timeout
        captured.setdefault("events", []).append([dict(e) for e in events])
        outcome = apply_upstream_events(list(events), binding=fixture.down_binding)
        data = _outcome_to_dict(outcome)
        if drop_replies:
            data.pop("claim_replies", None)
        return data

    return mock.patch("apps.business.peer_client.push_events", side_effect=fake)
