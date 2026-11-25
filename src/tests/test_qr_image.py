"""二维码出图模块（`apps/accounts/qr.py`，2026-10-20）。

端点级判据在 `tests/test_app_deposit.py`；本文件只钉**图本身**：裸 base64 / PNG 魔数 /
约 300px 的方形边长 / 长链接不炸。⚠️ 这些判据**不**证明「扫得出钱」——那要在真机上扫一次
（本仓没有扫码设备，如实登记）。
"""
import base64
import io

from django.test import SimpleTestCase
from PIL import Image

from apps.accounts.qr import qr_png_base64


class QrPngTests(SimpleTestCase):
    def test_returns_a_bare_base64_png(self):
        """契约：**裸** base64（⛔ 不带 `data:image/png;base64,` 前缀），解码后是 PNG。"""
        encoded = qr_png_base64("https://qr.alipay.com/bax00123abc")
        self.assertFalse(encoded.startswith("data:"), encoded[:30])
        raw = base64.b64decode(encoded, validate=True)
        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"), raw[:8])

    def test_image_is_square_and_near_the_300px_target(self):
        """边长目标 300、白边 2 模块、box_size 取整 ⇒ 落在 250–330 之间（且是正方形）。"""
        raw = base64.b64decode(
            qr_png_base64("https://qr.alipay.com/bax00123abc"), validate=True
        )
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
        self.assertEqual(width, height)
        self.assertGreaterEqual(width, 250, width)
        self.assertLessEqual(width, 330, width)

    def test_a_longer_payload_still_produces_a_valid_png(self):
        """长链接会升 QR 版本（模块更多） ⇒ `box_size` 反算后仍必须出合法 PNG，⛔ 不抛。"""
        long_text = "https://qr.alipay.com/" + "a" * 500
        raw = base64.b64decode(qr_png_base64(long_text), validate=True)
        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"), raw[:8])
        with Image.open(io.BytesIO(raw)) as image:
            self.assertGreater(image.size[0], 0)
