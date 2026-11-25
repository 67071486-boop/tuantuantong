"""接手后自动发群二维码（T12）：代练通 OSS 上传 + 留言 + 进度 + 自证回读。

## 覆盖什么

| 组 | 判据 |
|---|---|
| 客户端签名 | OSS V1 的 canonical string **逐行**钉住 + 一条固定输入的 HMAC-SHA1 向量（独立实现算出的期望值，见用例注释） |
| 客户端上传 | `MockTransport` 收请求：目标 URL / 方法 / 三个头 / body 字节；签名可按文档格式**独立重算** |
| 客户端凭据 | 轮换的 STS 三件套**不进** `repr` / 异常文案 / 日志（含上游错误 XML 里回显它们的场景） |
| 服务层 | 开关=0 零调用（总闸）；无内容跳过留原因；正常路径落 `qr_sent_at`；⛔ **`HomeQrCode` 不再兜底**；图源顺序（本单群码 > 规则图片）；留言只来自规则；上传失败 / 进度没出现不置 `qr_sent_at`；已发过不重发 |
| 触发点 | `dailiantong_sync` 每轮对 `remote_status=12` 且 `qr_sent_at is null` 的推送**至多调一次**；开关=0 / 已发过 / 还没到 12 都不调 |

## 不访问真实网络

- 代练通 API：`DailiantongClient(transport=httpx.MockTransport(...))`，服务层的客户端
  构造点是 `apps.business.dailiantong_qr._open_client`（它委托抛单模块的同一个构造点）。
- 阿里云 OSS：`put_object(..., transport=httpx.MockTransport(...))` 或把
  `apps.business.dailiantong_qr.put_object` 换成走同一个真函数 + 假传输的包装
  （⛔ 不重新实现签名）。

## 夹具纪律

凭据 / 单号 / STS token / 对象名全是**合成值**（不是任何真实 AppSecret / 订单号）；
`MEDIA_ROOT` 指到临时目录（不往本机 `.tools/media` 写文件）。
"""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
import hmac
import io
import os
import pathlib
import re
import shutil
import tempfile
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qsl

import httpx
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import dailiantong_qr as qr_module
from apps.business.dailiantong_client import (
    DailiantongClient,
    DailiantongConfig,
    DailiantongError,
    ListPage,
    OssStsCredentials,
    OssUploadError,
    oss_string_to_sign,
    put_object,
    sign_oss_v1,
)
from apps.business.models import (
    DailiantongPush,
    Game,
    GameServer,
    HomeQrCode,
    Order,
    OrderScreenshot,
    OrderStatus,
    PlatformAutoReplyRule,
    PlatformIntegration,
    PlatformPushSettings,
)
from apps.core.storage import get_storage

#: 合成凭据（⛔ 不是任何真实密钥）。
APP_ID = "synth-dlt-qr-app-0001"
APP_SECRET = "synth-dlt-qr-secret-7b21"
PAY_PASSWORD_MD5 = "0123456789abcdef0123456789abcdef"

#: 合成上游单号 / 本地单号。
SERIAL = "19000000000000000081"
ORDER_NO = "20261027000000000081"

#: 合成 STS 三件套（⛔ 不是任何真实凭证）。
STS_TOKEN = "synth-sts-token-0001"
STS_AK_ID = "STS.synthAkId0001"
STS_AK_SECRET = "synthStsAccessKeySecret0001"

#: 一张合成 PNG（前 8 字节是 PNG 魔数，服务层据此判图种）。
QR_BYTES = b"\x89PNG\r\n\x1a\nsynth-group-qr-bytes-0001"
FALLBACK_QR_BYTES = b"\x89PNG\r\n\x1a\nsynth-fallback-qr-bytes-0002"
#: 「接单后」规则里的图片（计划 §2.1 的第二顺位；⛔ 与上面两张不同字节，便于判来源）。
RULE_IMAGE_BYTES = b"\x89PNG\r\n\x1a\nsynth-rule-image-bytes-0003"

#: 埋点动作码（与 `apps/audit/actions.py` 同值；这里写死字面量，⛔ 不 import 常量偷懒）。
AUDIT_ACTION = "order.dailiantong_qr"

#: 固定签名向量的输入（⛔ 与任何真实凭证无关）。
VECTOR_SECRET = "synth-oss-secret-0001"
VECTOR_DATE = "Tue, 27 Oct 2026 02:00:00 GMT"
VECTOR_KEY = "tt/20261027000000000001/1700000000000.png"
VECTOR_RESOURCE = f"/dltfile01/{VECTOR_KEY}"
#: 期望签名：用 `openssl dgst -sha1 -hmac …` 对下面这条 canonical string **独立**算出
#: （实现者手算过一次，生成方式与依赖见 `report`；⛔ 不是拿被测函数自己算出来的）。
VECTOR_SIGNATURE = "t1r45HDlZxri7dd2/fFM9mtrw6Q="
VECTOR_STRING_TO_SIGN = (
    "PUT\n"
    "\n"
    "image/png\n"
    f"{VECTOR_DATE}\n"
    f"x-oss-security-token:{STS_TOKEN}\n"
    f"{VECTOR_RESOURCE}"
)

#: `Date` 头的形状（RFC 1123 GMT；阿里云 OSS 要求）。
_HTTP_DATE_RE = re.compile(
    r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT$"
)

OSS_HOST = "dltfile01.oss-cn-hangzhou.aliyuncs.com"


def _sts() -> OssStsCredentials:
    return OssStsCredentials(
        security_token=STS_TOKEN,
        access_key_id=STS_AK_ID,
        access_key_secret=STS_AK_SECRET,
    )


# ── 客户端：签名与上传（不碰数据库）──────────────────────────────────────


class OssSignatureTests(SimpleTestCase):
    """OSS V1 签名的**形状**与一条固定输入向量。"""

    def test_fixed_vector(self):
        """固定输入 → 固定签名。

        依据：阿里云 OSS V1 签名 = `base64(HMAC-SHA1(AccessKeySecret, StringToSign))`，
        StringToSign = `VERB\\nContent-MD5\\nContent-Type\\nDate\\n` +
        `CanonicalizedOSSHeaders` + `CanonicalizedResource`（见模块报告）。
        期望值由 `openssl dgst -sha1 -hmac` 对 `VECTOR_STRING_TO_SIGN` 独立算出。
        """
        actual = sign_oss_v1(
            access_key_secret=VECTOR_SECRET,
            verb="PUT",
            content_md5="",
            content_type="image/png",
            date=VECTOR_DATE,
            oss_headers=(("x-oss-security-token", STS_TOKEN),),
            resource=VECTOR_RESOURCE,
        )
        self.assertEqual(actual, VECTOR_SIGNATURE)

    def test_string_to_sign_is_exactly_the_documented_lines(self):
        actual = oss_string_to_sign(
            verb="PUT",
            content_md5="",
            content_type="image/png",
            date=VECTOR_DATE,
            oss_headers=(("x-oss-security-token", STS_TOKEN),),
            resource=VECTOR_RESOURCE,
        )
        self.assertEqual(actual, VECTOR_STRING_TO_SIGN)

    def test_canonicalized_headers_are_lowercased_and_sorted(self):
        actual = oss_string_to_sign(
            verb="PUT",
            content_md5="",
            content_type="image/png",
            date=VECTOR_DATE,
            oss_headers=(
                ("X-OSS-Meta-Author", "foo@bar.com"),
                ("x-OSS-magic", "abracadabra"),
            ),
            resource=VECTOR_RESOURCE,
        )
        self.assertEqual(
            actual,
            "PUT\n\nimage/png\n"
            f"{VECTOR_DATE}\n"
            "x-oss-magic:abracadabra\n"
            "x-oss-meta-author:foo@bar.com\n"
            f"{VECTOR_RESOURCE}",
        )


class GetStsTests(SimpleTestCase):
    """`GetSTS` 的三件套解析（裸对象信封，⛔ 不是 `Result` 信封）。"""

    def _client(self, handler) -> DailiantongClient:
        return DailiantongClient(
            DailiantongConfig(
                app_id=APP_ID,
                app_secret=APP_SECRET,
                user_id="USR-SYNTH-QR-01",
                pay_password_md5=PAY_PASSWORD_MD5,
            ),
            transport=httpx.MockTransport(handler),
            clock=lambda: 1_790_000_000,
        )

    def test_get_sts_returns_three_pieces(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            form = dict(parse_qsl(request.content.decode("utf-8")))
            seen.append(form)
            return httpx.Response(
                200,
                json={
                    "SecurityToken": STS_TOKEN,
                    "AccessKeyId": STS_AK_ID,
                    "AccessKeySecret": STS_AK_SECRET,
                },
            )

        with self._client(handler) as client:
            sts = client.get_sts()

        self.assertEqual(seen[0]["Action"], "GetSTS")
        self.assertEqual(sts.security_token, STS_TOKEN)
        self.assertEqual(sts.access_key_id, STS_AK_ID)
        self.assertEqual(sts.access_key_secret, STS_AK_SECRET)

    def test_missing_piece_is_a_transport_error_naming_the_field_only(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"SecurityToken": STS_TOKEN, "AccessKeyId": STS_AK_ID},
            )

        with self._client(handler) as client:
            with self.assertRaises(Exception) as caught:
                client.get_sts()

        message = str(caught.exception)
        # ⛔ 只报缺哪个字段，不带任何已拿到的值（凭据红线）。
        self.assertIn("AccessKeySecret", message)
        self.assertNotIn(STS_TOKEN, message)
        self.assertNotIn(STS_AK_ID, message)

    def test_repr_masks_the_three_pieces(self):
        text = repr(_sts())
        self.assertNotIn(STS_TOKEN, text)
        self.assertNotIn(STS_AK_ID, text)
        self.assertNotIn(STS_AK_SECRET, text)


class PutObjectTests(SimpleTestCase):
    """`put_object` 的目标 URL / 头 / body / 签名（⛔ 真实网络）。"""

    def _put(self, handler, **kwargs):
        return put_object(
            _sts(), VECTOR_KEY, QR_BYTES, "image/png",
            transport=httpx.MockTransport(handler), **kwargs,
        )

    def test_url_method_headers_and_body(self):
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = request.content
            return httpx.Response(200, headers={"ETag": '"synth-etag"'})

        self._put(handler)

        self.assertEqual(captured["method"], "PUT")
        self.assertEqual(f"https://{OSS_HOST}/{VECTOR_KEY}", captured["url"])
        self.assertEqual(captured["body"], QR_BYTES)

        headers = {str(k).lower(): v for k, v in dict(captured["headers"]).items()}
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(headers["x-oss-security-token"], STS_TOKEN)
        self.assertRegex(headers["date"], _HTTP_DATE_RE)

        authorization = headers["authorization"]
        self.assertTrue(authorization.startswith(f"OSS {STS_AK_ID}:"))
        signature = authorization.rsplit(":", 1)[1]

        # 按文档格式**独立重算**（⛔ 不调被测的 `sign_oss_v1`）：
        string_to_sign = (
            "PUT\n"
            "\n"
            "image/png\n"
            f"{headers['date']}\n"
            f"x-oss-security-token:{STS_TOKEN}\n"
            f"{VECTOR_RESOURCE}"
        )
        expected = base64.b64encode(
            hmac.new(
                STS_AK_SECRET.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("ascii")
        self.assertEqual(signature, expected)

    def test_http_error_raises_without_echoing_credentials(self):
        # 上游错误 XML 里**故意**回显三件套：异常文案仍不许带它们。
        body = (
            "<Error><Code>AccessDenied</Code>"
            f"<Message>{STS_TOKEN} {STS_AK_ID} {STS_AK_SECRET}</Message>"
            "</Error>"
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text=body)

        with self.assertRaises(OssUploadError) as caught:
            self._put(handler)

        message = str(caught.exception)
        self.assertIn("403", message)
        self.assertIn("AccessDenied", message)
        self.assertNotIn(STS_TOKEN, message)
        self.assertNotIn(STS_AK_ID, message)
        self.assertNotIn(STS_AK_SECRET, message)

    def test_transport_failure_raises_without_echoing_credentials(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"boom {STS_TOKEN} {STS_AK_SECRET}")

        with self.assertRaises(OssUploadError) as caught:
            self._put(handler)

        message = str(caught.exception)
        self.assertNotIn(STS_TOKEN, message)
        self.assertNotIn(STS_AK_SECRET, message)


# ── 服务层（租户库）─────────────────────────────────────────────────────


class _TempMediaRootMixin:
    """把 `MEDIA_ROOT` 指到临时目录（同 `tests/test_media_storage.py` 的 mixin）。"""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        patcher.enable()
        self.addCleanup(patcher.disable)


class _FakeQrClient:
    """假代练通客户端：只实现本线用到的四跳，记录调用顺序。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.sts_error: Exception | None = None
        self.message_error: Exception | None = None
        self.progress_error: Exception | None = None
        #: 进度列表（`progress_add` 成功时按「它们会存成 URL」的形态追加一行）。
        self.progress_rows: list[dict] = []
        #: 留言列表（`message_add` 成功时追加；发送前回读靠它判断「留言已发」）。
        self.message_rows: list[dict] = []
        self.progress_img_template = "http://{host}/{img}"
        self.closed = False

    def _record(self, action: str, **fields) -> None:
        self.calls.append((action, fields))

    @property
    def actions(self) -> list[str]:
        return [action for action, _fields in self.calls]

    def get_sts(self):
        self._record("GetSTS")
        if self.sts_error is not None:
            raise self.sts_error
        return _sts()

    def message_add(self, serial_no, msg):
        self._record("message_add", serial_no=serial_no, msg=msg)
        if self.message_error is not None:
            raise self.message_error
        self.message_rows.append({"Msg": msg, "SerialNo": serial_no})
        return "发送成功."

    def message_list(self, serial_no, *, max_count=None):
        self._record("message_list", serial_no=serial_no, max_count=max_count)
        return ListPage(rows=tuple(self.message_rows))

    def progress_add(self, serial_no, msg, *, img=None):
        self._record("progress_add", serial_no=serial_no, msg=msg, img=img)
        if self.progress_error is not None:
            raise self.progress_error
        self.progress_rows.append(
            {
                "Msg": msg,
                "Img": self.progress_img_template.format(host=OSS_HOST, img=img or ""),
            }
        )
        return "添加完成."

    def progress_list(self, serial_no, *, max_count=None):
        self._record("progress_list", serial_no=serial_no, max_count=max_count)
        return ListPage(rows=tuple(self.progress_rows))

    def close(self):
        self.closed = True


class _QrTestBase(_TempMediaRootMixin, TenantTestCase):
    """租户 A：一个启用且默认的代练通配置 + 一条已接手的推送行。"""

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.operator = get_user_model().objects.create_user(
            username="dlt-qr-operator", password="synth-pw-0001", first_name="群码员"
        )
        self.game = Game.objects.create(name="王者荣耀")
        self.other_game = Game.objects.create(name="和平精英")
        self.server = GameServer.objects.create(game=self.game, name="安卓QQ区")
        self.integration = PlatformIntegration.objects.create(
            platform=10,
            account="USR-SYNTH-QR-01",
            app_id=APP_ID,
            app_secret=APP_SECRET,
            pay_password_md5=PAY_PASSWORD_MD5,
            is_enabled=True,
            is_default=True,
            contact_phone="",
            contact_qq="",
        )
        self.order = Order.objects.create(
            game=self.game,
            server=self.server,
            order_no=ORDER_NO,
            status=OrderStatus.WAITING_GRAB,
            project_text="星耀上王者",
            total_price=Decimal("120.00"),
        )
        self.push = DailiantongPush.objects.create(
            order=self.order,
            platform=10,
            state=DailiantongPush.State.SENT,
            serial_no=SERIAL,
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=ORDER_NO,
            remote_status=12,
            remote_cancel_status=10,
            attempts=1,
        )
        self.fake = _FakeQrClient()
        self.uploaded: list[dict] = []

    # ── 夹具 ────────────────────────────────────────────────────────
    def _enable_switch(self, value: int = 1) -> None:
        settings_row = PlatformPushSettings.get_solo()
        settings_row.auto_send_group_qr_after_accept = value
        settings_row.save(update_fields=["auto_send_group_qr_after_accept", "updated_at"])

    def _order_qr(
        self,
        *,
        key: str = "",
        data: bytes = QR_BYTES,
        status: str = OrderScreenshot.STATUS_READY,
    ) -> OrderScreenshot:
        object_key = key or f"{self.tenant.schema_name}/orders/{ORDER_NO}/qr.r1.png"
        get_storage().put_bytes(object_key, data, content_type="image/png")
        return OrderScreenshot.objects.create(
            order=self.order,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            object_key=object_key,
            original_name="qr.png",
            size_bytes=len(data),
            content_type="image/png",
            uploaded_by=self.operator,
            status=status,
        )

    def _home_qr(
        self,
        *,
        game=None,
        key: str = "",
        data: bytes = FALLBACK_QR_BYTES,
        is_enabled: bool = True,
    ) -> HomeQrCode:
        """首页 QQ 群码（⭐ B1 之后**不再**被接手后发送使用；夹具留着证明它没被用）。"""
        media_key = key or f"{self.tenant.schema_name}/users/{self.operator.pk}/home-qr.r1.png"
        get_storage().put_bytes(media_key, data, content_type="image/png")
        return HomeQrCode.objects.create(
            kind=HomeQrCode.Kind.QQ_GROUP,
            game=game or self.game,
            media_key=media_key,
            is_enabled=is_enabled,
            created_by="tester",
        )

    def _rule(
        self,
        *,
        trigger: int = 0,
        game="__default__",
        content: str = "合成话术：您好，已接单。",
        image_key: str = "",
        is_enabled: bool = True,
    ) -> PlatformAutoReplyRule:
        """一个「接单后」规则（`game="__default__"` = 本单游戏；`None` = 全部游戏）。"""
        return PlatformAutoReplyRule.objects.create(
            platform=10,
            game=self.game if game == "__default__" else game,
            trigger=trigger,
            content=content,
            image_key=image_key,
            is_enabled=is_enabled,
        )

    def _rule_image(self, suffix: str = "r1") -> str:
        key = f"{self.tenant.schema_name}/auto-reply/qr-test-{suffix}.png"
        get_storage().put_bytes(key, RULE_IMAGE_BYTES, content_type="image/png")
        return key

    def _send(self):
        """调服务层；客户端与 OSS 上传都换成假件（⛔ 真实网络）。"""

        def fake_put_object(sts, object_key, data, content_type, **kwargs):
            self.uploaded.append(
                {
                    "sts": sts,
                    "object_key": object_key,
                    "data": data,
                    "content_type": content_type,
                }
            )

        with mock.patch.object(
            qr_module, "_open_client", side_effect=lambda _integration: self.fake
        ), mock.patch.object(qr_module, "put_object", side_effect=fake_put_object):
            return qr_module.send_accept_qr(self.push)

    def _reload(self) -> DailiantongPush:
        self.push.refresh_from_db()
        return self.push

    def _audit_rows(self):
        return list(OperationLog.objects.filter(action=AUDIT_ACTION).order_by("id"))


class SendQrTests(_QrTestBase):
    def test_switch_off_without_a_rule_sends_nothing_even_with_an_order_qr(self):
        """业务方口径：开关只管图；没有规则话术 ⇒ 图被挡住，本次什么都不发。"""
        self._enable_switch(0)
        self._order_qr()

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SKIPPED_NO_CONTENT)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.uploaded, [])
        push = self._reload()
        self.assertIsNone(push.qr_sent_at)
        self.assertIn("未开启", push.last_error)
        self.assertIn("没有话术", push.last_error)
        self.assertEqual(self._audit_rows(), [])

    def test_switch_off_with_a_rule_sends_the_text_but_not_the_image(self):
        """⭐ 2026-10-27 业务方改准：第 6 条开关只管图，话术与它无关。"""
        self._enable_switch(0)
        self._order_qr()
        self._rule(content="开关关也要发的话术")

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        # 只发文字：⛔ 不传图、⛔ 不发进度、⛔ 不取 OSS 凭证。
        self.assertEqual(self.uploaded, [])
        self.assertNotIn("GetSTS", self.fake.actions)
        self.assertNotIn("progress_add", self.fake.actions)
        self.assertEqual(self.fake.actions.count("message_add"), 1)
        message = self.fake.calls[-1][1]["msg"]
        self.assertTrue(message.startswith("开关关也要发的话术"))
        push = self._reload()
        self.assertIsNotNone(push.qr_sent_at)
        self.assertEqual(push.last_error, "")
        self.assertIn("规则话术已发送", self._audit_rows()[0].summary)

    def test_switch_on_with_a_rule_but_no_image_sends_text_only(self):
        """父需求 #3：开关开 + 有规则 + 无图 ⇒ 只发文字。"""
        self._enable_switch(1)
        self._rule(content="只有文字的话术")

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertEqual(self.uploaded, [])
        self.assertNotIn("progress_add", self.fake.actions)
        self.assertEqual(self.fake.actions.count("message_add"), 1)
        self.assertIsNotNone(self._reload().qr_sent_at)

    def test_switch_on_with_a_rule_and_an_order_qr_sends_text_and_image(self):
        """父需求 #2：开关开 + 有规则 + 有本单码 ⇒ 文字 + 图（各一条）。"""
        self._enable_switch(1)
        self._order_qr()
        self._rule(content="带图话术")

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertEqual(outcome.source, qr_module.SOURCE_ORDER_QR)
        self.assertEqual(len(self.uploaded), 1)
        self.assertEqual(self.fake.actions.count("message_add"), 1)
        self.assertEqual(self.fake.actions.count("progress_add"), 1)
        self.assertIsNotNone(self._reload().qr_sent_at)

    def test_no_content_at_all_skips_with_a_readable_reason(self):
        """无图 + 无「接单后」规则 ⇒ 跳过；⛔ 不发空内容、⛔ 不打上游。"""
        self._enable_switch(1)

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SKIPPED_NO_CONTENT)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.uploaded, [])
        push = self._reload()
        self.assertIsNone(push.qr_sent_at)
        self.assertIn("无可发送内容", push.last_error)
        self.assertIn("规则", push.last_error)
        self.assertIn("无可发送内容", outcome.reason)
        self.assertEqual(self._audit_rows(), [])

    def test_normal_path_uploads_order_qr_sends_rule_text_and_verifies_progress(self):
        self._enable_switch(1)
        shot = self._order_qr()
        rule = self._rule(content="合成话术：本单已接，请扫码进群。")

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertEqual(len(self.uploaded), 1)
        upload = self.uploaded[0]
        self.assertEqual(upload["sts"], _sts())
        self.assertEqual(upload["data"], QR_BYTES)
        self.assertEqual(upload["content_type"], "image/png")
        self.assertRegex(upload["object_key"], rf"^tt/{self.tenant.schema_name}/{ORDER_NO}/qr-\d{{13}}\.png$")
        self.assertEqual(
            self.fake.actions,
            ["message_list", "progress_list", "GetSTS", "progress_add", "progress_list", "message_add"],
        )

        # ⭐ 一次接手 = 最多一条留言 + 一张图（计划 §2.1）。
        self.assertEqual(self.fake.actions.count("message_add"), 1)
        self.assertEqual(self.fake.actions.count("progress_add"), 1)
        message = self.fake.calls[-1][1]["msg"]
        self.assertTrue(message.startswith("合成话术：本单已接，请扫码进群。"))
        self.assertIn(ORDER_NO, message)
        self.assertIn(f"规则 {rule.pk}", message)
        # ⛔ 不再有「兜底」字样（旧 HomeQrCode 兜底已删）。
        self.assertNotIn("兜底", message)

        progress = self.fake.calls[3][1]
        self.assertEqual(progress["img"], upload["object_key"])
        self.assertEqual(progress["serial_no"], SERIAL)
        self.assertEqual(progress["msg"], qr_module.PROGRESS_CAPTION)

        push = self._reload()
        self.assertIsNotNone(push.qr_sent_at)
        self.assertEqual(push.qr_last_error, "")
        self.assertEqual(push.last_error, "")

        rows = self._audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].order_no, ORDER_NO)
        # 对象名 / 规则正文都属于不该进审计的内容（内部标识 / 用户自由文本）。
        self.assertNotIn(upload["object_key"], rows[0].summary)
        self.assertNotIn(shot.object_key, rows[0].summary)
        self.assertNotIn(rule.content, rows[0].summary)

    def test_order_qr_alone_sends_no_message(self):
        """没有规则 ⇒ **不发明默认话术**（计划 §2.1），只发图。"""
        self._enable_switch(1)
        self._order_qr()

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertEqual(outcome.source, qr_module.SOURCE_ORDER_QR)
        self.assertNotIn("message_add", self.fake.actions)
        self.assertEqual(len(self.uploaded), 1)
        self.assertIsNotNone(self._reload().qr_sent_at)

    def test_rule_image_is_used_when_the_order_has_no_group_qr(self):
        """图源第二顺位：该游戏「接单后」规则里的图片。"""
        self._enable_switch(1)
        image_key = self._rule_image()
        self._rule(content="带图话术", image_key=image_key)

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertEqual(outcome.source, qr_module.SOURCE_RULE_IMAGE)
        self.assertEqual(len(self.uploaded), 1)
        self.assertEqual(self.uploaded[0]["data"], RULE_IMAGE_BYTES)
        message = self.fake.calls[-1][1]["msg"]
        self.assertTrue(message.startswith("带图话术"))
        self.assertIsNotNone(self._reload().qr_sent_at)

    def test_all_games_rule_is_used_when_the_game_has_none(self):
        self._enable_switch(1)
        self._rule(game=None, content="全部游戏话术")

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertIn("全部游戏话术", self.fake.calls[-1][1]["msg"])
        # 没有图 ⇒ 只发文字（也是合法的一种「最多一条留言 + 最多一张图」）。
        self.assertEqual(self.uploaded, [])
        self.assertNotIn("progress_add", self.fake.actions)

    def test_specific_game_rule_wins_over_the_all_games_rule(self):
        self._enable_switch(1)
        self._rule(game=None, content="全部游戏话术")
        self._rule(game=self.game, content="本游戏话术")

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertIn("本游戏话术", self.fake.calls[-1][1]["msg"])

    def test_disabled_rule_is_ignored(self):
        self._enable_switch(1)
        self._rule(content="停用话术", is_enabled=False)

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SKIPPED_NO_CONTENT)
        self.assertEqual(self.fake.calls, [])
        self.assertIsNone(self._reload().qr_sent_at)

    def test_home_qr_is_never_used_as_a_fallback(self):
        """⛔ B1：`HomeQrCode`（首页 QQ 群码是给客户加群的）不再参与接手后发送。

        夹具里**故意**放一张启用的、本游戏的首页群码：如果实现还看它，就会上传并成功。
        """
        self._enable_switch(1)
        home = self._home_qr()

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SKIPPED_NO_CONTENT)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.uploaded, [])
        self.assertIsNone(self._reload().qr_sent_at)
        # 证明夹具真的存在（否则这条用例可能是「本来就没图」的假绿）。
        self.assertTrue(HomeQrCode.objects.filter(pk=home.pk, is_enabled=True).exists())

    def test_order_qr_wins_over_the_rule_image_when_both_exist(self):
        """图源顺序 ① 必须先于 ②：两支对调 ⇒ 本用例红（评审 I1）。"""
        self._enable_switch(1)
        self._rule(image_key=self._rule_image())  # 规则图在场
        self._order_qr()  # 本单码也在场

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertEqual(outcome.source, qr_module.SOURCE_ORDER_QR)
        self.assertEqual(len(self.uploaded), 1)
        self.assertEqual(self.uploaded[0]["data"], QR_BYTES)
        message = self.fake.calls[-1][1]["msg"]
        self.assertNotIn("兜底", message)
        self.assertNotIn("未上传", message)

    def test_pending_order_qr_is_not_used_and_the_rule_image_is_used(self):
        """`status="ready"` 过滤：**待认领（pending）**的本单码不算数 ⇒ 退到规则图片。"""
        self._enable_switch(1)
        self._order_qr(
            key=f"{self.tenant.schema_name}/orders/{ORDER_NO}/qr.pending.png",
            data=b"\x89PNG\r\n\x1apending-qr-bytes",
            status=OrderScreenshot.STATUS_PENDING,
        )
        self._rule(image_key=self._rule_image())

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertEqual(outcome.source, qr_module.SOURCE_RULE_IMAGE)
        self.assertEqual(len(self.uploaded), 1)
        self.assertEqual(self.uploaded[0]["data"], RULE_IMAGE_BYTES)

    def test_pending_order_qr_alone_means_no_content_at_all(self):
        """只有待认领的本单码、且没有规则 ⇒ 仍然「无可发送内容」，⛔ 不拿 pending 充数。"""
        self._enable_switch(1)
        self._order_qr(
            key=f"{self.tenant.schema_name}/orders/{ORDER_NO}/qr.pending.png",
            data=b"\x89PNG\r\n\x1apending-qr-bytes",
            status=OrderScreenshot.STATUS_PENDING,
        )

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SKIPPED_NO_CONTENT)
        self.assertEqual(self.uploaded, [])
        self.assertEqual(self.fake.calls, [])
        self.assertIsNone(self._reload().qr_sent_at)

    def test_upload_failure_keeps_qr_sent_at_empty_and_records_reason(self):
        self._enable_switch(1)
        self._order_qr()

        def failing_put_object(*_args, **_kwargs):
            raise OssUploadError("上传到代练通 OSS 失败（HTTP 403，Code=AccessDenied）")

        with mock.patch.object(
            qr_module, "_open_client", side_effect=lambda _integration: self.fake
        ), mock.patch.object(qr_module, "put_object", side_effect=failing_put_object):
            outcome = qr_module.send_accept_qr(self.push)

        self.assertEqual(outcome.status, qr_module.STATUS_FAILED)
        self.assertIn("403", outcome.reason)
        push = self._reload()
        self.assertIsNone(push.qr_sent_at)
        self.assertIn("403", push.last_error)
        self.assertIn("403", push.qr_last_error)
        # 上传没成 ⇒ 留言 / 进度一次都不许发（⛔ 不刷屏）。
        self.assertEqual(self.fake.actions, ["message_list", "progress_list", "GetSTS"])

    def test_progress_readback_asks_for_a_full_page(self):
        """回读显式多取（`Max=100`）：⛔ 不拿上游默认首屏去核验（评审 I3）。"""
        self._enable_switch(1)
        self._order_qr()

        self._send()

        reads = [fields for action, fields in self.fake.calls if action == "progress_list"]
        # 两次回读（发送前判是否已发 + 发送后自证）都必须显式多取；
        # 钉字面值（100），⛔ 不钉模块常量（那会随实现漂移）。
        self.assertEqual(len(reads), 2)
        self.assertEqual([row["max_count"] for row in reads], [100, 100])

    def test_progress_image_found_when_it_is_not_the_first_row(self):
        """图在很后面（前面有 80 条历史进度）也要核验到（评审 I3）。"""
        self._enable_switch(1)
        self._order_qr()
        self.fake.progress_rows = [
            {
                "Msg": f"历史进度 {index}",
                "Img": f"http://{OSS_HOST}/tt/old/{index}.png",
            }
            for index in range(80)
        ]

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertEqual(len(self.uploaded), 1)
        # 本次那张是第 81 条（⛔ 不是首条），且回读里确实有它。
        self.assertEqual(len(self.fake.progress_rows), 81)
        self.assertIn(self.uploaded[0]["object_key"], self.fake.progress_rows[-1]["Img"])
        self.assertIsNotNone(self._reload().qr_sent_at)

    def test_progress_missing_after_add_is_a_failure(self):
        self._enable_switch(1)
        self._order_qr()
        self.fake.progress_img_template = "http://{host}/unrelated-{img}"

        # 让回读里**没有**本次对象名（把 Img 改成不相干的值）。
        original_add = self.fake.progress_add

        def add_without_image(*args, **kwargs):
            original_add(*args, **kwargs)
            self.fake.progress_rows[-1]["Img"] = "http://example.invalid/not-ours.png"
            return "添加完成."

        self.fake.progress_add = add_without_image

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_FAILED)
        push = self._reload()
        self.assertIsNone(push.qr_sent_at)
        self.assertIn("没", push.last_error)

    def test_message_failure_is_a_failure_and_keeps_the_image_but_not_the_marker(self):
        self._enable_switch(1)
        self._order_qr()
        self._rule(content="会失败的留言")
        self.fake.message_error = DailiantongError(
            code="-1", message="订单状态异常", action="LevelOrderMessageAdd"
        )

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_FAILED)
        self.assertIn("订单状态异常", outcome.reason)
        self.assertEqual(
            self.fake.actions,
            ["message_list", "progress_list", "GetSTS", "progress_add", "progress_list", "message_add"],
        )
        push = self._reload()
        self.assertIsNone(push.qr_sent_at)
        self.assertIn("订单状态异常", push.last_error)

    def test_already_sent_push_is_not_sent_again(self):
        self._enable_switch(1)
        self._order_qr()
        self.push.qr_sent_at = timezone.now()
        self.push.save(update_fields=["qr_sent_at", "updated_at"])

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_ALREADY_SENT)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.uploaded, [])
        self.assertEqual(self._audit_rows(), [])

    def test_no_content_then_uploaded_next_round_succeeds(self):
        """没有可发内容只是**跳过**：不置 `qr_sent_at` ⇒ 用户补上本单群码的下一轮还能发。"""
        self._enable_switch(1)
        first = self._send()
        self.assertEqual(first.status, qr_module.STATUS_SKIPPED_NO_CONTENT)
        self.assertIsNone(self._reload().qr_sent_at)

        self._order_qr()  # 用户补上了本单群码
        second = self._send()

        self.assertEqual(second.status, qr_module.STATUS_SENT)
        self.assertIsNotNone(self._reload().qr_sent_at)
        self.assertEqual(len(self.uploaded), 1)

    def test_repeated_failure_retries_once_per_round_but_audits_the_reason_once(self):
        """「下轮重试、每轮至多一次」+ 同一个失败原因只写一条审计（⛔ 30 秒刷一条）。"""
        self._enable_switch(1)
        self._order_qr()
        self._rule(content="会失败的留言")
        self.fake.message_error = DailiantongError(
            code="-1", message="订单状态异常", action="LevelOrderMessageAdd"
        )

        first = self._send()
        second = self._send()

        self.assertEqual(first.status, qr_module.STATUS_FAILED)
        self.assertEqual(second.status, qr_module.STATUS_FAILED)
        # 每轮尝试一次（两轮 = 两次留言调用；⛔ 不是同一轮里重试）。
        message_calls = [c for c in self.fake.calls if c[0] == "message_add"]
        self.assertEqual(len(message_calls), 2)
        self.assertIsNone(self._reload().qr_sent_at)
        self.assertEqual(len(self._audit_rows()), 1)

    def test_cancelling_push_is_skipped(self):
        self._enable_switch(1)
        self._order_qr()
        self.push.remote_cancel_status = 11  # 协商撤销中
        self.push.save(update_fields=["remote_cancel_status", "updated_at"])

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SKIPPED_CANCELLING)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.uploaded, [])
        self.assertIsNone(self._reload().qr_sent_at)

    def test_second_round_after_success_does_not_resend(self):
        self._enable_switch(1)
        self._order_qr()
        first = self._send()
        self.assertEqual(first.status, qr_module.STATUS_SENT)
        calls_after_first = len(self.fake.calls)

        second = self._send()

        self.assertEqual(second.status, qr_module.STATUS_ALREADY_SENT)
        self.assertEqual(len(self.fake.calls), calls_after_first)
        self.assertEqual(len(self.uploaded), 1)
        self.assertEqual(len(self._audit_rows()), 1)

    def test_credentials_never_leak_into_logs_audit_or_last_error(self):
        """端到端：真 `put_object` 遇 403、上游错误 XML 里回显三件套。"""
        self._enable_switch(1)
        self._order_qr()

        leaky_body = (
            "<Error><Code>AccessDenied</Code>"
            f"<Message>{STS_TOKEN} {STS_AK_ID} {STS_AK_SECRET}</Message></Error>"
        )

        def leaky_oss_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text=leaky_body)

        def real_put_with_fake_transport(sts, object_key, data, content_type, **kwargs):
            return put_object(
                sts, object_key, data, content_type,
                transport=httpx.MockTransport(leaky_oss_handler),
            )

        with self.assertLogs("apps.business.dailiantong_qr", level="DEBUG") as captured, \
                mock.patch.object(
                    qr_module, "_open_client", side_effect=lambda _integration: self.fake
                ), mock.patch.object(
                    qr_module, "put_object", side_effect=real_put_with_fake_transport
                ):
            outcome = qr_module.send_accept_qr(self.push)

        self.assertEqual(outcome.status, qr_module.STATUS_FAILED)
        push = self._reload()
        self.assertIsNone(push.qr_sent_at)

        audit_text = " ".join(
            "".join(
                str(getattr(row, column) or "")
                for column in (
                    "summary", "actor", "actor_name", "target_name", "order_no",
                    "from_value", "to_value", "reason",
                )
            )
            for row in self._audit_rows()
        )
        log_text = "\n".join(record.getMessage() for record in captured.records)
        haystack = "\n".join([audit_text, log_text, push.last_error, push.qr_last_error, outcome.reason])
        for secret in (STS_TOKEN, STS_AK_ID, STS_AK_SECRET):
            self.assertNotIn(secret, haystack)

    def test_storage_read_failure_is_recorded_without_object_key_or_credentials(self):
        self._enable_switch(1)
        shot = self._order_qr()
        # 行还在、对象没了（存储抽风 / 被清理）。
        get_storage().delete(shot.object_key)

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_FAILED)
        push = self._reload()
        self.assertIsNone(push.qr_sent_at)
        self.assertIn("读取", push.last_error)
        self.assertNotIn(shot.object_key, push.last_error)
        for secret in (STS_TOKEN, STS_AK_ID, STS_AK_SECRET):
            self.assertNotIn(secret, push.last_error)
        self.assertEqual(self.fake.calls, [])


# ── 触发点（`dailiantong_sync --once`）──────────────────────────────────


class _SyncTriggerBase(_QrTestBase):
    """按 `test_dailiantong_sync` 的夹具口径：回调根指到临时目录 + 假详情。"""

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp(prefix="dlt-qr-sync-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        env_patcher = mock.patch.dict(
            os.environ, {"DAILIANTONG_CALLBACK_ROOT": self.tmpdir}, clear=False
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def _detail_client_factory(self, integration, *, status: int):
        def handler(request: httpx.Request) -> httpx.Response:
            form = dict(parse_qsl(request.content.decode("utf-8")))
            if form.get("Action") == "LevelOrderDetail":
                return httpx.Response(200, json={"Status": status, "CancelStatus": 10})
            return httpx.Response(200, json={"Result": -101, "Err": "未知动作"})

        return DailiantongClient(
            DailiantongConfig(
                app_id=integration.app_id,
                app_secret=integration.app_secret,
                user_id=integration.account,
                pay_password_md5=integration.pay_password_md5,
            ),
            transport=httpx.MockTransport(handler),
            clock=lambda: 1_790_000_000,
        )

    def _run_sync(self, *, remote_status: int = 12, send_mock=None, switch: int = 1):
        self._enable_switch(switch)
        self.push.remote_status = 11
        self.push.save(update_fields=["remote_status", "updated_at"])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch(
            "apps.business.dailiantong_push._open_client",
            side_effect=lambda integration: self._detail_client_factory(
                integration, status=remote_status
            ),
        ), mock.patch(
            "apps.business.management.commands.dailiantong_sync.send_accept_qr",
            send_mock if send_mock is not None else mock.MagicMock(
                return_value=qr_module.QrSendOutcome(qr_module.STATUS_SENT, "ok")
            ),
        ) as patched:
            call_command("dailiantong_sync", "--once", stdout=out, stderr=err)
        return patched, out.getvalue(), err.getvalue()


class QrIdempotencyAndRetryTests(_QrTestBase):
    """⭐ 审计修复（2026-10-27）：发送前回读 + 库层占位 + 有界重试（P1-1 / P1-2）。"""

    def setUp(self):
        super().setUp()
        self._enable_switch(1)
        self._order_qr()
        self.rule = self._rule(content="幂等测试话术")

    def _message(self) -> str:
        return qr_module.build_message_text(
            self.rule.content, order_no=ORDER_NO, rule_id=self.rule.pk
        )

    def _assert_no_claim_left(self):
        push = self._reload()
        self.assertIsNone(push.qr_send_started_at)
        self.assertEqual(push.qr_claim_token, "")
        return push

    def test_message_already_in_readback_skips_message_and_only_sends_image(self):
        """上一轮图/留言发了一半：回读命中的步骤不再重放（P1-1 的核心判据）。"""
        self.fake.message_rows = [{"Msg": self._message(), "SerialNo": SERIAL}]

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertNotIn("message_add", self.fake.actions)
        self.assertEqual(len(self.uploaded), 1)
        push = self._assert_no_claim_left()
        self.assertIsNotNone(push.qr_sent_at)

    def test_image_already_in_readback_skips_upload_and_only_sends_message(self):
        self.fake.progress_rows = [
            {"Msg": "旧进度", "Img": f"http://{OSS_HOST}/{qr_module.build_qr_marker(ORDER_NO)}1700000000000.png"}
        ]

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertNotIn("progress_add", self.fake.actions)
        self.assertNotIn("GetSTS", self.fake.actions)
        self.assertEqual(self.uploaded, [])
        self.assertIn("message_add", self.fake.actions)
        self.assertIsNotNone(self._reload().qr_sent_at)

    def test_both_steps_already_there_marks_sent_without_any_write(self):
        self.fake.message_rows = [{"Msg": self._message(), "SerialNo": SERIAL}]
        self.fake.progress_rows = [
            {"Msg": "旧进度", "Img": f"http://{OSS_HOST}/{qr_module.build_qr_marker(ORDER_NO)}1700000000000.png"}
        ]

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertNotIn("message_add", self.fake.actions)
        self.assertNotIn("progress_add", self.fake.actions)
        self.assertEqual(self.uploaded, [])
        self.assertIsNotNone(self._reload().qr_sent_at)

    def test_claim_blocks_a_second_process_with_zero_upstream_calls(self):
        """库层占位（新鲜）⇒ 第二个进程直接退出，⛔ 不重复发。"""
        self.push.qr_send_started_at = timezone.now()
        self.push.qr_claim_token = "other-process-token"
        self.push.save(update_fields=["qr_send_started_at", "qr_claim_token", "updated_at"])

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SKIPPED_IN_PROGRESS)
        # 只允许发送前的只读回读；⛔ 任何写动作 / 上传都不许发生。
        self.assertNotIn("message_add", self.fake.actions)
        self.assertNotIn("progress_add", self.fake.actions)
        self.assertNotIn("GetSTS", self.fake.actions)
        self.assertEqual(self.uploaded, [])
        self.assertIsNone(self._reload().qr_sent_at)

    def test_stale_claim_is_reclaimed(self):
        """占位过期（进程被杀）⇒ 下一轮允许重新占用。"""
        self.push.qr_send_started_at = timezone.now() - timedelta(
            seconds=qr_module.QR_CLAIM_STALE_SECONDS + 1
        )
        self.push.qr_claim_token = "dead-process-token"
        self.push.save(update_fields=["qr_send_started_at", "qr_claim_token", "updated_at"])

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertIsNotNone(self._reload().qr_sent_at)

    def test_claim_is_written_to_the_db_before_the_upstream_write(self):
        """发送前占位：上传那一刻，库里必须已经能看到占位时间（P1-2）。"""
        seen = {}

        def observing_put_object(sts, object_key, data, content_type, **kwargs):
            row = DailiantongPush.objects.get(pk=self.push.pk)
            seen["started_at"] = row.qr_send_started_at
            seen["token"] = row.qr_claim_token
            self.uploaded.append({"object_key": object_key})

        with mock.patch.object(
            qr_module, "_open_client", side_effect=lambda _integration: self.fake
        ), mock.patch.object(
            qr_module, "put_object", side_effect=observing_put_object
        ):
            outcome = qr_module.send_accept_qr(self.push)

        self.assertEqual(outcome.status, qr_module.STATUS_SENT)
        self.assertIsNotNone(seen["started_at"])
        self.assertTrue(seen["token"])
        self._assert_no_claim_left()

    def test_five_consecutive_failures_stop_the_loop_with_a_readable_reason(self):
        self.fake.message_error = DailiantongError(
            code="-1", message="订单状态异常", action="LevelOrderMessageAdd"
        )

        for _ in range(qr_module.MAX_CONSECUTIVE_FAILURES):
            outcome = self._send()
            self.assertEqual(outcome.status, qr_module.STATUS_FAILED)

        push = self._reload()
        self.assertEqual(push.qr_attempts, qr_module.MAX_CONSECUTIVE_FAILURES)
        self.assertIn("已停止自动重发", push.qr_last_error)

        calls_before = len(self.fake.calls)
        capped = self._send()
        self.assertEqual(capped.status, qr_module.STATUS_SKIPPED_RETRY_LIMIT)
        self.assertEqual(len(self.fake.calls), calls_before)  # 零上游调用
        self.assertIsNone(self._reload().qr_sent_at)

    def test_changing_the_qr_image_resets_the_failure_budget(self):
        self.fake.message_error = DailiantongError(
            code="-1", message="订单状态异常", action="LevelOrderMessageAdd"
        )
        for _ in range(qr_module.MAX_CONSECUTIVE_FAILURES):
            self._send()
        self.push.refresh_from_db()
        self.assertEqual(self.push.qr_attempts, qr_module.MAX_CONSECUTIVE_FAILURES)
        # 换一张群码 = 新的失败预算（否则一次上游抖动后就永远不再试）。
        self.push.qr_retry_source_key = "old/source/key.png"
        self.push.save(update_fields=["qr_retry_source_key", "updated_at"])

        calls_before = len(self.fake.calls)
        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_FAILED)
        # 换图后预算归零，这一轮失败一次 ⇒ 1（不是继续累加到 6）。
        self.assertEqual(self.push.qr_attempts, 1)
        self.assertEqual(
            [action for action, _f in self.fake.calls[calls_before:]].count(
                "message_add"
            ),
            1,
        )

    def test_missing_serial_does_not_burn_the_retry_budget(self):
        self.push.serial_no = None
        self.push.save(update_fields=["serial_no", "updated_at"])

        outcome = self._send()

        self.assertEqual(outcome.status, qr_module.STATUS_FAILED)
        self.assertIn("没有上游订单号", outcome.reason)
        self.push.refresh_from_db()
        self.assertEqual(self.push.qr_attempts, 0)

    def test_object_key_has_the_tenant_prefix(self):
        self._send()
        object_key = self.uploaded[0]["object_key"]
        self.assertTrue(
            object_key.startswith(f"tt/{self.tenant.schema_name}/{ORDER_NO}/qr-")
        )


class SyncTriggerTests(_SyncTriggerBase):
    def test_first_accept_triggers_exactly_one_send(self):
        patched, out, _err = self._run_sync(remote_status=12)

        self.assertEqual(patched.call_count, 1)
        called_push = patched.call_args.args[0]
        self.assertEqual(called_push.pk, self.push.pk)
        push = self._reload()
        self.assertEqual(push.remote_status, 12)
        self.assertIn("接手后 1", out)

    def test_status_13_triggers_when_12_was_missed_between_polls(self):
        """⭐ P3-11：12→13 落在两次轮询之间 ⇒ 只要没发过，13 也补发。"""
        patched, out, _err = self._run_sync(remote_status=13)

        self.assertEqual(patched.call_count, 1)
        self.assertIn("接手后 1", out)

    def test_switch_off_still_runs_the_sender_because_text_is_independent(self):
        """⭐ 2026-10-27 业务方改准：第 6 条开关只管图 ⇒ 关着也要跑（文字可能该发）。"""
        patched, _out, _err = self._run_sync(remote_status=12, switch=0)

        self.assertEqual(patched.call_count, 1)

    def test_push_not_yet_in_progress_is_not_triggered(self):
        patched, _out, _err = self._run_sync(remote_status=11)

        self.assertEqual(patched.call_count, 0)

    def test_already_sent_push_is_not_triggered_again(self):
        self.push.qr_sent_at = timezone.now()
        self.push.save(update_fields=["qr_sent_at", "updated_at"])
        patched, _out, _err = self._run_sync(remote_status=12)

        self.assertEqual(patched.call_count, 0)

    def test_sender_failure_does_not_break_the_round(self):
        failing = mock.MagicMock(side_effect=RuntimeError("boom"))
        patched, out, err = self._run_sync(remote_status=12, send_mock=failing)

        self.assertEqual(patched.call_count, 1)
        # 单条失败是预期内噪音：整轮照常跑完、退出码不非零。
        self.assertIn("轮询 1", out)
        self.assertIn("接手后", out)
        self.assertNotIn("boom", err)
