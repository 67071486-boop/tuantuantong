"""微信消息加解密（**照官方算法逐步手写**，计划 T2 / S3）。

## 为什么手写、为什么这步必须自己验 `msg_signature`

官方「安全 / 兼容」模式下，微信推来的 body 是 `<xml><Encrypt><![CDATA[…]]></Encrypt></xml>`，
URL 上另带 `msg_signature`。**先验签、再解密**是安全模式的承重步：

- `msg_signature = sha1(字典序排序(token, timestamp, nonce, encrypt))` —— 它覆盖的是
  **密文本身**；先解密后验签等于「拿到明文才发现是假的」，而 AES 解密对篡改密文不设防
  （CBC 无认证）⇒ 漏这一步 = 接受伪造消息。malailai 靠 wechatpy 顺手做对了这一步；
  本仓手写实现（不引 wechatpy，D-0268），把顺序写死在 `decrypt_message()` 内部并配测试钉住
  （`tests/test_wechatmp_crypto.py`：msg_signature 错 ⇒ 403 且**不**尝试解密）。

- 明文模式的 `signature`（SHA1 of token/timestamp/nonce）**不覆盖 body** —— 重放面如实登记：
  v1 回调只翻 `WechatMpBinding.subscribed` 一位（计划 S3），无资金 / 状态语义。

## 判据（E-0003：断言第三方行为前先读源码/官方算法定义）

算法逐字照微信公众平台「加解密协议」：AES-256-CBC、key = `base64.b64decode(aes_key + "=")`
（官方给的 43 位串补一个 `=`）、IV = key 前 16 字节、PKCS#7 填充、明文结构
`16字节随机 + 4字节网络序消息长度 + XML + 接收方appid`。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct

from Crypto.Cipher import AES

#: `aes_key` 官方串长（43 位字符，base64 补位后 32 字节）。
AES_KEY_LENGTH = 43
#: 解出来的密钥长度（AES-256）。
AES_KEY_BYTES = 32
#: 明文头部的随机字节长。
RANDOM_BYTES_LENGTH = 16
#: 长度字段字节数（网络序 uint32）。
MSG_LENGTH_BYTES = 4


class WechatCryptoError(Exception):
    """验签 / 解密失败（密钥格式错、base64 错、填充错、appid 不符、长度截断）。

    ⚠️ 调用方把**全部**子类失败都翻译成 HTTP 403（回调端点对微信侧的统一拒绝码），
    ⛔ 不许把其中任何一档静默当成"消息不存在"。
    """

#: PKCS#7 填充的块大小。⚠️ 微信官方样例（`_PKCS7Encoder`）用的是 **32**（不是 AES 的 16）：
#: 合法填充字节的值范围是 1..32 —— 按 16 去判会把官方打出来的 pad=17..32 当成非法。
WECHAT_PAD_BLOCK_SIZE = 32


class MessageSignatureMismatch(WechatCryptoError):
    """`msg_signature` 与密文对不上（伪造 / 参数被改）。"""


class ReceiverAppidMismatch(WechatCryptoError):
    """密文尾部的接收方 appid 与本租户配置的 appid 不一致。"""


def check_signature(token: str, signature: str, timestamp: str, nonce: str) -> bool:
    """GET 验签：`sha1(字典序排序(token, timestamp, nonce))` 与 `signature` 比对。

    ⚠️ `hmac.compare_digest` 恒时比较（⛔ 不是 `==`：逐字节短路比较在计时侧泄露匹配前缀）。
    """
    if not (token and signature and timestamp and nonce):
        return False
    joined = "".join(sorted([token, timestamp, nonce]))
    expected = hashlib.sha1(joined.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, signature)


def check_message_signature(
    token: str, msg_signature: str, timestamp: str, nonce: str, encrypt: str
) -> bool:
    """安全 / 兼容模式的密文验签：`sha1(字典序排序(token, timestamp, nonce, encrypt))`。

    ⚠️ **必须在 AES 解密之前调**（见模块 docstring；顺序由 `decrypt_message()` 内部保证）。
    """
    if not (token and msg_signature and timestamp and nonce and encrypt):
        return False
    joined = "".join(sorted([token, timestamp, nonce, encrypt]))
    expected = hashlib.sha1(joined.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, msg_signature)


def _resolve_aes_key(aes_key: str) -> bytes:
    """官方 43 位 `EncodingAESKey` 串 → 32 字节密钥（补一个 `=` 再 base64 解）。"""
    if len(aes_key) != AES_KEY_LENGTH:
        raise WechatCryptoError(
            f"EncodingAESKey 长度必须为 {AES_KEY_LENGTH}（当前 {len(aes_key)}）"
        )
    try:
        key = base64.b64decode(aes_key + "=")
    except (binascii.Error, ValueError) as exc:
        raise WechatCryptoError("EncodingAESKey 不是合法的 base64 串") from exc
    if len(key) != AES_KEY_BYTES:
        raise WechatCryptoError("EncodingAESKey 解码后不是 32 字节")
    return key


def _pkcs7_unpad(data: bytes, block_size: int = WECHAT_PAD_BLOCK_SIZE) -> bytes:
    """PKCS#7 去填充（**严格**：填充字节的值必须全部相等且在 1..32，见常量处的注释）。"""
    if not data or len(data) % block_size != 0:
        raise WechatCryptoError("解密结果长度不是块大小的整数倍")
    pad = data[-1]
    if pad < 1 or pad > block_size:
        raise WechatCryptoError("PKCS#7 填充字节越界")
    if data[-pad:] != bytes([pad]) * pad:
        raise WechatCryptoError("PKCS#7 填充不一致")
    return data[:-pad]


def decrypt_message(
    *,
    aes_key: str,
    appid: str,
    encrypt: str,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    token: str,
) -> str:
    """安全 / 兼容模式的完整入口：**先验 `msg_signature`，再 AES 解密，再核 appid**。

    参数 `encrypt` = body 里 `<Encrypt>` 的 CDATA 原文（base64 密文）。
    成功 ⇒ 回包内的 XML 明文字符串；任何一步失败 ⇒ 抛 `WechatCryptoError` 子类。
    """
    # ① 先验签（⛔ 不先解密：S3 的承重步，测试钉住"签名错 ⇒ 不触碰 AES"）。
    if not check_message_signature(token, msg_signature, timestamp, nonce, encrypt):
        raise MessageSignatureMismatch("msg_signature 校验失败")
    # ② AES-256-CBC 解密 + 去 PKCS#7 填充。
    key = _resolve_aes_key(aes_key)
    try:
        ciphertext = base64.b64decode(encrypt)
    except (binascii.Error, ValueError) as exc:
        raise WechatCryptoError("Encrypt 不是合法的 base64 串") from exc
    if len(ciphertext) % AES.block_size != 0 or not ciphertext:
        raise WechatCryptoError("密文长度不是 16 字节的整数倍")
    cipher = AES.new(key, AES.MODE_CBC, key[:RANDOM_BYTES_LENGTH])
    plain = _pkcs7_unpad(cipher.decrypt(ciphertext))
    # ③ 剥掉 16 字节随机串 + 4 字节长度头，按长度截 XML，剩下的必须恰是接收方 appid。
    if len(plain) < RANDOM_BYTES_LENGTH + MSG_LENGTH_BYTES:
        raise WechatCryptoError("明文长度不够放消息头")
    content = plain[RANDOM_BYTES_LENGTH:]
    (msg_len,) = struct.unpack(">I", content[:MSG_LENGTH_BYTES])
    tail = content[MSG_LENGTH_BYTES:]
    if msg_len > len(tail):
        raise WechatCryptoError("消息长度字段越界")
    xml = tail[:msg_len]
    from_appid = tail[msg_len:]
    if from_appid != appid.encode("utf-8"):
        # ⚠️ 尾巴是 bytes、入参是 str：必须编码后比（漏编码会让合法包恒被拒——首跑实测）。
        raise ReceiverAppidMismatch("密文内接收方 appid 与配置不一致")
    return xml.decode("utf-8")
