"""`wechatpy.work.crypto.WeChatCrypto` 接线的判据（计划 §3：`test_wecom_crypto.py`）。

## 判据打在哪

- **往返还原**：独立实现加密（`_wecom_helpers.encrypt_message`，E-0024 族：与被测实现
  两份代码）→ `decrypt_message` 逐字节还原 XML；
- **篡改签名必拒**：`msg_signature` 对不上 ⇒ `InvalidSignatureException`（企微 / 微信
  共用的 -40001 档）；
- **先验签后解密顺序钉**：签名错 ⇒ **不触碰 AES**（把 aes_key 换成坏串让 AES 那步
  必然炸——若实现先解密，异常形状就不同 ⇒ 红）；
- **corp_id 尾巴不匹配必拒**：`InvalidCorpIdException`（corp_id 配错的包天然被拒，
  计划 §1.3 钉住这一点）。

零网络：纯加解密不碰 HTTP。
"""
from __future__ import annotations

from django.test import SimpleTestCase

from tests._wecom_helpers import (
    AES_KEY,
    CORP_ID,
    NONCE,
    TIMESTAMP,
    TOKEN,
    encrypt_message,
    signature,
    text_message_xml,
    wrap_encrypted_body,
)
from wechatpy.exceptions import InvalidSignatureException
from wechatpy.work.crypto import WeChatCrypto
from wechatpy.work.exceptions import InvalidCorpIdException

OTHER_CORP_ID = "wwffffffffffffffff"


def _crypto(corp_id: str = CORP_ID) -> WeChatCrypto:
    return WeChatCrypto(TOKEN, AES_KEY, corp_id)


class RoundTripTests(SimpleTestCase):
    def test_decrypt_restores_the_plaintext_xml(self):
        xml = text_message_xml(from_user="userid-synthetic-a", content="测试内容甲", msg_id="1234567890")
        encrypt = encrypt_message(aes_key=AES_KEY, corp_id=CORP_ID, xml=xml)
        msg_sig = signature([TOKEN, TIMESTAMP, NONCE, encrypt])
        body = wrap_encrypted_body(encrypt)
        plain = _crypto().decrypt_message(body, msg_sig, TIMESTAMP, NONCE)
        self.assertEqual(plain, xml)

    def test_check_signature_roundtrip_returns_decrypted_echostr(self):
        echostr_plain = "echostr-synthetic-5678"
        encrypt = encrypt_message(aes_key=AES_KEY, corp_id=CORP_ID, xml=echostr_plain)
        msg_sig = signature([TOKEN, TIMESTAMP, NONCE, encrypt])
        # 企微 URL 验证：`check_signature` 验签 + 解密一步完成，回**明文** echostr。
        echo = _crypto().check_signature(msg_sig, TIMESTAMP, NONCE, encrypt)
        self.assertEqual(echo, echostr_plain)


class TamperTests(SimpleTestCase):
    def test_wrong_msg_signature_raises_invalid_signature(self):
        xml = text_message_xml(from_user="userid-synthetic-a", content="x", msg_id="1")
        encrypt = encrypt_message(aes_key=AES_KEY, corp_id=CORP_ID, xml=xml)
        wrong_sig = signature([TOKEN, TIMESTAMP, NONCE, "tampered-encrypt"])
        with self.assertRaises(InvalidSignatureException):
            _crypto().decrypt_message(
                wrap_encrypted_body(encrypt), wrong_sig, TIMESTAMP, NONCE
            )

    def test_signature_is_verified_before_aes_touches_the_payload(self):
        """顺序承重钉（计划 §3）：签名错 ⇒ **不触碰 AES**。

        判据形态：把 `encoding_aes_key` 换成一个**合法 43 位 base64 但错误**的串——
        若实现先解密，AES-CBC 解出的尾部 corp_id 一定对不上 ⇒ `InvalidCorpIdException`；
        实现先验签（正确顺序）⇒ 走不到 AES ⇒ `InvalidSignatureException`。
        """
        bad_aes_key = "ZYXWVUTSRQPONMLKJIHGFEDCBAzyxwvutsrqponmlkj"  # 43 位合成串（另一把钥匙）
        crypto = WeChatCrypto(TOKEN, bad_aes_key, CORP_ID)
        xml = text_message_xml(from_user="u", content="x", msg_id="2")
        encrypt = encrypt_message(aes_key=AES_KEY, corp_id=CORP_ID, xml=xml)
        wrong_sig = signature([TOKEN, TIMESTAMP, NONCE, "tampered"])
        with self.assertRaises(InvalidSignatureException):
            crypto.decrypt_message(wrap_encrypted_body(encrypt), wrong_sig, TIMESTAMP, NONCE)

    def test_wrong_nonce_breaks_the_signature(self):
        xml = "<xml/>"
        encrypt = encrypt_message(aes_key=AES_KEY, corp_id=CORP_ID, xml=xml)
        msg_sig = signature([TOKEN, TIMESTAMP, NONCE, encrypt])
        with self.assertRaises(InvalidSignatureException):
            _crypto().decrypt_message(
                wrap_encrypted_body(encrypt), msg_sig, TIMESTAMP, "other-nonce"
            )


class CorpIdTests(SimpleTestCase):
    def test_other_tenant_corp_id_in_payload_is_rejected(self):
        """跨租户第一道闸（结构层）：别的 corp_id 加密的包打到本租户配置上必拒。"""
        xml = text_message_xml(from_user="userid-synthetic-b", content="别家消息", msg_id="3")
        encrypt = encrypt_message(aes_key=AES_KEY, corp_id=OTHER_CORP_ID, xml=xml)
        msg_sig = signature([TOKEN, TIMESTAMP, NONCE, encrypt])
        with self.assertRaises(InvalidCorpIdException):
            _crypto().decrypt_message(wrap_encrypted_body(encrypt), msg_sig, TIMESTAMP, NONCE)

    def test_other_tenant_token_breaks_the_signature_first(self):
        """别的租户用**它自己的 token** 算的签名：连验签都过不了（token 也是租户内值）。"""
        OTHER_TOKEN = "wecom-token-synthetic-ffffffff"
        xml = text_message_xml(from_user="u", content="x", msg_id="4")
        encrypt = encrypt_message(aes_key=AES_KEY, corp_id=OTHER_CORP_ID, xml=xml)
        msg_sig = signature([OTHER_TOKEN, TIMESTAMP, NONCE, encrypt])
        with self.assertRaises(InvalidSignatureException):
            _crypto().decrypt_message(wrap_encrypted_body(encrypt), msg_sig, TIMESTAMP, NONCE)
