"""微信公众号测试的共享夹具（⛔ 全部合成值；`AGENTS.md` §5-C / E-0077）。

## 两个角色

1. **`WechatApiStub`**：零网络假微信 —— `httpx.MockTransport` 只决定"对方回什么"，
   请求本身是真的（照 `tests/test_realname_vendor.py::_VendorStub` 手法）。记录每一次
   请求对象，用例能对真实发出的 URL / query / body 下断言。
2. **`encrypt_message` / `msg_signature`**：**测试内独立实现**的官方加解密对照面
   （与 `apps/wechatmp/crypto.py` 分开写 —— 用被测函数自己造期望值是自指，E-0024 族；
   先例 = realname vendor 那条「测试内独立实现一次 HMAC-SHA1」）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from unittest import mock

import httpx
from Crypto.Cipher import AES

from apps.wechatmp.client import WechatMpApiClient

# ── 合成配置值（⛔ 一个真值都没有；43 位 aes_key 按 base64 形状手造）────────────
APPID = "wx1234567890abcdef"
APPSECRET = "appsecret-synthetic-0123456789abcdef"
TOKEN = "token-synthetic-0123456789"
AES_KEY = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"  # 43 位合成串
TEMPLATE_ID = "template-synthetic-0001"
OPENID = "openid-synthetic-0123456789abcdef"
OPENID_OTHER = "openid-synthetic-ffffffffffffffff"
#: 验签用的时间戳与 nonce（固定值 ⇒ 签名可复现）。
TIMESTAMP = "1700000000"
NONCE = "nonce-synthetic"


def sha1_signature(parts: list[str]) -> str:
    """`sha1(字典序排序后拼接)`——GET 验签与 `msg_signature` 同一条公式，独立实现。"""
    return hashlib.sha1("".join(sorted(parts)).encode("utf-8")).hexdigest()


def _pad(data: bytes) -> bytes:
    """PKCS#7（块大小 **32**，微信官方样例的 `_PKCS7Encoder` 口径）。"""
    amount = 32 - len(data) % 32
    return data + bytes([amount]) * amount


def encrypt_message(*, aes_key: str, appid: str, xml: str) -> str:
    """官方算法的**加密侧**独立实现：16 随机 + 4 长度 + XML + appid → AES-CBC → base64。"""
    key = base64.b64decode(aes_key + "=")
    msg = xml.encode("utf-8")
    content = os.urandom(16) + struct.pack(">I", len(msg)) + msg + appid.encode("utf-8")
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    return base64.b64encode(cipher.encrypt(_pad(content))).decode("ascii")


def wrap_encrypted_body(encrypt: str) -> bytes:
    """微信安全模式推来的 body 形状：`<xml><Encrypt><![CDATA[…]]></Encrypt></xml>`。"""
    return f"<xml><ToUserName><![CDATA[{APPID}]]></ToUserName><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>".encode()


def event_xml(*, event: str, openid: str) -> str:
    """关注 / 取关事件的明文 XML（官方事件推送的固定形状）。"""
    return (
        f"<xml><ToUserName><![CDATA[{APPID}]]></ToUserName>"
        f"<FromUserName><![CDATA[{openid}]]></FromUserName>"
        f"<CreateTime>1700000000</CreateTime>"
        f"<MsgType><![CDATA[event]]></MsgType>"
        f"<Event><![CDATA[{event}]]></Event></xml>"
    )


def query_params(token: str, *, timestamp: str = TIMESTAMP, nonce: str = NONCE, **extra: str) -> dict:
    """GET 验签三参（+ 可选 echostr / msg_signature）。签名按**独立实现**算。"""
    params = {
        "timestamp": timestamp,
        "nonce": nonce,
        "signature": sha1_signature([token, timestamp, nonce]),
    }
    params.update(extra)
    return params


class WechatApiStub:
    """零网络假微信。`reply` 按调用序排队；记录每次请求（URL / query / body）。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._replies: list[tuple[int, object]] = []

    def enqueue(self, payload: object, *, status: int = 200) -> "WechatApiStub":
        self._replies.append((status, payload))
        return self

    def transport(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, payload = self._replies.pop(0) if self._replies else (200, {"errcode": -1, "errmsg": "stub exhausted"})
        if isinstance(payload, (dict, list)):
            return httpx.Response(status, json=payload, request=request)
        return httpx.Response(status, text=str(payload), request=request)

    def install(self) -> "WechatApiStub":
        """把 `services` 模块级的单例换成走 MockTransport 的**真**客户端（E-0029：
        打在"被查找的那个模块"上；`api_client()` 命中缓存就不会再去建真客户端）。"""
        patcher = mock.patch(
            "apps.wechatmp.services._CLIENT",
            WechatMpApiClient(transport=httpx.MockTransport(self.transport)),
        )
        patcher.start()
        self._patcher = patcher
        return self

    def stop(self) -> None:
        self._patcher.stop()

    # ── 断言助手 ──────────────────────────────────────────────────────────

    def last_json(self) -> dict:
        return json.loads(self.requests[-1].content.decode("utf-8"))

    def last_url(self) -> str:
        return str(self.requests[-1].url)

    def call_count_for(self, path: str) -> int:
        return sum(1 for r in self.requests if path in str(r.url))
