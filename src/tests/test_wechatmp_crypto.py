"""`apps/wechatmp/crypto.py`（微信消息加解密）的判据 —— ⛔ 零网络、零 DB（SimpleTestCase）。

## 判据怎么造（⛔ 不用被测函数造期望值，E-0024 族）

签名与加密侧都在 `tests/_wechatmp_helpers.py` **独立实现**一遍官方算法
（sha1 字典序 / AES-256-CBC / 16 随机 + 4 长度 + XML + appid）——被测代码与测试代码
是**两份实现**，对得上才叫过。

## S3 的承重钉

「safe 模式**必须先验 `msg_signature` 再解密**」的证明力在
`test_wrong_msg_signature_is_rejected_before_decryption`：密文**与**签名**都**坏时，
报的必须是签名错（`MessageSignatureMismatch`）而不是解密错 —— 若实现先解密，
坏的密文会先在 AES/base64 层炸出另一种异常，这条用例当场红。
"""
from __future__ import annotations

import base64
from unittest import mock

from django.test import SimpleTestCase

from apps.wechatmp.crypto import (
    MessageSignatureMismatch,
    ReceiverAppidMismatch,
    WechatCryptoError,
    check_message_signature,
    check_signature,
    decrypt_message,
)
from tests._wechatmp_helpers import (
    AES_KEY,
    APPID,
    TOKEN,
    encrypt_message,
    event_xml,
    sha1_signature,
    wrap_encrypted_body,
)

TIMESTAMP = "1700000000"
NONCE = "nonce-synthetic"


class CheckSignatureTests(SimpleTestCase):
    """GET 验签（echostr 那一步的判据）。"""

    def test_valid_signature_passes(self):
        signature = sha1_signature([TOKEN, TIMESTAMP, NONCE])
        self.assertTrue(check_signature(TOKEN, signature, TIMESTAMP, NONCE))

    def test_wrong_signature_fails(self):
        signature = sha1_signature([TOKEN, TIMESTAMP, "other-nonce"])
        self.assertFalse(check_signature(TOKEN, signature, TIMESTAMP, NONCE))

    def test_wrong_token_fails(self):
        signature = sha1_signature([TOKEN, TIMESTAMP, NONCE])
        self.assertFalse(check_signature("other-token", signature, TIMESTAMP, NONCE))

    def test_empty_parts_fail_closed(self):
        # 四个参数任何一个为空 ⇒ False（⛔ 不是抛异常：回调端点要拿它回 403）。
        for args in (
            ("", "sig", TIMESTAMP, NONCE),
            (TOKEN, "", TIMESTAMP, NONCE),
            (TOKEN, "sig", "", NONCE),
            (TOKEN, "sig", TIMESTAMP, ""),
        ):
            self.assertFalse(check_signature(*args))


class CheckMessageSignatureTests(SimpleTestCase):
    """safe / compatible 模式的密文验签（四参：多了 encrypt）。"""

    def test_valid_signature_passes(self):
        encrypt = encrypt_message(aes_key=AES_KEY, appid=APPID, xml=event_xml(event="subscribe", openid="oX"))
        signature = sha1_signature([TOKEN, TIMESTAMP, NONCE, encrypt])
        self.assertTrue(check_message_signature(TOKEN, signature, TIMESTAMP, NONCE, encrypt))

    def test_signature_over_modified_encrypt_fails(self):
        encrypt = encrypt_message(aes_key=AES_KEY, appid=APPID, xml=event_xml(event="subscribe", openid="oX"))
        signature = sha1_signature([TOKEN, TIMESTAMP, NONCE, encrypt])
        tampered = encrypt[:-4] + "AAAA"
        self.assertFalse(
            check_message_signature(TOKEN, signature, TIMESTAMP, NONCE, tampered)
        )


class DecryptMessageTests(SimpleTestCase):
    def _decrypt(self, *, xml: str, msg_signature: str | None = None, encrypt: str | None = None):
        if encrypt is None:
            encrypt = encrypt_message(aes_key=AES_KEY, appid=APPID, xml=xml)
        if msg_signature is None:
            msg_signature = sha1_signature([TOKEN, TIMESTAMP, NONCE, encrypt])
        return decrypt_message(
            aes_key=AES_KEY,
            appid=APPID,
            encrypt=encrypt,
            msg_signature=msg_signature,
            timestamp=TIMESTAMP,
            nonce=NONCE,
            token=TOKEN,
        )

    def test_roundtrip_returns_inner_xml(self):
        xml = event_xml(event="subscribe", openid="openid-synthetic-roundtrip")
        self.assertEqual(self._decrypt(xml=xml), xml)

    # ⭐ S3 承重钉：签名错 ⇒ **在解密之前**就拒（证据 = 异常类型是签名错，不是解密错）。
    def test_wrong_msg_signature_is_rejected_before_decryption(self):
        xml = event_xml(event="subscribe", openid="openid-synthetic-x")
        # 密文也砸坏：若实现先解密，AES/base64 层会先炸出别的错；先验签的实现必然报签名错。
        good_encrypt = encrypt_message(aes_key=AES_KEY, appid=APPID, xml=xml)
        bad_encrypt = "!!!!not-base64!!!!"
        with self.assertRaises(MessageSignatureMismatch):
            self._decrypt(
                xml=xml,
                encrypt=bad_encrypt,
                msg_signature=sha1_signature([TOKEN, TIMESTAMP, NONCE, good_encrypt]),
            )

    def test_receiver_appid_mismatch_is_rejected(self):
        xml = event_xml(event="subscribe", openid="openid-synthetic-y")
        encrypt = encrypt_message(aes_key=AES_KEY, appid="wxOTHER0000000000", xml=xml)
        msg_signature = sha1_signature([TOKEN, TIMESTAMP, NONCE, encrypt])
        with self.assertRaises(ReceiverAppidMismatch):
            self._decrypt(xml=xml, encrypt=encrypt, msg_signature=msg_signature)

    def test_bad_key_length_is_rejected(self):
        encrypt = encrypt_message(aes_key=AES_KEY, appid=APPID, xml="<xml/>")
        msg_signature = sha1_signature([TOKEN, TIMESTAMP, NONCE, encrypt])
        with self.assertRaises(WechatCryptoError):
            decrypt_message(
                aes_key="short-key",
                appid=APPID,
                encrypt=encrypt,
                msg_signature=msg_signature,
                timestamp=TIMESTAMP,
                nonce=NONCE,
                token=TOKEN,
            )

    def test_tampered_ciphertext_with_valid_signature_shape_fails_clean(self):
        # 签名按坏密文重算（签名"合法"）、但密文被翻转 ⇒ 解密层必须干净地报错，⛔ 不许吐出乱码 XML。
        xml = event_xml(event="unsubscribe", openid="openid-synthetic-z")
        encrypt = encrypt_message(aes_key=AES_KEY, appid=APPID, xml=xml)
        raw = bytearray(base64.b64decode(encrypt))
        raw[20] ^= 0xFF  # 翻转第一个数据块里的一个字节（CBC 下解密结果整体变乱码）
        tampered = base64.b64encode(bytes(raw)).decode("ascii")
        msg_signature = sha1_signature([TOKEN, TIMESTAMP, NONCE, tampered])
        with self.assertRaises(WechatCryptoError):
            self._decrypt(xml=xml, encrypt=tampered, msg_signature=msg_signature)

    # ⛔ 自证（E-0029 同族）：上面的对照面真的接上了 —— 独立实现的加密侧产出能被
    # 被测代码原样解回来。这条如果红，说明共享夹具坏了，其它绿读数全部作废。
    def test_fixture_helper_encrypt_roundtrip_through_production_code(self):
        xml = event_xml(event="VIEW", openid="openid-synthetic-selfcheck")
        # 额外钉「官方 32 字节 PKCS#7」那一档：内容长到让 pad = 17..32 之间
        # （16 字节块的判据会误杀它 —— crypto.WECHAT_PAD_BLOCK_SIZE 的存在理由）。
        long_xml = event_xml(event="VIEW", openid="o" * 300)
        self.assertEqual(self._decrypt(xml=xml), xml)
        self.assertEqual(self._decrypt(xml=long_xml), long_xml)

    def test_decrypt_never_invoked_without_signature_check(self):
        """结构侧自证：`decrypt_message` 的第一步就是 `check_message_signature`
        （源码顺序钉 —— 防将来有人把两步调换位置但测试没跑到那条路径）。"""
        import inspect

        from apps.wechatmp import crypto

        source = inspect.getsource(crypto.decrypt_message)
        assert_pos = source.index("check_message_signature")
        decrypt_pos = source.index("AES.new")
        self.assertLess(
            assert_pos,
            decrypt_pos,
            "decrypt_message 里验签必须出现在 AES 解密之前（S3 的承重顺序）",
        )
