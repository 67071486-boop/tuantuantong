"""`apps/business/peer_client.py` 的**出站**行为（spec §5.1 / §5.2 / §7.2）。

这是本线**唯一**的出站点，三个函数各自把「网络层失败」与「对端拒收」分成两种异常，
页面端点再把它们翻成业务码 1021 / 1020 ⇒ 这里必须把**分界线**钉住：
- 连不上 / 超时 / 非 JSON / 形状不对 / **应答超过大小上限** ⇒ `PeerUnreachable`；
- 收到信封但业务码非 0 ⇒ `PeerRejected`。

⚠️ 桩打在**被查找的那个模块**上（`apps.business.peer_client.httpx.stream`，经
`tests._peer_stream_fakes` 共用假件—— C5 起出站是**流式读**），⛔ 不是定义
`stream` 的 `httpx` 模块本身（`AGENTS.md` §5-E / E-0029）。
"""
import json
from unittest import mock

from django.test import SimpleTestCase

import httpx

from apps.business import peer_client
from apps.business.peer_client import (
    BIND_TIMEOUT_SECONDS,
    PEER_RESPONSE_MAX_BYTES,
    PeerRejected,
    PeerUnreachable,
    bind_upstream,
    push_events,
    unbind_upstream,
)
from tests._peer_stream_fakes import patch_peer_stream, stream_response

BASE_URL = "https://peer-a.test.invalid"
BIND_URL = "https://peer-a.test.invalid/api/business/peer-sync/bind/"


def _bind(**kwargs_over):
    kwargs = dict(
        account="peer-account-a",
        password="peer-password-a",
        token_out="TOKEN-OUT",
        local_base_url="https://peer-b.test.invalid",
    )
    kwargs.update(kwargs_over)
    base_url = kwargs.pop("base_url", BASE_URL)
    return base_url, kwargs


class BindUpstreamTests(SimpleTestCase):
    def _post(self, envelope, **kwargs):
        base_url, bind_kwargs = _bind(**kwargs)
        with patch_peer_stream(stream_response(envelope)) as stream:
            token = bind_upstream(base_url, **bind_kwargs)
        return token, stream

    def test_success_returns_the_token_the_upstream_handed_back(self):
        token, stream = self._post({"code": 0, "data": {"token_out": "TOKEN-UP"}})
        self.assertEqual(token, "TOKEN-UP")
        method, url = stream.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(url, BIND_URL)
        self.assertEqual(
            stream.call_args.kwargs["json"],
            {
                "account": "peer-account-a",
                "password": "peer-password-a",
                "peer_base_url": "https://peer-b.test.invalid",
                "token_out": "TOKEN-OUT",
            },
        )
        # 握手是**唯一**一个不带令牌的调用（令牌还没换到）。
        self.assertNotIn("Authorization", stream.call_args.kwargs.get("headers") or {})

    def test_a_trailing_slash_on_the_base_url_does_not_double_the_separator(self):
        token, stream = self._post(
            {"code": 0, "data": {"token_out": "TOKEN-UP"}},
            base_url=BASE_URL + "/",
        )
        self.assertEqual(token, "TOKEN-UP")
        self.assertEqual(stream.call_args.args[1], BIND_URL)

    def test_the_short_handshake_timeout_is_used_by_default(self):
        """握手是「人在页面上等」的动作（spec §5.1）⇒ 默认超时必须短（**5 秒**）。

        ⚠️ 评审 C4：旧版断言 `timeout == BIND_TIMEOUT_SECONDS` 是**自指**——把常量改成
        `300` 也照样绿。这里钉**字面 5.0**，并把常量本身也钉住（两处一起改，⛔ 不留恒真断言）。
        """
        _, stream = self._post({"code": 0, "data": {"token_out": "TOKEN-UP"}})
        self.assertEqual(stream.call_args.kwargs["timeout"], 5.0)
        self.assertEqual(BIND_TIMEOUT_SECONDS, 5.0)

    def test_a_non_zero_business_code_is_a_rejection_not_a_network_error(self):
        with self.assertRaises(PeerRejected) as ctx:
            self._post({"code": 1018, "msg": "上家系统的账号或密码不对"})
        self.assertEqual(str(ctx.exception), "上家系统的账号或密码不对")

    def test_a_rejection_without_a_message_still_raises_without_crashing(self):
        """⛔ 空 `msg` 不许变成 `KeyError` / `TypeError`（那会 500 在页面上）。"""
        with self.assertRaises(PeerRejected) as ctx:
            self._post({"code": 1018, "msg": ""})
        self.assertTrue(str(ctx.exception))

    def test_a_transport_error_is_unreachable(self):
        base_url, bind_kwargs = _bind()
        with patch_peer_stream(side_effect=httpx.ConnectError("connection refused")):
            with self.assertRaises(PeerUnreachable):
                bind_upstream(base_url, **bind_kwargs)

    def test_a_body_that_is_not_json_is_unreachable(self):
        base_url, bind_kwargs = _bind()
        with patch_peer_stream(stream_response(body=b"<html>not json</html>")):
            with self.assertRaises(PeerUnreachable):
                bind_upstream(base_url, **bind_kwargs)

    def test_json_that_is_not_an_object_is_unreachable_not_a_crash(self):
        """对端回了合法 JSON 但不是信封（如裸列表）⇒ 形状不对，仍按不可达报。

        ⛔ 不接这一支的话 `envelope.get` 会抛 `AttributeError` ⇒ 页面端点 500
        （未预期的对端变更伪装成"我们的接口坏了"）。
        """
        base_url, bind_kwargs = _bind()
        with patch_peer_stream(stream_response(["not", "an", "envelope"])):
            with self.assertRaises(PeerUnreachable):
                bind_upstream(base_url, **bind_kwargs)

    def test_a_success_envelope_with_an_unusable_data_shape_is_unreachable(self):
        """`data` 缺失 / 不是对象 / 没有 `token_out` ⇒ 一律不可达，⛔ 不静默当空串。"""
        base_url, bind_kwargs = _bind()
        for envelope in (
            {"code": 0},
            {"code": 0, "data": []},
            {"code": 0, "data": "TOKEN-UP"},
            {"code": 0, "data": {}},
            {"code": 0, "data": {"token_out": ""}},
        ):
            with self.subTest(envelope=envelope):
                with patch_peer_stream(stream_response(envelope)):
                    with self.assertRaises(PeerUnreachable):
                        bind_upstream(base_url, **bind_kwargs)


class PushEventsTests(SimpleTestCase):
    def test_events_are_posted_with_the_bearer_token(self):
        with patch_peer_stream(
            stream_response({"code": 0, "data": None})
        ) as stream:
            push_events(BASE_URL, "TOKEN-IN", [{"k": 1}])
        self.assertEqual(
            stream.call_args.args[1],
            "https://peer-a.test.invalid/api/business/peer-sync/inbound/events/",
        )
        self.assertEqual(stream.call_args.kwargs["json"], {"events": [{"k": 1}]})
        self.assertEqual(
            stream.call_args.kwargs["headers"], {"Authorization": "Bearer TOKEN-IN"}
        )

    def test_a_non_zero_code_is_a_rejection(self):
        with patch_peer_stream(stream_response({"code": 1020, "msg": "令牌无效"})):
            with self.assertRaises(PeerRejected):
                push_events(BASE_URL, "TOKEN-IN", [])


class UnbindUpstreamTests(SimpleTestCase):
    def test_unbind_posts_the_token_to_the_unbind_endpoint(self):
        with patch_peer_stream(
            stream_response({"code": 0, "data": None})
        ) as stream:
            unbind_upstream(BASE_URL, "TOKEN-IN")
        self.assertEqual(
            stream.call_args.args[1],
            "https://peer-a.test.invalid/api/business/peer-sync/unbind/",
        )
        self.assertEqual(
            stream.call_args.kwargs["headers"], {"Authorization": "Bearer TOKEN-IN"}
        )

    def test_a_non_zero_code_is_a_rejection_the_caller_can_swallow(self):
        """视图层要能**分辨**这两种异常并都吞掉（spec §5.2 第 5 条）⇒ 这里钉住分界线。"""
        with patch_peer_stream(stream_response({"code": 1020, "msg": "令牌无效"})):
            with self.assertRaises(PeerRejected):
                unbind_upstream(BASE_URL, "TOKEN-IN")

    def test_a_transport_error_is_unreachable(self):
        with patch_peer_stream(side_effect=httpx.ReadTimeout("timed out")):
            with self.assertRaises(PeerUnreachable):
                unbind_upstream(BASE_URL, "TOKEN-IN")


class ResponseSizeCapTests(SimpleTestCase):
    """⭐ C5（L-3，2026-10-25 审计批）：应答**流式读 + 上限**，超限按失联处理。

    ⚠️ 判据必须打在「读的过程中」断掉（流式），⛔ 不是「读完再比长度」——
    那样一个数 GB 的应答已经把内存吃满了，闸是摆设。用真上限 + 真超限体直接量。
    """

    def test_an_oversized_response_body_is_unreachable(self):
        """超过 `PEER_RESPONSE_MAX_BYTES` 的应答 ⇒ `PeerUnreachable`（按对端没回话走退避）。"""
        oversized = b"x" * (PEER_RESPONSE_MAX_BYTES + 1)
        with patch_peer_stream(stream_response(body=oversized)):
            with self.assertRaises(PeerUnreachable) as ctx:
                push_events(BASE_URL, "TOKEN-IN", [{"k": 1}])
        self.assertIn("上限", str(ctx.exception))

    def test_a_body_of_exactly_the_cap_still_parses(self):
        """边界：**恰好**到上限不许误杀（否则正常大批量会被一刀切）。"""
        envelope = {"code": 0, "data": {"applied": 1}}
        raw = json.dumps(envelope).encode()
        padding = PEER_RESPONSE_MAX_BYTES - len(raw)
        # 在开括号后垫空白（JSON 允许的空白）把体撑到恰好上限。
        body = b"{" + b" " * padding + raw[1:]
        with patch_peer_stream(stream_response(body=body)):
            data = push_events(BASE_URL, "TOKEN-IN", [{"k": 1}])
        self.assertEqual(data, {"applied": 1})

    def test_the_cap_constant_is_ten_megabytes(self):
        """上限定值钉子：10 MB（改这个数 = 改对端的信任边界，必须当场红）。"""
        self.assertEqual(PEER_RESPONSE_MAX_BYTES, 10 * 1024 * 1024)
        # 名字仍然从模块取得到（桩/后人改名字时这条先红）。
        self.assertTrue(hasattr(peer_client, "PEER_RESPONSE_MAX_BYTES"))
