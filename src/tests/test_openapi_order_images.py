"""开放平台 v2 **进度图取票 + 认领**两条写端点的判据（设计文档 §6.1 / §6.3 / §13.10-3）。
十组判据（取票 / 认领 / 数量闸与拒绝分支 / 幂等四态 / `img_type` 分叉 / 会话与跨租户 / 畸形入参 / 领域函数直判 / 内网对照 / `WecomLockedAppImgTypeTests` 锁定应用）与结构性判据（`RegistryTests`：两条 `POST` 被分类登记（`DELIBERATELY_NOT_LOGGED`，⛔ 不留「既不在只读表也不在写表」的空洞）、`images/` 路径唯一 pattern 且两个处理器各住该住的模块、⭐ 两侧调**同一个函数对象**（身份判据）+ 对外端点**确实经过**业务层函数（打桩判据））见各用例 docstring。
夹具纪律：凭据 / 订单号 / 对象名一律**合成值**（`AGENTS.md` §5-C）；`MEDIA_ROOT` 指临时目录（`_TempMediaRootMixin`，⛔ 不写本机 `.tools/media`）；跨域名请求结束后连接停在对方租户的 schema 上 ⇒ 每个发请求的辅助函数用 `try/finally` 钉回租户 A；⚠️ 判据钉字面值（`AGENTS.md` §5-E / E-0024）：键集、业务码、文案都写字面量，⛔ 不从实现模块 import（「改实现 + 改常量」一起改就不会红）。"""
from __future__ import annotations

import json
import pathlib
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderScreenshot,
    OrderStatus,
    PeerDownstreamBinding,
    PeerSyncEvent,
    Performance,
)
from apps.business.peer_sync import hash_token, new_token
from apps.core.storage import get_storage
from apps.openapi import tokens
from apps.openapi.models import ApiApp, ApiAppKind, ApiAppState, CallbackState, OpenApiDelivery
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

# ── 路径（与 `apps/openapi/urls.py` 的 pattern 逐字一致；⚠️ 都带尾斜杠）──────────────
TICKET_TEMPLATE = "/api/open/v1/orders/{order_no}/images/ticket/"
CLAIM_TEMPLATE = "/api/open/v1/orders/{order_no}/images/"
#: 内部那两条（**对照用**；⚠️ 不带尾斜杠，与 `apps/business/urls.py` 一致）。
INTERNAL_TICKET_TEMPLATE = "/api/business/orders/{order_no}/screenshots/ticket"
INTERNAL_CLAIM_TEMPLATE = "/api/business/orders/{order_no}/screenshots"
#: 会话认证的对照端点（本文件用它证明"会话本身是好的"）。
SESSION_CONTROL_PATH = "/api/auth/me"

# ── 业务码 / 状态码（**字面值**，⛔ 不 import 实现常量）───────────────────────────
OK = 0
VALIDATION = 1000
BUSINESS = 1002
UNAUTHORIZED = 401
FORBIDDEN = 403
NOT_FOUND_STATUS = 404

# ── 载荷键集（**写死的字面量**；改实现或改常量都不会让本文件静默变绿）────────────────
ENVELOPE_KEYS = {"code", "msg", "data"}
TICKET_DATA_KEYS = {"key", "upload_url", "expires_in"}
IMAGE_ROW_KEYS = {
    "img_type",
    "url",
    "original_name",
    "size_bytes",
    "remark",
    "uploaded_by_name",
    "created_at",
}

# ── 文案（**对外契约的一部分**）────────────────────────────────────────────────
LIMIT_MSG = "单个订单最多上传20张截图，请先删除后再上传"
BAD_EXT_MSG = "只支持 jpg / jpeg / png"
IMG_TYPE_MSG = "本接口只支持订单进度图（img_type=0）"
#: ⭐ **锁定应用**（`kind=wecom_group_qr`）那一档的新文案（设计文档 §7.2 逐字；
#: ⚠️ 写成本文件的字面量：`AGENTS.md` §5-E / E-0024 —— 从实现模块 import 的话
#: "改实现 + 改常量"一起改就不会红）。
WECOM_QR_IMG_TYPE_MSG = "本应用只能上传群聊二维码（img_type=1）"
NO_PENDING_MSG = "没有待认领的上传记录"
EMPTY_MSG = "图片无效或为空"
TOO_BIG_MSG = "图片超过 18 MB"
NOT_UPLOADED_MSG = "文件还没上传成功"
MISSING_IDEM_MSG = "缺少幂等键"

# ── 夹具用的合成值 ────────────────────────────────────────────────────────────
APP_KEY = "probeimageskey00000001"
APP_SECRET = "probe-images-secret-000000000001"
APP_KEY_B = "probeimageskey00000002"
APP_SECRET_B = "probe-images-secret-000000000002"
#: ⭐ **锁定应用**（`kind=wecom_group_qr`）的凭据（设计文档 §7.2 / D6 / D2：一租户恰好一把）。
APP_KEY_LOCKED = "probelockedkey00000001"
APP_SECRET_LOCKED = "probe-locked-secret-00000000001"
DOMAIN_B = "openapi-images-iso-b.test.local"
SCHEMA_B = "openapi_images_iso_b"
ORDER_NO_A = "20261008000000000201"
ORDER_NO_B = "20261008000000000202"
CALLBACK_URL = "https://callback-images.test.invalid/open/hook"
SOURCE_IP = "203.0.113.61"

#: 一个**真的** JPEG 魔数开头的最小字节串（类型判据是魔数，假的开头会落库失败）。
JPEG = b"\xff\xd8\xff" + b"syn-image-body"
PNG = b"\x89PNG\r\n\x1a\n" + b"syn-image-body"
#: ⛔ **不是**图片的字节（用来打"魔数认不出"那一档）。
NOT_AN_IMAGE = b"this-is-not-an-image"
#: 18 MiB（**字面量**，⛔ 不用 `MAX_IMAGE_BYTES`：两侧同源的话把常量改坏这两条仍绿）。
EIGHTEEN_MIB = 18 * 1024 * 1024
#: 幂等键（合成值；长度落在 8–128 的契约区间里）。
IDEM_TICKET = "probe-ticket-key-0001"
IDEM_CLAIM = "probe-claim-key-0001"

#: 内部端点要的权限节点（与 `screenshot_views.SCREENSHOT_UPLOAD_NODE` 同名同源，
#: ⚠️ 但这里写**字面量**：那是"对外契约"之外的内部节点，写死才能让节点被改坏时红）。
INTERNAL_UPLOAD_NODE = "order.upload_screenshot"


class _ImagesFixture(TenantTestCase):
    """租户 A（`TenantTestCase` 的 schema）+ 租户 B（`SCHEMA_B`）两侧的最小数据（`MEDIA_ROOT` 指临时目录，理由见模块 docstring 的夹具纪律）。"""

    extra_test_tenants = (
        {"schema_name": SCHEMA_B, "name": "开放平台进度图租户 B", "domain": DOMAIN_B},
    )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 里建：schema 的 DDL **不能**回滚在 `TestCase` 的事务里。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="开放平台进度图租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行把连接钉回租户 A（上一条用例的跨域名请求会把连接留在那个租户的 schema 上）。
        connection.set_tenant(self.tenant)
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        patcher.enable()
        self.addCleanup(patcher.disable)

        self.domain_a = self.domain.domain
        self.game_a = Game.objects.create(name="测试进度图游戏甲")
        self.order_a = Order.objects.create(
            game=self.game_a, order_no=ORDER_NO_A, status=OrderStatus.IN_PROGRESS
        )
        #: 应用（对外两条端点用它；⭐ 配上回调地址 + 回调开关 ⇒ 认领时会真的产生推送行）。
        self.app_a = self._create_app(
            name="进度图探针应用甲",
            app_key=APP_KEY,
            app_secret=APP_SECRET,
            callback_url=CALLBACK_URL,
        )
        #: 上传人 = 创建该应用的那个员工（对外路径的"代理上传"口径：
        #: `uploaded_by=ApiApp.created_by`）。⭐ 昵称与登录账号**故意不同** ⇒
        #: "给的是显示名"与"给的是账号名"才是两件事。
        self.uploader = self.app_a.created_by
        self.uploader.first_name = "测试进度图上传昵称"
        self.uploader.save(update_fields=["first_name"])
        #: ⭐ **下游绑定**（认领时的 **peer 链路广播**要有人接才入队：
        #: `enqueue_screenshot_event` 的两个方向各自按绑定是否存在决定）。
        self.taker = User.objects.create_user(username="syn-images-taker")
        Performance.objects.create(
            order=self.order_a, emp_type=EmpType.MAIN, user=self.taker
        )
        self.downstream_binding = PeerDownstreamBinding.objects.create(
            account=self.taker,
            peer_base_url="https://peer-images-down.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        try:
            with tenant_context(self.tenant_b):
                self.game_b = Game.objects.create(name="测试进度图游戏乙")
                self.order_b = Order.objects.create(
                    game=self.game_b, order_no=ORDER_NO_B, status=OrderStatus.IN_PROGRESS
                )
                self.app_b = self._create_app(
                    name="进度图探针应用乙",
                    app_key=APP_KEY_B,
                    app_secret=APP_SECRET_B,
                )
        finally:
            connection.set_tenant(self.tenant)

    # ── 造物 ───────────────────────────────────────────────────────────
    @staticmethod
    def _create_app(*, name, app_key, app_secret, callback_url=""):
        """在**当前 schema** 里建一个应用（`created_by` = 当场建的那位员工）。
        ⚠️ 本线端点**不装权限类、也不做游戏范围过滤**（设计文档 §13.10）⇒ **不挂节点、不勾游戏**（挂了只会让用例看起来在测一件不存在的事）；**不传 `kind`** ⇒ 吃的是生产代码 `ApiApp.kind` 的**列默认值**（`ApiAppKind.GENERAL`）——「没显式设用途的应用仍然是通用应用」正是 `WecomLockedAppImgTypeTests` 那条回归用例钉的事，⛔ 在这里显式写 `kind=GENERAL` 会让那条钉子变成自指。"""
        creator, _ = User.objects.get_or_create(
            username=f"openapi-images-{app_key}",
            defaults={"password": "unused-in-this-test"},
        )
        return ApiApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret=app_secret,
            created_by=creator,
            # ⚠️ 两个回调字段的**默认值**就是"启用"（模型 docstring 的理由），这里只填地址
            #    ⇒ 认领那一刻 `eligible_apps()` 会选中它（判据：推送行数）。
            callback_url=callback_url,
        )

    @staticmethod
    def _create_app_for(*, name, app_key, app_secret, creator, callback_url="", kind):
        """建一个**指定创建人**的应用（「锁定应用」那一组用）。
        ⚠️ 为什么需要它：对外两条端点的行归属判据是 `uploaded_by = request.auth.created_by`（**创建该应用的员工**），而领域层的行选择器 `_pending_row_or_error` 上还有一道 `uploaded_by=user`（只能认领**自己**签发的 pending 行）⇒ **取票与认领必须是同一个员工创建的应用**，否则认领会回「没有待认领的上传记录」；⇒ 锁定应用与 `self.app_a` **同一个创建人**（同一员工可建多把应用、`created_by` 上没有唯一约束——「同一员工创建的多个应用之间不设界」这条边界登记在 `order_image_views` 的模块 docstring 里）。"""
        return ApiApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret=app_secret,
            created_by=creator,
            callback_url=callback_url,
            kind=kind,
        )

    # ── 发请求 ─────────────────────────────────────────────────────────
    def _bearer(
        self,
        method,
        path,
        body=None,
        *,
        key=None,
        app=None,
        domain=None,
        token=None,
        ip=SOURCE_IP,
        data=None,
        **extra,
    ):
        """按 `domain` 发一条带 Bearer 令牌的请求；结束后把连接钉回租户 A。"""
        app = app if app is not None else self.app_a
        domain = self.domain_a if domain is None else domain
        token = tokens.issue(app.app_key, app.app_secret) if token is None else token
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        if key is not None:
            headers["HTTP_IDEMPOTENCY_KEY"] = key
        headers.update(extra)
        client = Client(SERVER_NAME=domain)
        try:
            if method == "post":
                return client.post(
                    path,
                    body if body is not None else {},
                    content_type="application/json",
                    REMOTE_ADDR=ip,
                    **headers,
                )
            return client.get(path, data or {}, REMOTE_ADDR=ip, **headers)
        finally:
            connection.set_tenant(self.tenant)

    def _ticket(self, order_no=ORDER_NO_A, body=None, *, key=IDEM_TICKET, **kwargs):
        payload = {"ext": "jpg"} if body is None else body
        return self._bearer(
            "post", TICKET_TEMPLATE.format(order_no=order_no), payload, key=key, **kwargs
        )

    def _claim(self, object_key, *, order_no=ORDER_NO_A, key=IDEM_CLAIM, body=None, **kwargs):
        payload = {"key": object_key} if body is None else body
        return self._bearer(
            "post", CLAIM_TEMPLATE.format(order_no=order_no), payload, key=key, **kwargs
        )

    # ── 领域小工具 ─────────────────────────────────────────────────────
    def _issue_and_upload(self, *, body=None, order_no=ORDER_NO_A, ext="jpg"):
        """走**取票**端点拿 key，再把字节真放进 storage（本机 = 直接写文件）；返回 `(row, key)`，`row` 从库里重读（⛔ 不信响应）。"""
        response = self._ticket(order_no=order_no, key=f"{IDEM_TICKET}-{order_no}")
        body_data = self.assert_ok(response)
        key = body_data["key"]
        get_storage().put_bytes(key, JPEG if body is None else body, content_type="image/jpeg")
        row = OrderScreenshot.objects.get(object_key=key)
        return row, key

    def _seed_rows(self, count, *, start=0, status=OrderScreenshot.STATUS_READY):
        """给这张单预置 `count` 行订单进度图（直接建行：测的是**数量闸**，⛔ 不是上传链路）。
        ⚠️ `start` 只为**接着上一批往下编号**：`object_key` 上有唯一约束（`business_orderscreenshot_object_key_key`），重号会 `UniqueViolation`。"""
        for index in range(start, start + count):
            OrderScreenshot.objects.create(
                order=self.order_a,
                object_key=f"shared-orders/{ORDER_NO_A}/seed-{index}.jpg",
                uploaded_by=self.uploader,
                img_type=OrderScreenshot.IMG_ORDER,
                status=status,
            )

    # ── 断言小工具 ─────────────────────────────────────────────────────
    def assert_ok(self, response):
        """成功信封：HTTP 200 + `code=0` + **恰好** code/msg/data 三个键。"""
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], OK, body)
        self.assertEqual(set(body), ENVELOPE_KEYS, body)
        return body["data"]

    def assert_failure(self, response, code):
        """业务失败信封：HTTP 200 + 指定业务码（⛔ 不是 4xx/5xx）。"""
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], code, body)
        self.assertEqual(set(body), ENVELOPE_KEYS, body)
        self.assertIsNone(body["data"], body)
        return body


# ═════════════════════════════════════════════════════════════════════════════
# ① 取票
# ═════════════════════════════════════════════════════════════════════════════
class TicketTests(_ImagesFixture):
    def test_the_ticket_lands_a_pending_row_and_returns_exactly_three_keys(self):
        response = self._ticket()
        data = self.assert_ok(response)
        # 键集**恰好**是那三个（⛔ 不回内部主键 `screenshot_id`：认领的入口是 `key`）。
        self.assertEqual(set(data), TICKET_DATA_KEYS, data)
        # ⭐ 判据打在**库里那一行**上（⛔ 不信响应）。
        rows = OrderScreenshot.objects.filter(order=self.order_a)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.status, OrderScreenshot.STATUS_PENDING)
        self.assertEqual(row.img_type, OrderScreenshot.IMG_ORDER)
        self.assertEqual(row.object_key, data["key"])
        # ⭐ 上传人 = **创建该应用的那个员工**（对外路径没有"人"，代理上传口径）。
        self.assertEqual(row.uploaded_by_id, self.app_a.created_by_id)
        # ⭐ key 的形状：共享前缀 + **本单号**分段（P4 的裁决：订单截图按 origin 共享）。
        self.assertTrue(
            data["key"].startswith(f"shared-orders/{ORDER_NO_A}/"), data["key"]
        )
        self.assertTrue(data["key"].endswith(".jpg"), data["key"])

    def test_the_upload_url_really_points_at_that_key_and_accepts_a_put(self):
        """⭐ 「回来的 URL 真的能用」——用**本机替身**端点把字节 PUT 上去。这一条同时压住两件事：
        ① `presign_put` 的 URL 里带的是**那一个** key（⛔ 不是别的 key、也不是一个占位串）；② 本机直传路由（`/api/media/blob/<key>`）对**共享前缀**的订单截图放行（护栏的窄例外：本租户确实持有这张单）。"""
        data = self.assert_ok(self._ticket())
        key = data["key"]
        # 本机后端（`MEDIA_STORAGE_BACKEND=local`）：URL 就是那个替身端点 + 那个 key。
        self.assertEqual(data["upload_url"], f"/api/media/blob/{key}", data)
        session = make_logged_in_client(username="images-blob-uploader")
        put = session.put(data["upload_url"], data=JPEG, content_type="image/jpeg")
        connection.set_tenant(self.tenant)
        self.assertEqual(put.status_code, 200, put.content[:200])
        meta = get_storage().head(key)
        self.assertIsNotNone(meta, "PUT 之后对象应当真的在 storage 里")
        self.assertEqual(meta[0], len(JPEG))

    def test_expires_in_comes_from_the_storage_layer_not_a_hardcoded_number(self):
        """⭐ `expires_in` **从 storage 层取现成值**（设计文档 §13.10-3）。判据分两半，缺一不可（否则「写死 600」也会绿）：
        ① 不打桩时它**等于 storage 层当前的默认值**（本机 `presign_put` 的 600）；② 把 `presign_put` 打桩成**另一个**有效期 ⇒ 回的就是那个值（证明端点读的是 `Presigned.expires_in`，⛔ 不是自己编的数字）。桩打在**被查找的那个模块**上（`apps.business.api.screenshot_views.get_storage`，E-0029）——领域函数在那一模块里查找它。"""
        from unittest import mock

        from apps.core.storage import Presigned

        # ① 不打桩：600 = `LocalStorage.presign_put` 的默认值（**字面量**）。
        self.assertEqual(self.assert_ok(self._ticket())["expires_in"], 600)

        # ② 打桩：换一个**不可能**是默认值的数 ⇒ 只有真的读了 storage 才会跟着变。
        with mock.patch(
            "apps.business.api.screenshot_views.get_storage"
        ) as fake_storage:
            fake_storage.return_value.presign_put.return_value = Presigned(
                url="/api/media/blob/probe-stub-key", expires_in=1234
            )
            data = self.assert_ok(
                self._ticket(key=f"{IDEM_TICKET}-stub", body={"ext": "png"})
            )
        self.assertEqual(data["expires_in"], 1234, data)
        self.assertEqual(data["upload_url"], "/api/media/blob/probe-stub-key", data)
        self.assertTrue(fake_storage.return_value.presign_put.called)

    def test_the_ticket_refuses_a_disallowed_extension_and_writes_nothing(self):
        for ext in ("gif", "exe", ""):
            with self.subTest(ext=ext):
                key = f"{IDEM_TICKET}-ext-{ext or 'blank'}"
                body = self.assert_failure(
                    self._ticket(key=key, body={"ext": ext}), VALIDATION
                )
                # 空串那一档由序列化器挡（`ext 不能为空`），与"表外后缀"是**两句话**
                # ⇒ 只断言"是同一种失败"（`1000`）而不钉同一句文案。
                self.assertTrue(body["msg"], body)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_a).count(), 0)

    def test_img_type_one_is_refused_and_writes_nothing(self):
        """⭐ 差异登记（对标 `UploadImg` 有 `ImgType=1`、**我们对 v1 不做**）。"""
        body = self.assert_failure(
            self._ticket(key=f"{IDEM_TICKET}-imgtype", body={"ext": "jpg", "img_type": 1}),
            VALIDATION,
        )
        self.assertEqual(body["msg"], IMG_TYPE_MSG, body)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_a).count(), 0)
        # ⭐ 同一颗幂等键**可以重试**（非 0 码 ⇒ 占位行跟着回滚，⛔ 不会永久卡"正在处理中"）。
        retry = self.assert_ok(self._ticket(key=f"{IDEM_TICKET}-imgtype"))
        self.assertTrue(retry["key"].startswith(f"shared-orders/{ORDER_NO_A}/"), retry)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_a).count(), 1)


# ═════════════════════════════════════════════════════════════════════════════
# ② 认领
# ═════════════════════════════════════════════════════════════════════════════
class ClaimTests(_ImagesFixture):
    def test_the_claim_lands_a_ready_row_and_returns_the_image_item(self):
        row, key = self._issue_and_upload()
        response = self._claim(
            key, body={"key": key, "original_name": "进度图甲.jpg", "remark": "测试备注甲"}
        )
        data = self.assert_ok(response)
        # 键集**恰好**是列表项那七个（同一个装配出口）。
        self.assertEqual(set(data), IMAGE_ROW_KEYS, data)
        # ⭐ 判据打在**库里那一行**上。
        row.refresh_from_db()
        self.assertEqual(row.status, OrderScreenshot.STATUS_READY)
        self.assertEqual(row.size_bytes, len(JPEG))
        self.assertEqual(row.content_type, "image/jpeg")   # **魔数**嗅探出来的
        self.assertEqual(row.original_name, "进度图甲.jpg")
        self.assertEqual(row.remark, "测试备注甲")
        self.assertIsNotNone(row.ready_at)
        self.assertIsNotNone(row.uploaded_by_id)
        # 载荷里的值来自那一行（含显示名与一个指向该 key 的短时效读链接）。
        self.assertEqual(data["img_type"], OrderScreenshot.IMG_ORDER)
        self.assertEqual(data["size_bytes"], len(JPEG))
        self.assertEqual(data["remark"], "测试备注甲")
        self.assertEqual(data["uploaded_by_name"], "测试进度图上传昵称")
        self.assertIn(key, data["url"], data)

    def test_the_claim_enqueues_both_broadcasts(self):
        """⭐ 两笔入队各一句（peer 链路广播 + 开放平台推送）——判据打在**行数**上。"""
        _, key = self._issue_and_upload()
        self.assertEqual(PeerSyncEvent.objects.count(), 0)
        self.assertEqual(OpenApiDelivery.objects.count(), 0)
        self.assert_ok(self._claim(key))
        peer_rows = PeerSyncEvent.objects.filter(kind="order.screenshot.add")
        self.assertEqual(peer_rows.count(), 1, list(peer_rows))
        self.assertEqual(peer_rows.get().payload["object_key"], key)
        push_rows = OpenApiDelivery.objects.all()
        self.assertEqual(push_rows.count(), 1, list(push_rows))
        self.assertEqual(push_rows.get().event, "order.image_added")
        self.assertEqual(push_rows.get().app_id, self.app_a.pk)
        self.assertEqual(
            push_rows.get().payload["images"][0]["object_key"], key
        )

    def test_the_returned_item_is_word_for_word_the_list_item(self):
        """⭐ 「同一装配出口」的行为判据：认领回的载荷 == 列表里那一项（逐键相等）。"""
        _, key = self._issue_and_upload()
        claimed = self.assert_ok(self._claim(key))
        listing = self.assert_ok(
            self._bearer("get", CLAIM_TEMPLATE.format(order_no=ORDER_NO_A))
        )
        self.assertEqual(listing["count"], 1, listing)
        self.assertEqual(len(listing["results"]), 1, listing)
        self.assertEqual(set(claimed), set(listing["results"][0]), claimed)
        for name in IMAGE_ROW_KEYS:
            self.assertEqual(claimed[name], listing["results"][0][name], name)

    def test_the_claim_refuses_a_key_that_is_not_pending(self):
        """别人的 key / 不存在的 key ⇒ 与内网同一句话（⛔ 不回一个泄漏存在性的码）。"""
        body = self.assert_failure(self._claim("shared-orders/x/nope.jpg"), VALIDATION)
        self.assertEqual(body["msg"], NO_PENDING_MSG, body)

    def test_the_claim_refuses_an_object_that_was_never_uploaded(self):
        data = self.assert_ok(self._ticket())
        body = self.assert_failure(self._claim(data["key"]), VALIDATION)
        self.assertEqual(body["msg"], NOT_UPLOADED_MSG, body)
        # ⚠️ 这一支**不**删行（对象只是还没传上来）⇒ 那一行还在 `pending`。
        self.assertEqual(
            OrderScreenshot.objects.get(object_key=data["key"]).status,
            OrderScreenshot.STATUS_PENDING,
        )


# ═════════════════════════════════════════════════════════════════════════════
# ③ 闸门：数量闸 + 三条拒绝分支（都要求"那一行被删掉"）
# ═════════════════════════════════════════════════════════════════════════════
class GateTests(_ImagesFixture):
    def test_the_21st_ticket_is_refused_and_writes_no_new_row(self):
        self._seed_rows(20)
        body = self.assert_failure(self._ticket(key=f"{IDEM_TICKET}-limit"), VALIDATION)
        self.assertEqual(body["msg"], LIMIT_MSG, body)
        self.assertEqual(
            OrderScreenshot.objects.filter(
                order=self.order_a, img_type=OrderScreenshot.IMG_ORDER
            ).count(),
            20,
        )

    def test_the_claim_of_the_21st_row_is_refused_and_that_row_is_deleted(self):
        """⭐ 数量闸的**第二个强制点**（认领侧，`img_type=0`）：除本行外已有 20 行 ⇒ 拒 + 删那一行。
        ⚠️ 这一档同时是 `writes_kept` 标记的**靶子**：删行**必须活下来**（若被幂等入口回滚，订单张数会永久停在 21、而列表看不到这一行）；造法：先 19 行 ⇒ 取票（第 20 行）⇒ 再直接补 1 行 ⇒ 此刻「除本行外」正好 20 行。"""
        self._seed_rows(19)
        row, key = self._issue_and_upload()
        self._seed_rows(1, start=19)
        body = self.assert_failure(self._claim(key), VALIDATION)
        self.assertEqual(body["msg"], LIMIT_MSG, body)
        self.assertFalse(OrderScreenshot.objects.filter(pk=row.pk).exists())
        self.assertIsNone(get_storage().head(key))
        # 剩下的仍然只有 20 行（⛔ 不是 21：那一行真的被删掉了）。
        self.assertEqual(
            OrderScreenshot.objects.filter(
                order=self.order_a, img_type=OrderScreenshot.IMG_ORDER
            ).count(),
            20,
        )

    def test_a_zero_byte_object_is_refused_and_the_row_and_object_are_gone(self):
        row, key = self._issue_and_upload(body=b"")
        body = self.assert_failure(self._claim(key), VALIDATION)
        self.assertEqual(body["msg"], EMPTY_MSG, body)
        self.assertFalse(OrderScreenshot.objects.filter(pk=row.pk).exists())
        self.assertIsNone(get_storage().head(key), "被拒的孤立对象也应当删掉")
        # ⭐ 清理**真的留下来了**（不是"行只是看不见了"）：换一颗幂等键再认领 ⇒
        #    已经找不到待认领的行（若那一行被回滚回来，这里会得到「文件还没上传成功」）。
        self.assertEqual(
            self.assert_failure(
                self._claim(key, key=f"{IDEM_CLAIM}-after"), VALIDATION
            )["msg"],
            NO_PENDING_MSG,
        )
        # ⚠️ 如实钉住同一个标记的**代价**：那一档的响应快照被留下了 ⇒ 同一颗键重发
        #    会**回放那次失败**（⛔ 不会重跑清理、也不是"再拒一次"）。
        replay = self.assert_failure(self._claim(key), VALIDATION)
        self.assertEqual(replay["msg"], EMPTY_MSG, replay)

    def test_one_byte_over_18mb_is_refused_and_the_row_and_object_are_gone(self):
        """⭐ **真实边界**（⛔ 不是把上限 mock 成 4）：18 MiB + 1 字节。"""
        row, key = self._issue_and_upload(body=b"\xff\xd8\xff" + b"0" * (EIGHTEEN_MIB - 2))
        body = self.assert_failure(self._claim(key), VALIDATION)
        self.assertEqual(body["msg"], TOO_BIG_MSG, body)
        self.assertFalse(OrderScreenshot.objects.filter(pk=row.pk).exists())
        self.assertIsNone(get_storage().head(key))

    def test_a_non_image_magic_is_refused_and_the_row_and_object_are_gone(self):
        row, key = self._issue_and_upload(body=NOT_AN_IMAGE)
        body = self.assert_failure(self._claim(key), VALIDATION)
        self.assertEqual(body["msg"], BAD_EXT_MSG, body)
        self.assertFalse(OrderScreenshot.objects.filter(pk=row.pk).exists())
        self.assertIsNone(get_storage().head(key))

    def test_a_png_is_accepted_and_its_sniffed_type_is_recorded(self):
        """对照：**换一种合法图片**照常成功（否则上面几条"什么都拒"也能绿）。"""
        row, key = self._issue_and_upload(body=PNG, ext="png")
        self.assert_ok(self._claim(key))
        row.refresh_from_db()
        self.assertEqual(row.status, OrderScreenshot.STATUS_READY)
        self.assertEqual(row.content_type, "image/png")
        self.assertEqual(row.size_bytes, len(PNG))


# ═════════════════════════════════════════════════════════════════════════════
# ④ 幂等（§6.3 硬前置）：同键同体 ⇒ 副作用只发生一次
# ═════════════════════════════════════════════════════════════════════════════
class IdempotencyTests(_ImagesFixture):
    def test_a_missing_key_is_a_parameter_error(self):
        for response in (
            self._ticket(key=None),
            self._claim("shared-orders/x/nope.jpg", key=None),
        ):
            with self.subTest(path=response.request["PATH_INFO"]):
                body = self.assert_failure(response, VALIDATION)
                self.assertEqual(body["msg"], MISSING_IDEM_MSG, body)

    def test_the_ticket_replays_without_issuing_a_second_row(self):
        first = self.assert_ok(self._ticket())
        second = self.assert_ok(self._ticket())
        self.assertEqual(first, second)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_a).count(), 1)

    def test_the_claim_replays_without_a_second_ready_or_a_second_broadcast(self):
        row, key = self._issue_and_upload()
        first = self.assert_ok(self._claim(key))
        second = self.assert_ok(self._claim(key))
        self.assertEqual(first, second)
        self.assertEqual(PeerSyncEvent.objects.filter(kind="order.screenshot.add").count(), 1)
        self.assertEqual(OpenApiDelivery.objects.count(), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, OrderScreenshot.STATUS_READY)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_a).count(), 1)

    def test_the_same_key_with_a_different_body_is_refused(self):
        self.assert_ok(self._ticket())
        body = self.assert_failure(
            self._ticket(key=IDEM_TICKET, body={"ext": "png"}), VALIDATION
        )
        self.assertEqual(body["msg"], "同一个幂等键配了不同的请求体", body)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_a).count(), 1)


# ═════════════════════════════════════════════════════════════════════════════
# ⑤ 认证与租户边界
# ═════════════════════════════════════════════════════════════════════════════
class AuthAndIsolationTests(_ImagesFixture):
    def test_a_session_request_is_refused_by_both_endpoints(self):
        session = make_logged_in_client(username="images-session-probe", is_superuser=True)
        for path in (
            TICKET_TEMPLATE.format(order_no=ORDER_NO_A),
            CLAIM_TEMPLATE.format(order_no=ORDER_NO_A),
        ):
            with self.subTest(path=path):
                response = session.post(path, {"ext": "jpg"}, content_type="application/json")
                connection.set_tenant(self.tenant)
                self.assertEqual(response.status_code, UNAUTHORIZED, response.content[:200])
                self.assertIn("code", response.json())
        # 对照：同一条会话**是好的**（否则上面的 401 可能只是"会话坏了"）。
        control = session.get(SESSION_CONTROL_PATH)
        connection.set_tenant(self.tenant)
        self.assertEqual(control.status_code, 200, control.content[:200])

    def test_tenant_a_token_on_tenant_bs_domain_is_refused(self):
        response = self._bearer(
            "post",
            TICKET_TEMPLATE.format(order_no=ORDER_NO_B),
            {"ext": "jpg"},
            key=f"{IDEM_TICKET}-cross",
            domain=DOMAIN_B,
        )
        self.assertEqual(response.status_code, UNAUTHORIZED, response.content[:200])

    def test_tenant_as_token_cannot_see_tenant_bs_order(self):
        """⭐ 跨租户否定断言**用订单号指认**（⛔ 不用主键：两个 schema 的序列各自从 1 开始）。"""
        body = self.assert_failure(
            self._ticket(order_no=ORDER_NO_B, key=f"{IDEM_TICKET}-b-order"), BUSINESS
        )
        self.assertEqual(body["msg"], "记录不存在或已被删除", body)
        self.assertFalse(
            OrderScreenshot.objects.filter(order__order_no=ORDER_NO_B).exists()
        )

    def test_a_key_that_belongs_to_another_tenant_is_forbidden(self):
        """护栏：对象名不属于本租户 ⇒ **403**，且那一行**原样不动**（⛔ 不是 500、也不是静默删行）。"""
        row, _key = self._issue_and_upload()
        foreign = f"{SCHEMA_B}/orders/{ORDER_NO_B}/abc.jpg"
        OrderScreenshot.objects.filter(pk=row.pk).update(object_key=foreign)
        response = self._claim(foreign, key=f"{IDEM_CLAIM}-foreign")
        self.assertEqual(response.status_code, FORBIDDEN, response.content[:200])
        row.refresh_from_db()
        self.assertEqual(row.object_key, foreign)
        self.assertEqual(row.status, OrderScreenshot.STATUS_PENDING)

    def test_another_employee_cannot_claim_this_apps_pending_row(self):
        """⭐ 行选择器还锁着**上传人**：换一个员工建的应用认领不到这一行。
        ⚠️ 这是「同一员工创建的多个应用之间不设界」的**对照**（那条边界登记在 `order_image_views` 的模块 docstring 里）：**别的员工**确实认领不到。"""
        _, key = self._issue_and_upload()
        other = self._create_app(
            name="进度图探针应用丙", app_key="probeimageskey00000003",
            app_secret="probe-images-secret-000000000003",
        )
        body = self.assert_failure(
            self._claim(key, app=other, key=f"{IDEM_CLAIM}-other-app"), VALIDATION
        )
        self.assertEqual(body["msg"], NO_PENDING_MSG, body)
        self.assertEqual(
            OrderScreenshot.objects.get(object_key=key).status,
            OrderScreenshot.STATUS_PENDING,
        )


# ═════════════════════════════════════════════════════════════════════════════
# ⑥ 畸形入参：**不 500**，逐条写清期望码
# ═════════════════════════════════════════════════════════════════════════════
class MalformedInputTests(_ImagesFixture):
    def test_a_nul_order_no_does_not_match_any_route(self):
        """`%00` 经测试客户端变成**真 NUL** ⇒ 共享转换器不收它 ⇒ **404**（⛔ 不是 500）。"""
        for template in (TICKET_TEMPLATE, CLAIM_TEMPLATE):
            with self.subTest(template=template):
                response = self._bearer(
                    "post",
                    template.format(order_no="%00"),
                    {"ext": "jpg", "key": "shared-orders/x/a.jpg"},
                    key=f"{IDEM_TICKET}-nul",
                )
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                self.assertEqual(response.status_code, NOT_FOUND_STATUS)

    def test_a_long_order_no_is_a_business_answer_not_a_server_error(self):
        """长单号（超过 20 位格式、但路径上仍可存）：取票 ⇒ **1002**；认领 ⇒ **1000**。
        ⚠️ 两档**本来就不同**（取票先按单号取单、认领按 `key` 取行），如实逐条写清——⛔ 不是「随便回什么都行」：都不是 5xx、也都不是 200 假成功。长度取 **150**（「格式上超长、路径上仍装得进幂等台账」那一档）；**更长**的那一档由下一条用例打（撞的是 `@idempotent` 入口的路径长度闸）。"""
        long_no = "9" * 150
        self.assertTrue(len(TICKET_TEMPLATE.format(order_no=long_no)) <= 191, "本档必须可存")
        self.assert_failure(
            self._ticket(order_no=long_no, key=f"{IDEM_TICKET}-long"), BUSINESS
        )
        self.assert_failure(
            self._claim("shared-orders/x/a.jpg", order_no=long_no, key=f"{IDEM_CLAIM}-long"),
            VALIDATION,
        )

    def test_a_path_too_long_for_the_idempotency_ledger_is_a_1000_not_a_500(self):
        """⭐⭐ **超长路径 ⇒ `1000`（⛔ 不是 500）**——一处既有缺陷的判据：`@idempotent` 入口把 `request.path` 原样写进 `OpenApiIdempotency.path`（`varchar`，列宽见 `idempotency.MAX_PATH_CHARS`），而路径里的 `order_no` 来自请求、**没有长度界** ⇒ 超长路径在**占位行 `INSERT`** 那一步撞 `StringDataRightTruncation` ⇒ HTTP 500（值来自请求、进了类型化的列：`AGENTS.md` §5-E / E-0010 那一类；修前实测：路径长 > 191 即 500，波及全部 `@idempotent` 端点）。
        本条用 **300 字符**（远超阈值），逐条钉三件事：① 两个端点都**不许 500**；② 两条都回 **`1000`**——取票那一侧**不是 `1002`**（那道闸排在「按单号取单」**之前** ⇒ 它连一次查询都没发）；③ **一行都不落库**（既没有占位行、也没有 `pending` 行）。"""
        long_no = "9" * 300
        self.assertTrue(len(TICKET_TEMPLATE.format(order_no=long_no)) > 191, "本档必须越界")
        for path, payload, key in (
            (TICKET_TEMPLATE, {"ext": "jpg"}, f"{IDEM_TICKET}-toolong"),
            (CLAIM_TEMPLATE, {"key": "shared-orders/x/a.jpg"}, f"{IDEM_CLAIM}-toolong"),
        ):
            with self.subTest(path=path):
                response = self._bearer(
                    "post", path.format(order_no=long_no), payload, key=key
                )
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                self.assert_failure(response, VALIDATION)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_a).count(), 0)

    def test_the_idempotency_path_gate_follows_the_column_width(self):
        """⭐ 闸与**列宽同源**（⛔ 不硬编码 191）：那个常量就是模型那一列的 `max_length`——挡的是「闸里写死一个数、后来列宽改了闸没跟着改」的静默错位（同族先例：共享订单截图 key 的列宽判据也是从 `_meta` 取的）。"""
        from apps.openapi.idempotency import MAX_PATH_CHARS
        from apps.openapi.models import OpenApiIdempotency

        self.assertEqual(
            MAX_PATH_CHARS, OpenApiIdempotency._meta.get_field("path").max_length
        )
        self.assertTrue(MAX_PATH_CHARS > 0)

    def test_a_missing_or_malformed_body_is_a_parameter_error(self):
        cases = (
            (TICKET_TEMPLATE, {}),                                   # 缺 ext
            (TICKET_TEMPLATE, {"ext": "jpg" * 10}),                  # ext 超长（> 8）
            (TICKET_TEMPLATE, {"ext": "jpg", "img_type": "abc"}),    # img_type 不是整数
            (CLAIM_TEMPLATE, {}),                                    # 缺 key
            (CLAIM_TEMPLATE, {"key": ""}),                           # 空 key
            (CLAIM_TEMPLATE, {"key": "x" * 300}),                    # key 超列宽（255）
            (CLAIM_TEMPLATE, {"key": "a" * 10, "remark": "r" * 80}),  # remark 超 50
            (CLAIM_TEMPLATE, {"key": "a" * 10, "original_name": "n" * 300}),
        )
        for index, (template, payload) in enumerate(cases):
            with self.subTest(payload=str(payload)[:40]):
                response = self._bearer(
                    "post",
                    template.format(order_no=ORDER_NO_A),
                    payload,
                    key=f"{IDEM_TICKET}-malformed-{index}",
                )
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                self.assert_failure(response, VALIDATION)
        # 一条都不该落库。
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_a).count(), 0)

    def test_a_non_object_body_is_a_parameter_error_not_a_500(self):
        """JSON 数组 / 字符串标量 / 数字标量 ⇒ `1000`（序列化器报「要一个对象」），⛔ 不是 500。
        ⚠️ 三种都要发**合法 JSON**（`json.dumps`）：否则拿到的是 DRF 的 `ParseError` ⇒ HTTP **400** + `code=400`（那是**传输层**语义，本仓刻意保留状态码）；本条的关注点是「**解析得出的**非对象体不会 500」。"""
        for body in ([{"ext": "jpg"}], json.dumps("jpg"), json.dumps(7)):
            with self.subTest(body=str(body)[:30]):
                response = self._bearer(
                    "post",
                    TICKET_TEMPLATE.format(order_no=ORDER_NO_A),
                    body,
                    key=f"{IDEM_TICKET}-nonobject",
                )
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                self.assert_failure(response, VALIDATION)

    def test_a_boolean_img_type_is_not_silently_treated_as_zero(self):
        """`img_type: true` ⇒ **1000** + 一行不写（⛔ 不静默当 `0`：`True == 1`，那是二维码那一档）。
        ⚠️ 如实写清它是被哪一道挡下的：DRF 的 `IntegerField` **自己**拒布尔（实测文案「请填写合法的整数值。」，`code=1000`）⇒ 它**根本到不了**视图里 `img_type != 0` 的判据；本条钉的是**结果**（布尔不许被当 `0` 收下、一行不落库），「`1` 走视图判据」那一档由 `TicketTests.test_img_type_one_is_refused_and_writes_nothing` 独立钉住。"""
        body = self.assert_failure(
            self._ticket(
                key=f"{IDEM_TICKET}-bool", body={"ext": "jpg", "img_type": True}
            ),
            VALIDATION,
        )
        self.assertTrue(body["msg"], body)
        self.assertNotEqual(body["msg"], IMG_TYPE_MSG, "布尔应当在序列化器那一层就被挡下")
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_a).count(), 0)


# ═════════════════════════════════════════════════════════════════════════════
# ⑦ 结构网：分类 + 同路径 + **同一份实现**
# ═════════════════════════════════════════════════════════════════════════════
class RegistryTests(_ImagesFixture):
    """照 T5 给 `status/` 补的那条同形：只把路径从只读表里删掉就是**净损失**。"""

    IMAGES_PATH = "api/open/v1/orders/<order_no:order_no>/images/"
    TICKET_PATH = "api/open/v1/orders/<order_no:order_no>/images/ticket/"

    def test_images_is_no_longer_read_only_and_both_posts_are_classified(self):
        from tests.test_operation_log_coverage import MUST_HAVE_NO_WRITE_METHOD
        from tests.test_operation_log_ledger import DELIBERATELY_NOT_LOGGED

        # ① 那条路径**不再**在只读反向钉子里（它现在有 `POST`）。
        self.assertNotIn(self.IMAGES_PATH, MUST_HAVE_NO_WRITE_METHOD)
        # ② 对照：那张表**没有被整张放宽**（既有的只读路径仍在里面）。
        self.assertIn("api/open/v1/ranks/", MUST_HAVE_NO_WRITE_METHOD)
        self.assertIn("api/business/my-orders/", MUST_HAVE_NO_WRITE_METHOD)
        # ③ ⭐ 两条新 `POST` 都被**分类**（⛔ 不落进"既不在只读表、也不在写表"的静默空洞）；
        #    它们与内网那两条 upload 端点**同一条口径**：`spec §9.2` 只给不可恢复的动作留痕。
        self.assertIn(("post", self.TICKET_PATH), DELIBERATELY_NOT_LOGGED)
        self.assertIn(("post", self.IMAGES_PATH), DELIBERATELY_NOT_LOGGED)
        for path in (self.TICKET_PATH, self.IMAGES_PATH):
            self.assertNotIn(("post", path), MUST_HAVE_NO_WRITE_METHOD)

    def test_the_images_route_is_one_pattern_serving_both_handlers(self):
        """⭐ 框架事实：同一条路径在 resolver 树里**只**能有一个 pattern（先匹配先赢）。"""
        from django.urls import get_resolver

        from tests.test_operation_log_coverage import _normalise, _owner_of, _walk

        patterns = [
            (raw, callback)
            for raw, callback in _walk(get_resolver())
            if _normalise(raw) == self.IMAGES_PATH
        ]
        self.assertEqual(len(patterns), 1, patterns)
        cls = patterns[0][1].cls
        self.assertTrue(hasattr(cls, "get") and hasattr(cls, "post"), cls)
        # GET 住 `order_extra_views` 模块、POST 住本线的 `order_image_views` 模块（⛔ 不存在"又注册了一条同 pattern 的路由"）。
        self.assertEqual(
            _owner_of(cls, "get").__module__, "apps.openapi.api.order_extra_views"
        )
        self.assertEqual(
            _owner_of(cls, "post").__module__,
            "apps.openapi.api.order_image_views",
        )

    def test_both_sides_call_the_same_function_object(self):
        """⭐ **同一份实现**的身份判据（⛔ 不是「看起来像」）：对外模块里的那两个名字必须**就是**业务层那两个函数对象（`is`）——有人哪天在 `apps/openapi/**` 里抄一份，这条立刻红（`AGENTS.md` §5-E：同一个事实两套判据必然漂移）。"""
        from apps.business.api import screenshot_views
        from apps.openapi.api import order_image_views

        self.assertIs(order_image_views.issue_screenshot_ticket, screenshot_views.issue_screenshot_ticket)
        self.assertIs(order_image_views.claim_screenshot, screenshot_views.claim_screenshot)

    def test_the_openapi_endpoints_go_through_the_business_domain_functions(self):
        """⭐ 行为面：把业务层那两份实现打桩 ⇒ 对外两条端点**必须**经过它们。
        ⚠️ 桩打在**被查找的那个模块**上（`apps.openapi.api.order_image_views`，E-0029）：对外模块是 `from … import …` 进来的 ⇒ 打业务模块的名字**不会**拦住它（那正是本条要证明的「调用确实经过对外模块自己那个名字」）。"""
        from unittest import mock

        with mock.patch(
            "apps.openapi.api.order_image_views.issue_screenshot_ticket",
            side_effect=RuntimeError("取票走了别的实现"),
        ) as stub:
            with self.assertRaises(RuntimeError):
                self._ticket(key=f"{IDEM_TICKET}-stub-domain")
        self.assertEqual(stub.call_count, 1)

        _, key = self._issue_and_upload()
        with mock.patch(
            "apps.openapi.api.order_image_views.claim_screenshot",
            side_effect=RuntimeError("认领走了别的实现"),
        ) as stub2:
            with self.assertRaises(RuntimeError):
                self._claim(key, key=f"{IDEM_CLAIM}-stub-domain")
        self.assertEqual(stub2.call_count, 1)


# ═════════════════════════════════════════════════════════════════════════════
# ⑧ 领域函数自己的判据（A 段抽取出来的那一份）
# ═════════════════════════════════════════════════════════════════════════════
class DomainFunctionTests(_ImagesFixture):
    def test_the_pending_row_selector_refuses_to_guess(self):
        """⭐ 两个选择器都不给 ⇒ 显式 `ValueError`（⛔ 不静默取「这一单任意一条 pending 行」）。
        ⚠️ 判据**直调**那个模块级函数（抽取后它是对外可达的领域入口）：两个调用方各自只给一个选择器（内网给主键、对外给对象名），而「都不给」是一条**写得出来的误用**——它会变成「随便认领这一单的一张图」；配对照（给任一个选择器时正常工作）⇒ 防「函数压根不工作」那种假绿。"""
        from apps.business.api.screenshot_views import _pending_row_or_error

        row, _key = self._issue_and_upload()
        with self.assertRaises(ValueError):
            _pending_row_or_error(self.order_a.order_no, None, self.uploader)
        self.assertEqual(
            _pending_row_or_error(self.order_a.order_no, row.pk, self.uploader).pk,
            row.pk,
        )
        self.assertEqual(
            _pending_row_or_error(
                self.order_a.order_no, None, self.uploader, object_key=row.object_key
            ).pk,
            row.pk,
        )


# ═════════════════════════════════════════════════════════════════════════════
# ⑨ ⭐ 对照：同一组参数下「内网」与「对外」产生的行关键字段一致
# ═════════════════════════════════════════════════════════════════════════════
class InternalVersusOpenApiParityTests(_ImagesFixture):
    """设计文档 §13.10-3 的判据：抽出领域函数后**两侧行为一致**（⛔ 不是「两边都能跑」）。
    ⚠️ 两侧的**入参形状不同**（内网按 `screenshot_id` 认领、对外按 `key`）是契约差异；本条比的是**产出的行**：`img_type` / `status` / `remark` / `content_type` / `size_bytes` 与 `object_key` 的**形状**（同一段共享前缀 + 本单号 + 同一后缀）。"""

    def _internal_client(self):
        from apps.business.roles import TENANT_ADMIN

        return make_logged_in_client(
            username="images-parity-admin",
            groups=(TENANT_ADMIN,),
            nodes=(INTERNAL_UPLOAD_NODE,),
        )

    def test_the_same_parameters_produce_the_same_row_key_fields(self):
        client = self._internal_client()
        # ── 内网：取票 + 认领 ─────────────────────────────────────────────
        internal_ticket = client.post(
            INTERNAL_TICKET_TEMPLATE.format(order_no=ORDER_NO_A),
            {"ext": "jpg", "img_type": 0},
            content_type="application/json",
        )
        connection.set_tenant(self.tenant)
        self.assertEqual(internal_ticket.data["code"], OK, internal_ticket.data)
        internal_key = internal_ticket.data["data"]["key"]
        get_storage().put_bytes(internal_key, JPEG, content_type="image/jpeg")
        internal_row = OrderScreenshot.objects.get(object_key=internal_key)
        internal_claim = client.post(
            INTERNAL_CLAIM_TEMPLATE.format(order_no=ORDER_NO_A),
            {"screenshot_id": internal_row.pk, "original_name": "进度图甲.jpg",
             "remark": "测试备注甲"},
            content_type="application/json",
        )
        connection.set_tenant(self.tenant)
        self.assertEqual(internal_claim.data["code"], OK, internal_claim.data)
        internal_row.refresh_from_db()

        # ── 对外：取票 + 认领（**同一组参数**：同后缀、同备注、同原始文件名）──
        openapi_key = self.assert_ok(self._ticket(key=f"{IDEM_TICKET}-parity"))["key"]
        get_storage().put_bytes(openapi_key, JPEG, content_type="image/jpeg")
        self.assert_ok(
            self._claim(
                openapi_key,
                key=f"{IDEM_CLAIM}-parity",
                body={"key": openapi_key, "original_name": "进度图甲.jpg",
                      "remark": "测试备注甲"},
            )
        )
        openapi_row = OrderScreenshot.objects.get(object_key=openapi_key)

        # ── 逐字段比（判据钉在**行**上，⛔ 不是响应上）────────────────────
        self.assertEqual(openapi_row.img_type, internal_row.img_type)
        self.assertEqual(openapi_row.status, internal_row.status)
        self.assertEqual(openapi_row.remark, internal_row.remark)
        self.assertEqual(openapi_row.original_name, internal_row.original_name)
        self.assertEqual(openapi_row.content_type, internal_row.content_type)
        self.assertEqual(openapi_row.size_bytes, internal_row.size_bytes)
        self.assertEqual(openapi_row.status, OrderScreenshot.STATUS_READY)
        self.assertEqual(openapi_row.img_type, OrderScreenshot.IMG_ORDER)
        # ── key 的**形状**一致（共享前缀 + 本单号分段 + 同一后缀；随机段当然不同）──
        for key in (internal_key, openapi_key):
            self.assertTrue(key.startswith(f"shared-orders/{ORDER_NO_A}/"), key)
            self.assertTrue(key.endswith(".jpg"), key)
        self.assertEqual(
            internal_key.rsplit("/", 1)[0], openapi_key.rsplit("/", 1)[0]
        )
        # ⭐ 两行各有一次广播（同一份实现：两侧都入队）。
        self.assertEqual(
            PeerSyncEvent.objects.filter(kind="order.screenshot.add").count(), 2
        )


# ═════════════════════════════════════════════════════════════════════════════
# ⑩ ⭐ `img_type` 按**应用用途**分叉（设计文档 §7.2 / D6）—— 锁定应用只收群聊二维码
# ═════════════════════════════════════════════════════════════════════════════
class WecomLockedAppImgTypeTests(_ImagesFixture):
    """`kind=wecom_group_qr`（锁定应用）走与通用应用**相反**的那一档。本组证什么：
    ① 锁定应用取票 `img_type=1` ⇒ `code=0` + 库里**恰好一行** `img_type=1`/`pending`，⭐ 对象名的**形状**对（与订单截图**同一套共享段** = `shared-orders/<origin>/qr.<随机>.jpg`，D-0316 现口径；旧「留在本租户前缀」口径已废除）；② `img_type=0` / **缺省** / 别的整数 ⇒ `1000` + 新文案、**一行不写**（⛔ 缺省**不静默按 1 兜底**：那会让「锁死」变成一句空话）；③ **回归**：通用应用（**不显式设 `kind`** ⇒ 吃生产代码的列默认值）传 `1` ⇒ 仍是 `1000` + 旧文案 `IMAGE_IMG_TYPE_MSG`；④ 20 张闸只数 `img_type=0` ⇒ 群码取票**仍然放行**；⑤ 同单第二次认领 ⇒ 旧的被淘汰（`ready` 的群码**恰好 1 行**）；⑥ ⭐ 群码认领**只**触发 peer 广播、开放平台推送 0 条（判据打在**入队行数**上，D-0316 现口径）；⑦ 锁定应用调 `GET …/images/` ⇒ **两类图都看得到**（§9-12 那条授权的判据）。
    ⛔ 本组**不**覆盖**认证层的放行表**（`apps/openapi/permissions.py`：锁定应用打不在白名单里的对外端点 ⇒ `403`）——那是另一条线的判据，不在本文件里；本文件只管取票/认领这两条端点上 `img_type` 怎么判。"""

    #: 同一单第二张群码用的幂等键（与第一张区分开，否则是幂等回放、压根不会签第二张）。
    SECOND_QR_IDEM = "probe-ticket-qr-second-0001"

    def setUp(self):
        super().setUp()
        #: 本租户那把**锁定应用**（`kind=wecom_group_qr`；一租户一把，D2）。
        #: ⚠️ 创建人与 `self.app_a` **同一个员工**（理由见 `_create_app_for` 的 docstring：
        #: 行选择器要求"签发人 == 认领人"）。
        self.locked_app = self._create_app_for(
            name="企微群码探针锁定应用甲",
            app_key=APP_KEY_LOCKED,
            app_secret=APP_SECRET_LOCKED,
            creator=self.app_a.created_by,
            callback_url=CALLBACK_URL,
            kind=ApiAppKind.WECOM_GROUP_QR,
        )
        #: 对照钉子（本组所有用例的前提）：夹具走的**确实是**生产代码的列默认值。
        self.assertEqual(self.app_a.kind, ApiAppKind.GENERAL, self.app_a.kind)
        self.assertEqual(
            ApiApp.objects.filter(kind=ApiAppKind.WECOM_GROUP_QR).count(),
            1,
            "本租户的锁定应用应当恰好一行",
        )

    # ── 小工具 ─────────────────────────────────────────────────────────
    def _qr_ticket(self, *, img_type=OrderScreenshot.IMG_GROUP_QR, key=None, body=None,
                   order_no=ORDER_NO_A, **kwargs):
        """用**锁定应用**取票；`img_type=None` ⇒ **整个键都不传**（打"缺省"那一档）。"""
        if body is None:
            body = {"ext": "jpg"}
            if img_type is not None:
                body["img_type"] = img_type
        return self._ticket(
            order_no=order_no, body=body, app=self.locked_app,
            key=IDEM_TICKET if key is None else key, **kwargs,
        )

    def _qr_issue_and_upload(self, *, key, order_no=ORDER_NO_A):
        """锁定应用取票 + 把字节真放进 storage；返回 `(row, key)`（`row` 从库里重读）。"""
        data = self.assert_ok(self._qr_ticket(key=key, order_no=order_no))
        object_key = data["key"]
        get_storage().put_bytes(object_key, JPEG, content_type="image/jpeg")
        return OrderScreenshot.objects.get(object_key=object_key), object_key

    def _qr_rows(self, *, status=None):
        rows = OrderScreenshot.objects.filter(
            order=self.order_a, img_type=OrderScreenshot.IMG_GROUP_QR
        )
        if status is not None:
            rows = rows.filter(status=status)
        return rows

    # ── ① 放行 + 对象名形状 ────────────────────────────────────────────
    def test_the_locked_app_ticket_with_img_type_one_is_accepted(self):
        """⭐ 放行；判据打在**库里那一行**与**对象名的形状**上（⛔ 不是"有没有行"）。"""
        response = self._qr_ticket()
        data = self.assert_ok(response)
        # 键集**恰好**那三个（分叉不改载荷形状）。
        self.assertEqual(set(data), TICKET_DATA_KEYS, data)

        rows = OrderScreenshot.objects.filter(order=self.order_a)
        self.assertEqual(rows.count(), 1, list(rows))
        row = rows.get()
        # ⭐ 原样传 `1` 进领域函数（⛔ 没有被翻译成 0、也没有被兜底成别的值）。
        self.assertEqual(row.img_type, OrderScreenshot.IMG_GROUP_QR)
        self.assertEqual(row.status, OrderScreenshot.STATUS_PENDING)
        self.assertEqual(row.uploaded_by_id, self.locked_app.created_by_id)
        self.assertEqual(row.object_key, data["key"])
        self.assertTrue(data["upload_url"].endswith(data["key"]), data)

        # ⭐ **对象名的形状**（D-0316 现口径）：群码与订单截图**同一套共享段** = `shared-orders/<origin>/qr.<随机>.jpg`
        #    （群码随订单同步；旧口径 R-P4-7「留在本租户前缀」已废除）。这一段与内网签发共用**同一个领域函数**
        #    （`order_image_views` 调 `issue_screenshot_ticket`）⇒ 判据必须钉住**共享**前缀。
        self.assertTrue(
            data["key"].startswith(f"shared-orders/{ORDER_NO_A}/qr."), data["key"]
        )
        self.assertRegex(
            data["key"], rf"^shared-orders/{ORDER_NO_A}/qr\.[0-9a-f]{{12}}\.jpg$"
        )

    # ── ② 反向：0 / 缺省 / 其它值 ⇒ 响亮拒绝、一行不写 ────────────────────
    def test_the_locked_app_refuses_img_type_zero_and_the_default(self):
        """⭐ 锁定应用**缺省** `img_type` ⇒ **响亮拒绝**（⛔ 不静默按 `1` 兜底）。"""
        for label, img_type in (("explicit-zero", 0), ("default", None)):
            with self.subTest(img_type=label):
                body = self.assert_failure(
                    self._qr_ticket(img_type=img_type, key=f"{IDEM_TICKET}-locked-{label}"),
                    VALIDATION,
                )
                self.assertEqual(body["msg"], WECOM_QR_IMG_TYPE_MSG, body)
                self.assertEqual(self._qr_rows().count(), 0, "拒了就不许落任何行")
        # ⚠️ 整个库都不许有行（这一单一张图都没有）——⛔ 不是"群码那类没有"。
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_a).count(), 0)
        # 对照：**通用应用**在**同样的入参**下**放行**（同一颗键形状、同一张单）
        # ⇒ 上面那两个 `1000` 是"按应用分叉"的结果，⛔ 不是"这个入参本身非法"。
        data = self.assert_ok(self._ticket(key=f"{IDEM_TICKET}-general-control"))
        self.assertTrue(data["key"].startswith("shared-orders/"), data["key"])
        rows = OrderScreenshot.objects.filter(order=self.order_a)
        self.assertEqual(rows.count(), 1, list(rows))
        self.assertEqual(rows.get().img_type, OrderScreenshot.IMG_ORDER)

    def test_the_locked_app_refuses_every_other_img_type(self):
        """`2` / `-1` ⇒ `1000` + 新文案（与"今天的非法档"同形），**一行不写**。"""
        for img_type in (2, -1):
            with self.subTest(img_type=img_type):
                body = self.assert_failure(
                    self._qr_ticket(img_type=img_type, key=f"{IDEM_TICKET}-other-{img_type}"),
                    VALIDATION,
                )
                self.assertEqual(body["msg"], WECOM_QR_IMG_TYPE_MSG, body)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_a).count(), 0)
        # ⭐ 非整数在**序列化器**那一层就被挡（⛔ 本线不改序列化器）：码仍是 `1000`，
        #    但文案**不是**视图那一句（证明它压根没到视图的判据）。
        malformed = self.assert_failure(
            self._qr_ticket(body={"ext": "jpg", "img_type": "abc"}, key=f"{IDEM_TICKET}-nonint"),
            VALIDATION,
        )
        self.assertNotEqual(malformed["msg"], WECOM_QR_IMG_TYPE_MSG, malformed)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_a).count(), 0)

    # ── ③ 回归：通用应用一字不变 ───────────────────────────────────────
    def test_a_general_app_still_refuses_img_type_one_with_the_old_message(self):
        """⭐ 既有那条钉子（本文件 `TicketTests.test_img_type_one_is_refused_…`）的**对照**：同一个测试进程里两把应用各自走自己那一档 ⇒ 证明分叉是**按应用**分的，⛔ 不是「整个端点被放松了」。
        ⚠️ `self.app_a` **没有**显式设 `kind`（吃生产代码的列默认值 `general`）——改了那个默认值这条会红。"""
        body = self.assert_failure(
            self._ticket(key=f"{IDEM_TICKET}-general-imgtype",
                         body={"ext": "jpg", "img_type": 1}),
            VALIDATION,
        )
        self.assertEqual(body["msg"], IMG_TYPE_MSG, body)
        self.assertNotEqual(body["msg"], WECOM_QR_IMG_TYPE_MSG, body)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_a).count(), 0)
        # 对照：**锁定应用**在**同一颗**请求形状下放行（同进程、同一张单、同一颗库）。
        self.assert_ok(self._qr_ticket(key=f"{IDEM_TICKET}-locked-imgtype"))
        rows = OrderScreenshot.objects.filter(order=self.order_a)
        self.assertEqual(rows.count(), 1, list(rows))
        self.assertEqual(rows.get().img_type, OrderScreenshot.IMG_GROUP_QR)

    # ── ④ 20 张闸只数 0 类 ─────────────────────────────────────────────
    def test_the_20_row_gate_does_not_count_group_qr_codes(self):
        """⭐ 20 张订单进度图造满之后，群码取票**仍然放行**（群码不占名额，§2.3 / §9-13）。
        ⚠️ 对照钉在同一处：同一时刻**通用应用**取票会被那道闸拒 ⇒ 证明这 20 张**确实**把闸压满了（⛔ 不是「闸压根没生效」那种假绿）。"""
        self._seed_rows(20)
        limit = self.assert_failure(self._ticket(key=f"{IDEM_TICKET}-gate-control"), VALIDATION)
        self.assertEqual(limit["msg"], LIMIT_MSG, limit)

        data = self.assert_ok(self._qr_ticket(key=f"{IDEM_TICKET}-gate-qr"))
        # ⭐ D-0316：群码也走共享前缀（这一条只判"闸没数它"，前缀断言与①那条同源）。
        self.assertTrue(
            data["key"].startswith(f"shared-orders/{ORDER_NO_A}/qr."), data
        )
        self.assertEqual(self._qr_rows().count(), 1)
        # 那 20 张订单进度图**原样不动**（群码没有挤掉任何一张）。
        self.assertEqual(
            OrderScreenshot.objects.filter(
                order=self.order_a, img_type=OrderScreenshot.IMG_ORDER
            ).count(),
            20,
        )

    # ── ⑤ 认领：成功 + 第二张淘汰第一张 ───────────────────────────────
    def test_claiming_the_group_qr_ticket_succeeds(self):
        row, key = self._qr_issue_and_upload(key=f"{IDEM_TICKET}-claim-qr")
        data = self.assert_ok(self._claim(key, key=f"{IDEM_CLAIM}-qr"))
        self.assertEqual(set(data), IMAGE_ROW_KEYS, data)
        row.refresh_from_db()
        self.assertEqual(row.status, OrderScreenshot.STATUS_READY)
        self.assertEqual(row.img_type, OrderScreenshot.IMG_GROUP_QR)
        self.assertEqual(row.content_type, "image/jpeg")
        self.assertEqual(row.size_bytes, len(JPEG))
        self.assertIsNotNone(row.ready_at)
        # 载荷里的图种**就是 1**（列表项那个出口照给，⛔ 没有"对外只会有 0"这回事了）。
        self.assertEqual(data["img_type"], OrderScreenshot.IMG_GROUP_QR)

    def test_a_second_group_qr_claim_evicts_the_first(self):
        """⭐ 「每单最多 1 张群码」：第二张认领成功之后，`ready` 的群码**恰好 1 行**。
        ⚠️ 淘汰只删 **`ready`** 的旧行（领域层的事实）：第一张若还在 `pending`，它**不会**被删——本条的第一张**已经认领过**，所以必然被淘汰。"""
        first, first_key = self._qr_issue_and_upload(key=f"{IDEM_TICKET}-qr-1")
        self.assert_ok(self._claim(first_key, key=f"{IDEM_CLAIM}-qr-1"))
        first.refresh_from_db()
        self.assertEqual(first.status, OrderScreenshot.STATUS_READY)

        second, second_key = self._qr_issue_and_upload(
            key=self.SECOND_QR_IDEM
        )
        self.assertNotEqual(first_key, second_key)
        self.assert_ok(self._claim(second_key, key=f"{IDEM_CLAIM}-qr-2"))
        second.refresh_from_db()
        self.assertEqual(second.status, OrderScreenshot.STATUS_READY)

        ready = self._qr_rows(status=OrderScreenshot.STATUS_READY)
        self.assertEqual(ready.count(), 1, list(ready))
        self.assertEqual(ready.get().object_key, second_key)
        # 旧行**真的没了**（⛔ 不是"看不见了"）；但⭐ D-0316：旧群码已迁到**共享前缀**，
        # 覆盖只删行、**不删字节**（链上别的租户可能还在用）。
        self.assertFalse(OrderScreenshot.objects.filter(pk=first.pk).exists())
        self.assertIsNotNone(
            get_storage().head(first_key), "共享前缀的旧群码字节不许被覆盖动作删掉"
        )

    # ── ⑥ 群码认领：peer 广播入队、开放平台推送仍 0 条 ─────────────────
    def test_claiming_a_group_qr_enqueues_only_the_peer_broadcast(self):
        """⭐ 判据打在**入队行数**上（⛔ 不是「没报错」）：群码认领**只**触发 peer 广播——D-0316 现口径：群码随订单同步 ⇒ `enqueue_screenshot_event` 那一句对 `img_type=1` **必须**执行；而开放平台推送 / 打手回调那两句仍是「订单进度图」的语义 ⇒ 群码**仍然 0 条**推送（⛔ 别把两件事混成一条判据）。
        ⚠️ 两半都要钉（否则「认领压根失败」也会绿）：① 认领**成功了**（业务码 `0` + 那一行真的变 `ready`）；② **通用应用**认领一张订单进度图时**两条链路都**入队 ⇒ 证明下面那个「开放平台 0 条」不是「链路整个坏了」。"""
        row, key = self._qr_issue_and_upload(key=f"{IDEM_TICKET}-qr-peer")
        self.assertEqual(PeerSyncEvent.objects.count(), 0)
        self.assertEqual(OpenApiDelivery.objects.count(), 0)
        self.assert_ok(self._claim(key, key=f"{IDEM_CLAIM}-qr-peer"))
        row.refresh_from_db()
        self.assertEqual(row.status, OrderScreenshot.STATUS_READY)
        # ① 群码：peer **恰一条**（本夹具只有下游绑定）、载荷 `img_type=1`、key = 那一行
        #    （口径照 `test_peer_p4_shared_media.py` 的群码认领用例）。
        peer_rows = PeerSyncEvent.objects.filter(
            kind__in=("order.screenshot.add", "order.screenshot.delete")
        )
        self.assertEqual(peer_rows.count(), 1, list(peer_rows))
        event = peer_rows.get()
        self.assertEqual(event.kind, "order.screenshot.add")
        self.assertEqual(event.binding_role, "downstream")
        self.assertEqual(event.payload["object_key"], key)
        self.assertEqual(event.payload["img_type"], OrderScreenshot.IMG_GROUP_QR)
        # ⛔ 开放平台推送仍是 0 条：群码没有"订单进度图"那个第三方语义。
        self.assertEqual(OpenApiDelivery.objects.count(), 0, list(OpenApiDelivery.objects.all()))
        # ② 对照：**通用应用**认领一张订单进度图 ⇒ 两条链路**各**入队一行
        #    （证明上面那个"0"不是"推送整个坏了"那种假绿）。
        _, order_key = self._issue_and_upload()
        self.assert_ok(self._claim(order_key, key=f"{IDEM_CLAIM}-order-control"))
        # ⚠️ 计数按**增量**：群码那一条也是 `order.screenshot.add` ⇒ 现在共 2 条。
        self.assertEqual(
            PeerSyncEvent.objects.filter(kind="order.screenshot.add").count(), 2,
            list(PeerSyncEvent.objects.all()),
        )
        push_rows = OpenApiDelivery.objects.all()
        # ⚠️ 推送是**逐应用**入队的（`dispatch._enqueue` 对 `eligible_apps()` 每一把各写一行）
        #    ⇒ 本组有**两把**配了回调的应用（通用 `app_a` + 锁定 `locked_app`）⇒ 两行。
        #    ⛔ 这条**不**断言"恰好 1 行"：那会把"推送只发给一把应用"这条不存在的契约钉进去。
        self.assertEqual(push_rows.count(), 2, list(push_rows))
        self.assertEqual(
            {row.app_id for row in push_rows},
            {self.app_a.pk, self.locked_app.pk},
            list(push_rows),
        )
        for row in push_rows:
            self.assertEqual(row.event, "order.image_added")
            # ⭐ 推送说的是**订单进度图**（`img_type=0`）——群码那一行**没有**产生任何推送
            #    （上面那个"0"就是它没产生：开放平台推送与 peer 广播同住领域层那**一个** `if`）。
            self.assertEqual(row.payload["images"][0]["img_type"], 0)
        self.assertEqual(
            {row.payload["images"][0]["object_key"] for row in push_rows},
            {order_key},
            list(push_rows),
        )

    # ── ⑦ 列表：两类图都看得到（§9-12 的授权） ────────────────────────
    def test_the_locked_app_sees_both_image_types_in_the_list(self):
        """⭐ 授权如实钉在判据上：白名单放行 `GET …/images/` ⇒ 锁定应用读得到**两类**图。
        ⚠️ 造法：**通用应用**传一张订单进度图（`img_type=0`）、**锁定应用**传一张群码（`img_type=1`），两条都认领 ⇒ 列表里应当恰好两项、两类各一。⛔ 这一条**不是**在说「两类图都归锁定应用」：它只读这张单的图，写入面仍被 §7.2 分叉锁着。"""
        _, order_key = self._issue_and_upload()
        self.assert_ok(self._claim(order_key, key=f"{IDEM_CLAIM}-list-order"))
        _, qr_key = self._qr_issue_and_upload(key=f"{IDEM_TICKET}-list-qr")
        self.assert_ok(self._claim(qr_key, key=f"{IDEM_CLAIM}-list-qr"))

        listing = self.assert_ok(
            self._bearer(
                "get",
                CLAIM_TEMPLATE.format(order_no=ORDER_NO_A),
                app=self.locked_app,
            )
        )
        self.assertEqual(listing["count"], 2, listing)
        self.assertEqual(len(listing["results"]), 2, listing)
        for item in listing["results"]:
            self.assertEqual(set(item), IMAGE_ROW_KEYS, item)
            # ⭐ 判据取自**调用方真正拿到的载荷**（E-0176）：`url` 里的对象名。
            #    ⚠️ 两条 key 形状不同（`shared-orders/…` 与 `<租户>/orders/…`），
            #    但**都以文件名结尾** ⇒ 两侧用**同一套归一化**（取最后一段）再比。
            self.assertTrue(item["url"].rsplit("/", 1)[-1].endswith(".jpg"), item)
        names = {item["url"].rsplit("/", 1)[-1] for item in listing["results"]}
        self.assertEqual(
            names, {order_key.rsplit("/", 1)[-1], qr_key.rsplit("/", 1)[-1]}, listing
        )
        self.assertEqual(
            sorted(item["img_type"] for item in listing["results"]),
            [OrderScreenshot.IMG_ORDER, OrderScreenshot.IMG_GROUP_QR],
            listing,
        )
