"""企微自建应用测试的共享夹具（⛔ 全部合成值；`AGENTS.md` §5-C / E-0077）。

## 两个角色（照 `tests/_wechatmp_helpers.py` 的同一套手法）

1. **`WecomApiStub`**：零网络假企微——拦 `requests` 层（wechatpy 走 `requests.Session`），
   只决定"对方回什么"，请求本身是真的；记录每一次请求，用例能对真实发出的
   URL / query / body 下断言。打桩位置见该类的 `install()`（E-0029：打在被查找的模块）。
2. **`encrypt_message` / `signature`**：**测试内独立实现**的官方加解密对照面
   （与 wechatpy 的实现分开写——用被测函数自己造期望值是自指，E-0024 族）。
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct
from unittest import mock

from Crypto.Cipher import AES

# ── 合成配置值（⛔ 一个真值都没有；43 位 aes_key 按 base64 形状手造）────────────
CORP_ID = "ww1234567890abcdef"
AGENT_ID = 1000002
SECRET = "wecom-secret-synthetic-0123456789abcdef"
CONTACTS_SECRET = "wecom-contacts-secret-synthetic-0123456789abcdef"
TOKEN = "wecom-token-synthetic-0123456789"
AES_KEY = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"  # 43 位合成串
#: 验签用的时间戳与 nonce（固定值 ⇒ 签名可复现）。
TIMESTAMP = "1700000000"
NONCE = "nonce-wecom-synthetic"


def signature(parts: list[str]) -> str:
    """`sha1(字典序排序后拼接)`——企微 GET 验签与 `msg_signature` 同一条公式，独立实现。"""
    return hashlib.sha1("".join(sorted(parts)).encode("utf-8")).hexdigest()


def _pad(data: bytes) -> bytes:
    """PKCS#7（块大小 **32**，微信官方样例的 `_PKCS7Encoder` 口径）。"""
    amount = 32 - len(data) % 32
    return data + bytes([amount]) * amount


def encrypt_message(*, aes_key: str, corp_id: str, xml: str) -> str:
    """官方算法的**加密侧**独立实现：16 随机 + 4 长度 + XML + corp_id → AES-CBC → base64。"""
    key = base64.b64decode(aes_key + "=")
    msg = xml.encode("utf-8")
    content = os.urandom(16) + struct.pack(">I", len(msg)) + msg + corp_id.encode("utf-8")
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    return base64.b64encode(cipher.encrypt(_pad(content))).decode("ascii")


def wrap_encrypted_body(encrypt: str) -> bytes:
    """企微安全模式推来的 body 形状：`<xml><Encrypt><![CDATA[…]]></Encrypt></xml>`。"""
    return f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>".encode()


def text_message_xml(*, from_user: str, content: str, msg_id: str) -> str:
    """文本消息的明文 XML（企微回调的固定形状）。"""
    return (
        f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>1700000000</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"<MsgId>{msg_id}</MsgId>"
        f"<AgentID>{AGENT_ID}</AgentID></xml>"
    )


def event_message_xml(*, from_user: str, event: str) -> str:
    """事件推送的明文 XML（事件类没有 MsgId）。"""
    return (
        f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>1700000000</CreateTime>"
        f"<MsgType><![CDATA[event]]></MsgType>"
        f"<Event><![CDATA[{event}]]></Event></xml>"
    )


def query_params(*, timestamp: str = TIMESTAMP, nonce: str = NONCE, **extra: str) -> dict:
    """GET 验签三参（+ 可选 echostr / msg_signature）。签名按**独立实现**算。"""
    params = {
        "timestamp": timestamp,
        "nonce": nonce,
        "signature": signature([TOKEN, timestamp, nonce]),
    }
    params.update(extra)
    return params


class WecomApiStub:
    """零网络假企微（拦 `requests.Session` 的 `request`）。`reply` 按调用序排队。"""

    def __init__(self) -> None:
        self.requests: list[object] = []
        self._replies: list[tuple[int, object]] = []

    def enqueue(self, payload: object, *, status: int = 200) -> "WecomApiStub":
        self._replies.append((status, payload))
        return self

    def _transport(self, method: str, url: str, **kwargs) -> object:
        import json as _json

        recorded = type(
            "RecordedRequest", (), {"method": method, "url": url, "kwargs": kwargs}
        )()
        self.requests.append(recorded)
        status, payload = (
            self._replies.pop(0)
            if self._replies
            else (200, {"errcode": -1, "errmsg": "stub exhausted"})
        )
        content = (
            payload.encode("utf-8")
            if isinstance(payload, str)
            else _json.dumps(payload).encode("utf-8")
        )
        response = type(
            "FakeResponse",
            (),
            {
                "status_code": status,
                "_payload": payload,
                "_content": content,
                "request": recorded,
                "raise_for_status": lambda self: None,
                "json": lambda self: self._payload,
                "text": lambda self: str(self._payload),
                "content": content,
            },
        )()
        return response

    def install(self) -> "WecomApiStub":
        """把 `wechatpy` 客户端基类的 `_http` 会话换成假传输（E-0029：wechatpy 的
        `BaseWeChatClient.__init__` 建立 `self._http = requests.Session()` ⇒ 打在
        **requests.Session**（被查找的那个类）上，客户端照常构造、网络被换掉）。"""
        parent = self

        class StubSession:
            @staticmethod
            def _url_with_params(url, params):
                query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
                return f"{url}?{query}" if query else url

            def get(self, url, params=None, **kwargs):
                return parent._transport("GET", self._url_with_params(url, params))

            def post(self, url, data=None, json=None, **kwargs):
                return parent._transport("POST", url, data=data, json=json)

            def request(self, method, url, params=None, **kwargs):
                # wechatpy 的业务端点走 `request(method, url, params=…, …)`（不是 get/post）：
                # query 参数同样拼回 URL，让用例能对真实发出的 URL 下断言。
                return parent._transport(method, self._url_with_params(url, params), **kwargs)

        patcher = mock.patch("requests.Session", StubSession)
        patcher.start()
        self._patcher = patcher
        return self

    def stop(self) -> None:
        self._patcher.stop()

    # ── 断言助手 ──────────────────────────────────────────────────────────

    def last_url(self) -> str:
        return str(self.requests[-1].url)

    def call_count_for(self, path: str) -> int:
        return sum(1 for r in self.requests if path in str(r.url))
