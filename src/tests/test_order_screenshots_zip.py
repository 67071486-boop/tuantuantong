"""打包下载的用例（spec §7 / §11）。

⚠️ 断言打在**真实出口**上：解出来的 zip **条目名与内容**，⛔ 不只断言状态码。
"""
import io
import zipfile
from urllib.parse import quote
from decimal import Decimal

from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order, OrderScreenshot, OrderStatus
from apps.core.storage import get_storage
from tests.p5_auth_helpers import make_logged_in_client

ZIP_NODE = "order.download_screenshots_zip"
UPLOAD_NODE = "order.upload_screenshot"


def _client(username, group, *nodes):
    """⚠️ 每个 client 用**不同的组**：节点挂在组对应的 `Role` 上，同组会互相继承（见 Task 3 的用例说明）。"""
    return make_logged_in_client(username=username, groups=(group,), nodes=nodes)


class ScreenshotZipTests(TenantTestCase):
    PNG = b"\x89PNG\r\n\x1a\n" + b"png-body"
    JPEG = b"\xff\xd8\xff" + b"jpeg-body"

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260925000000000303",
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("88.00"),
        )
        self.client = _client("zip-admin", "系统管理员", ZIP_NODE, UPLOAD_NODE)
        self.url = f"/api/business/orders/{self.order.order_no}/screenshots/zip"
        self.storage = get_storage()

    def _ready_row(self, name, body, *, order=None):
        storage = self.storage
        key = f"{self.order.order_no}/zip-{name}"
        storage.put_bytes(key, body, content_type="image/jpeg")
        return OrderScreenshot.objects.create(
            order=order or self.order, object_key=key,
            original_name=name, size_bytes=len(body),
            status=OrderScreenshot.STATUS_READY,
            uploaded_by_id=self._user_id(),
        )

    def _user_id(self):
        from django.contrib.auth.models import User

        return User.objects.get(username="zip-admin").pk

    def test_zip_contains_every_ready_image_with_its_bytes(self):
        self._ready_row("a.png", self.PNG)
        self._ready_row("b.jpg", self.JPEG)

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/zip")

        payload = b"".join(resp.streaming_content)
        zf = zipfile.ZipFile(io.BytesIO(payload))
        self.assertEqual(sorted(zf.namelist()), ["001_a.png", "002_b.jpg"])
        # 内容逐字节对上（不是只看条目名）
        self.assertEqual(zf.read("001_a.png"), self.PNG)
        self.assertEqual(zf.read("002_b.jpg"), self.JPEG)

    def test_pending_rows_are_not_packed(self):
        self._ready_row("ready.jpg", self.JPEG)
        pending_key = f"{self.order.order_no}/zip-pending.jpg"
        self.storage.put_bytes(pending_key, self.JPEG, content_type="image/jpeg")
        OrderScreenshot.objects.create(
            order=self.order, object_key=pending_key, uploaded_by_id=self._user_id(),
        )

        payload = b"".join(self.client.get(self.url).streaming_content)
        self.assertEqual(zipfile.ZipFile(io.BytesIO(payload)).namelist(), ["001_ready.jpg"])

    def test_empty_order_returns_envelope_1000_而不是空_zip(self):
        resp = self.client.get(self.url)
        # ⚠️ 本仓业务失败 = HTTP 200 + 业务码（⛔ 不是 404/400）
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["code"], 1000)

    def test_zip_node_is_required(self):
        self._ready_row("a.jpg", self.JPEG)
        client = _client("zip-nobody", "财务", UPLOAD_NODE)   # 有上传节点、没有打包节点
        self.assertEqual(client.get(self.url).status_code, 403)


class ZipFilenameSourceFirstTests(ScreenshotZipTests):
    """⭐ D-0301（2026-10-24）：zip 落盘文件名用**展示单号**（来源号非空取它、空回退
    内置号），且来源号是自由文本 ⇒ 出站前清洗（只留 `\\w` / `.` / `-`、截 64）。"""

    def test_filename_uses_a_sanitized_source_no(self):
        self._ready_row("a.jpg", self.JPEG)
        Order.objects.filter(pk=self.order.pk).update(source_order_no="TB/合成 88:最终?")
        self.order.refresh_from_db()
        resp = self.client.get(self.url)
        disposition = resp["Content-Disposition"]
        # ⚠️ 逐字断言清洗结果：斜杠/空格/冒号/问号被剥掉，其余保留。
        # ⚠️ 中文文件名走 **RFC 5987 `filename*`**（Django 对非 ASCII 的裸
        # `filename="…"` 会整头 MIME 编码、浏览器落盘成乱码——见实现处注释）。
        self.assertEqual(
            disposition,
            "attachment; filename*=utf-8''" + quote("TB合成88最终.zip"),
        )

    def test_ascii_source_keeps_the_plain_filename_shape(self):
        """纯 ASCII 来源号照旧走 `filename="…"` 的老形状（与改前逐字同形）。"""
        self._ready_row("a.jpg", self.JPEG)
        Order.objects.filter(pk=self.order.pk).update(source_order_no="SRC-TEST-1")
        resp = self.client.get(self.url)
        self.assertEqual(
            resp["Content-Disposition"], 'attachment; filename="SRC-TEST-1.zip"'
        )

    def test_blank_source_falls_back_to_the_system_no(self):
        self._ready_row("a.jpg", self.JPEG)
        Order.objects.filter(pk=self.order.pk).update(source_order_no="")
        resp = self.client.get(self.url)
        self.assertEqual(
            resp["Content-Disposition"],
            f'attachment; filename="{self.order.order_no}.zip"',
        )

    def test_an_all_special_source_still_yields_a_filename(self):
        """极端：来源号清洗后为空 ⇒ 回退内置号（文件名永远非空）。"""
        self._ready_row("a.jpg", self.JPEG)
        Order.objects.filter(pk=self.order.pk).update(source_order_no="///   ")
        resp = self.client.get(self.url)
        self.assertEqual(
            resp["Content-Disposition"],
            f'attachment; filename="{self.order.order_no}.zip"',
        )

    def test_an_overlong_source_is_truncated_to_64_chars(self):
        self._ready_row("a.jpg", self.JPEG)
        Order.objects.filter(pk=self.order.pk).update(source_order_no="S" * 100)
        resp = self.client.get(self.url)
        self.assertIn("S" * 64, resp["Content-Disposition"])
        self.assertNotIn("S" * 65, resp["Content-Disposition"])
