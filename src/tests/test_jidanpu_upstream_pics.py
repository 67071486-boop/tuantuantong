"""上家鸡蛋铺线（客服个人绑定）：**传图三步**（计划 Task 7；spec §6.3 / §2.4 / §11-10~13 / §13.1-R4）。

被钉住的契约（**都是 2026-09-28 真机实测下来的形状**，⛔ 不许走样）：

① **取 OSS 策略** `GET /CommonPermissionAjax/GetAliOssPolicy?bk=1&exsec=240`（**走 SDK**）
   ⇒ `data` 里有 `accessid` / `host` / `policy` / `signature` / `expire`；
② **直传阿里云 OSS**：multipart，字段顺序**固定** `key` / `policy` / `OSSAccessKeyId` /
   `signature` / `success_action_status=200` / `file`（**`file` 必须最后**）；
   `key = ${sid}-${orderId}-${rand5}-${epoch}.${ext}`。
   ⚠️ ⛔ **这一跳不是鸡蛋铺端点**（SDK 没有 OSS 上传器）⇒ 它是**独立故障面**（spec §13.1-R4），
   失败抛 `OssUploadError`，⛔ **不是**鸡蛋铺的 `ApiError` / `JidanpuError`（两档不互相伪装）；
③ **登记图片** `POST /OrderAjax/UploadOrderPic`（走 SDK）
   `data={orderId, remark, imgListStr:"<host>/<key>", gameAccId:"0", taskId:"0", imgType:"0"}`；
④ `sid` / `gameAccId` / `taskId` **三个值全部从上传页隐藏域取**（复用 Task 3 的
   `fetch_upload_page_fields`）——⛔ **一个都不硬编码**；
⑤ **判断上家有没有图**：`GET /OrderAjax/ViewPic?orderId=…` ⇒ **`msg.data` 非空即已有图**
   （⛔ **`imgCount` 不是判据**：实测补图前后恒为 0）。

## 本文件怎么钉

- `build_oss_multipart` 是**纯函数**（不过网）⇒ 字段顺序 / `file` 在最后**直接断字节**；
  默认 `post_to_oss` 另有一条**真 `httpx.Client` + `MockTransport`**（零网络）证明
  「真发出去的就是这些字节」，并证明它**不带任何 cookie / 认证头**；
- 第③步的成功码是**我们自己判的**（spec §2.3 / GC 7 / E-0087）：`register_pic` **显式传
  `success_rule="lenient"`** 把 SDK 的 `positive_code` 收手，让我们的判据成为**唯一出口**
  （否则 SDK 会在我们的判据之前自己抛异常 ⇒ 我们的判据不可达）；另有一条
  **真 SDK + `MockTransport`** 的失败信封用例（`code=0` + 非空 `msg` + 空 `data` ⇒ 必须判失败，
  ⛔ 不是桩抛异常伪造的）；
- ⚠️ `ViewPic` **三种形状都用真 SDK + `MockTransport` 钉**（有图 ⇒ True / 空图 ⇒ False /
  传输失败 ⇒ 仍抛）：它们的难点**全在 SDK 的宽松规则**上（`msg` 是对象 + 顶层没有 `data` ⇒
  SDK 把「空图」也判成失败），用桩测不出来；
- ⚠️ 文案期望是**测试里的字面量**（⛔ 不从被测常量派生：自指断言在常量被改坏时不会红，E-0024）；
  另有一小组用例把常量**钉回字面量**（值对值）；
- ⚠️ 判据打**桩的调用集合 / `PicOutcome` 的字段值 / 上传的原始字节**，⛔ 不打 logger；
- ⚠️ **零真实网络**：`_StubClient` 打在 SDK 边界、`oss_post` 是注入替身；
- ⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `upstream-pics-a`、
  密码 `pw-canary-pics`、单号 `20260928000000000701`（全零段，一眼是合成值）、`sid` 用 `10001`、
  `gameAccId` / `taskId` **故意用 `7` / `9`**（实测真值是 `0` ⇒ 用非 `0` 的合成值才能证明
  「读出来的、不是写死的」）。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone as dt_timezone
from unittest import mock

import httpx
from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import SimpleTestCase, override_settings
from django_tenants.test.cases import TenantTestCase
from jidanpu.client import JidanpuClient
from jidanpu.config import JidanpuConfig
from jidanpu.errors import (
    ApiError,
    JidanpuError,
    JidanpuTimeoutError,
    ServerError,
    SessionExpiredError,
    TransportError,
)
from jidanpu.transport import Transport

from apps.business import jidanpu, jidanpu_upstream, jidanpu_upstream_pics
from apps.business.models import (
    Game,
    JidanpuUpstreamBinding,
    JidanpuUpstreamOrderLink,
    Order,
    OrderScreenshot,
    OrderStatus,
)
from apps.core.storage import ALLOWED_EXTS, MAX_IMAGE_BYTES, get_storage

#: 端点路径**逐字**抄 spec §2.1 / §2.3 / §2.4（⛔ 不从模块常量派生）。
VIEW_PIC_PATH = "/OrderAjax/ViewPic"
UPLOAD_PIC_PATH = "/OrderAjax/UploadOrderPic"
OSS_POLICY_PATH = "/CommonPermissionAjax/GetAliOssPolicy"
UPLOAD_PAGE_PATH = "/Order/UploadPic"
LOGIN_PATH = "/Login/Login"

BASE_URL = "https://jidanpu-upstream-pics.test.invalid"
ACCOUNT = "upstream-pics-a"
PASSWORD = "pw-canary-pics"

#: 合成单号（一段零，一眼是夹具）。
REMOTE_ID = "20260928000000000701"

SID_SYNTH = "10001"
GAME_ACC_SYNTH = "7"
TASK_SYNTH = "9"
OSS_HOST = "https://oss-cn-synth.aliyuncs.com"

#: 合成凭据 canary（spec §11-18：**搜字符串**，不是「看起来没有」）。
CANARY_ACCESSID = "CANARY-ACCESSID-PICS"
CANARY_SIGNATURE = "CANARY-SIGNATURE-PICS"
CANARY_POLICY = "CANARY-POLICY-PICS"

#: 期望文案（**字面量**，独立于实现；spec §6.3 的人话）。
#: ⚠️这句话现在是**方向中立**的（上家线那边是「上家」、
#: 代练员线那边是「下家」）⇒ 两处都读得通。
ALREADY_HAD = "对面这张单已经有图了（只看有没有图、不看图种），不重复传"
EXT_REASON = "图片格式不是 jpg/jpeg/png，未上传"
SIZE_REASON = "图片超过单张 18MB 上限，未上传"

#: multipart 字段顺序（**逐字**抄 spec §2.4；`file` 最后）。
FIELD_ORDER = ("key", "policy", "OSSAccessKeyId", "signature", "success_action_status", "file")

#: 取**每个 part 的 form-data 字段名**。⚠️ 必须带 `Content-Disposition: form-data; ` 前缀：
#: `filename="…"` 里**也含** `name="`（"file**name=\""**），只搜 `name="` 会把文件名数进来
#: ——计划里那条 `body.rindex(b'name="') == body.index(b'name="file"')` 正是被它坑到
#: （真页面用 `FormData.append('file', File)`，**带** `filename`；见 `.tools/recon/media-storage/`
#: 的 `assets_JS2019_Order_uploadpicnew.js`）。
_PART_NAME_RE = re.compile(rb'Content-Disposition: form-data; name="([^"]+)"')
_PART_HEADER = b'Content-Disposition: form-data; name="'

#: 真 `httpx.Client`：模块级取好，这样下面那个工厂替换 `httpx.Client` 之后仍能造出**真的**客户端
#: （只是 transport 换成 `httpx.MockTransport`）。
_REAL_HTTPX_CLIENT = httpx.Client


def _http_factory(handler):
    """把 `httpx.Client(...)` 换成「真客户端 + MockTransport」⇒ **零网络**。"""

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _upload_page_html(*, sid=SID_SYNTH, game_acc_id=GAME_ACC_SYNTH, task_id=TASK_SYNTH,
                      order_id=REMOTE_ID):
    """合成上传页：隐藏域里带 `orderId` / `gameAccId` / `taskId` / `sid`（spec §2.4 的形状）。"""
    parts = ["<html><body><form method=\"post\">"]
    if order_id is not None:
        parts.append(f'<input type="hidden" name="orderId" value="{order_id}" />')
    if game_acc_id is not None:
        parts.append(f"<input name='gameAccId' type='hidden' value='{game_acc_id}'>")
    if task_id is not None:
        parts.append(f'<input type="hidden" value="{task_id}" name="taskId"/>')
    if sid is not None:
        parts.append(f"<input value='{sid}' name='sid' type='hidden'>")
    parts.append("</form></body></html>")
    return "".join(parts)


class _StubHttp:
    """HTTP 边界替身（只服务上传页那一跳）。"""

    def __init__(self, *, status=200, html=None, location=""):
        self.status = status
        self.html = _upload_page_html() if html is None else html
        self.location = location
        self.gets: list[tuple[str, dict]] = []

    def get(self, path, **kwargs):
        self.gets.append((path, kwargs))
        headers = {"Location": self.location} if self.location else {}
        if self.status == 200:
            return httpx.Response(200, headers=headers, text=self.html)
        return httpx.Response(self.status, headers=headers, text="<html>合成错误页</html>")


class _StubClient:
    """SDK 边界替身：按端点回包 / 抛错，并记录调用集合（⛔ 不接真网络）。

    - `pic_envelope`：`ViewPic` 的**原始业务壳**（判据只看 `msg.data`）；
    - `policy` / `policy_error`：`GetAliOssPolicy` 的 `data`（或要抛的异常）；
    - `register_payload` / `register_error` / `register_code`：`UploadOrderPic` 的回包。
    """

    def __init__(self, *, pic_envelope=None, policy=None, policy_error=None,
                 register_code=1, register_msg="成功保存1张图片", register_error=None):
        self.pic_envelope = pic_envelope if pic_envelope is not None else {
            "code": 0, "msg": "success", "data": [],
        }
        self.policy = policy if policy is not None else {
            "accessid": CANARY_ACCESSID, "host": OSS_HOST, "policy": CANARY_POLICY,
            "signature": CANARY_SIGNATURE, "expire": 1790784240,
        }
        self.policy_error = policy_error
        self.register_code = register_code
        self.register_msg = register_msg
        self.register_error = register_error
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {}

    def ensure_login(self):
        pass

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, dict(kwargs)))
        if path == VIEW_PIC_PATH:
            self.last_envelope = dict(self.pic_envelope)
            return self.pic_envelope.get("data", self.pic_envelope)
        if path == OSS_POLICY_PATH:
            if self.policy_error is not None:
                raise self.policy_error
            self.last_envelope = {"code": 0, "msg": "success", "data": self.policy}
            return self.policy
        if path == UPLOAD_PIC_PATH:
            if self.register_error is not None:
                raise self.register_error
            self.last_envelope = {
                "code": self.register_code, "msg": self.register_msg, "data": [],
            }
            return []
        raise AssertionError(f"未预期的端点：{method} {path}")


class _OssRecorder:
    """`oss_post` 的替身：记录调用，可指定在第几次抛 `OssUploadError`。"""

    def __init__(self, *, fail_at=None, error=None):
        self.calls: list[tuple] = []
        self.fail_at = fail_at
        self.error = error

    def __call__(self, host, fields, key, data, content_type, *, filename="file"):
        self.calls.append((host, dict(fields), key, data, content_type))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise self.error or jidanpu_upstream_pics.OssUploadError("合成：OSS 拒绝")

    #: 便于断言「一张都没真传」。
    @property
    def keys(self):
        return [call[2] for call in self.calls]


# ══ 常量与值（⛔ 钉值，不钉常量名）══════════════════════════════════════════════


class PicContractTests(SimpleTestCase):
    def test_endpoint_paths_are_the_verbatim_ones(self):
        """三个端点路径逐字钉住（spec §2.4）：⛔ 拼错一个就是一次静默失败。"""
        self.assertEqual(jidanpu_upstream_pics.VIEW_PIC_PATH, VIEW_PIC_PATH)
        self.assertEqual(jidanpu_upstream_pics.UPLOAD_PIC_PATH, UPLOAD_PIC_PATH)
        self.assertEqual(jidanpu_upstream_pics.OSS_POLICY_PATH, OSS_POLICY_PATH)
        # ⛔ 本模块不复制字面量，唯一出处仍是 `jidanpu_upstream`（值对值，不是自指）。
        self.assertEqual(jidanpu_upstream_pics.OSS_POLICY_PATH, jidanpu_upstream.OSS_POLICY_PATH)

    def test_image_gates_come_from_the_single_storage_source(self):
        """图种 / 大小闸门 = `apps/core/storage` 的既有服务端上限（对标实测值）。

        ⚠️ **不是新口径**：18 MiB 与 `jpg|jpeg|png` 早就落在 `OrderScreenshot` 的认领端点上，
        本模块只是**复用同一个出处**（⛔ 不写第二份定义）。
        ⚠️ 值对值的 `assertEqual` 拦不住「换成一份**字面量副本**」（值相等、却不是同一个对象）
        ⇒ 额外用 **`assertIs`** 钉住「就是那个对象本身」（别名而不是第二份定义）。
        """
        self.assertEqual(jidanpu_upstream_pics.ALLOWED_IMAGE_EXTS, ("jpg", "jpeg", "png"))
        self.assertIs(jidanpu_upstream_pics.ALLOWED_IMAGE_EXTS, ALLOWED_EXTS)
        self.assertEqual(jidanpu_upstream_pics.MAX_SCREENSHOT_BYTES, 18 * 1024 * 1024)
        self.assertIs(jidanpu_upstream_pics.MAX_SCREENSHOT_BYTES, MAX_IMAGE_BYTES)

    def test_multipart_field_order_is_the_verbatim_contract(self):
        """字段顺序（含 `file` 最后）逐字钉住（spec §2.4）。"""
        self.assertEqual(
            tuple(jidanpu_upstream_pics.OSS_FORM_FIELD_ORDER) + (jidanpu_upstream_pics.FILE_FIELD_NAME,),
            FIELD_ORDER,
        )

    def test_oss_failure_is_not_a_jidanpu_failure(self):
        """⭐ spec §13.1-R4：② 那一跳**不是鸡蛋铺端点** ⇒ 两档异常**不同族**、不互相伪装。"""
        err = jidanpu_upstream_pics.OssUploadError("合成")
        self.assertNotIsInstance(err, JidanpuError)
        self.assertNotIsInstance(err, ApiError)
        self.assertNotIsInstance(ApiError("合成", code=0), jidanpu_upstream_pics.OssUploadError)


# ══ `build_object_key`：`${sid}-${orderId}-${rand5}-${epoch}.${ext}` ════════════


class BuildObjectKeyTests(SimpleTestCase):
    def test_key_shape_is_verbatim(self):
        """⭐ spec §2.4 / §11-13：形状**逐字** = `${sid}-${orderId}-${rand5}-${epoch}.${ext}`。"""
        self.assertEqual(
            jidanpu_upstream_pics.build_object_key(
                sid="10001", remote_order_id=REMOTE_ID, ext="jpg",
                rand5="12345", epoch=1790784000,
            ),
            f"10001-{REMOTE_ID}-12345-1790784000.jpg",
        )

    def test_extension_is_lowercased(self):
        self.assertEqual(
            jidanpu_upstream_pics.build_object_key(
                sid="10001", remote_order_id=REMOTE_ID, ext="PNG",
                rand5="12345", epoch=1,
            ),
            f"10001-{REMOTE_ID}-12345-1.png",
        )

    def test_rand5_and_epoch_have_defaults_with_the_expected_shape(self):
        """`rand5` / `epoch` 本地生成（spec §6.3）：5 位随机 + **13 位毫秒**时间戳。"""
        key = jidanpu_upstream_pics.build_object_key(
            sid="10001", remote_order_id=REMOTE_ID, ext="jpeg",
        )
        self.assertRegex(key, rf"^10001-{REMOTE_ID}-\d{{5}}-\d{{13}}\.jpeg$")

    def test_the_default_epoch_is_milliseconds_not_seconds(self):
        """⛔ **毫秒**（对标页面 `new Date().getTime()`）：默认 stamp 必须是 **13 位**。

        失败方向：改回秒级 ⇒ 10 位 ⇒ 同一次 push 的多张图共用同一秒（随机串一撞就
        **OSS 同名覆盖**、上家少一张）⇒ 这一条就是拦它的。
        """
        key = jidanpu_upstream_pics.build_object_key(
            sid="1", remote_order_id="2", ext="jpg", rand5="12345",
        )
        stamp = key.rsplit("-", 1)[1].split(".")[0]
        self.assertRegex(stamp, r"^\d{13}$")
        # 13 位 + 落在「2001 年之后」的毫秒量级（秒级只有 10 位 ⇒ 上一条已拦）。
        self.assertGreater(int(stamp), 10**12)

    def test_an_explicit_epoch_is_passed_through_verbatim(self):
        """显式 `epoch`（毫秒）原样进 key —— 供可复现的测试（spec §2.4 的逐字形状）。"""
        moment = datetime(2026, 9, 28, 12, 0, tzinfo=dt_timezone.utc)
        ms = int(moment.timestamp() * 1000)
        key = jidanpu_upstream_pics.build_object_key(
            sid="1", remote_order_id="2", ext="jpg", rand5="12345", epoch=ms,
        )
        self.assertEqual(key, f"1-2-12345-{ms}.jpg")


# ══ multipart：字段顺序 / `file` 最后（结构性断言）════════════════════════════


class BuildOssMultipartTests(SimpleTestCase):
    FIELDS = {
        "accessid": CANARY_ACCESSID, "host": OSS_HOST, "policy": CANARY_POLICY,
        "signature": CANARY_SIGNATURE, "expire": 1790784240,
    }

    def _build(self, *, data=b"\xff\xd8\xffSYNTH", content_type="image/jpeg"):
        return jidanpu_upstream_pics.build_oss_multipart(
            key="10001-1-12345-1790784000.jpg", fields=self.FIELDS, data=data,
            content_type=content_type,
        )

    def test_file_is_the_last_multipart_field(self):
        """⭐ spec §11-11：编出来的 multipart 体里 `name="file"` **必须是最后一段**。

        失败方向：把 `file` 挪到中间（哪怕业务上「能传上去」）⇒ OSS 会因为 `file` 之后还有
        字段而**静默丢掉**后面那些 —— 这一条就是拦它的。
        ⚠️ 判据锚在**完整的 part 头**上：`filename="…"` 里也含 `name="`，只搜 `name="`
        会数到文件名（计划里那条逐字断言就是这么写的，见 `_PART_NAME_RE` 的注释）。
        """
        _header, body = self._build()
        self.assertLess(body.index(b'name="policy"'), body.index(b'name="file"'))
        self.assertEqual(body.rindex(_PART_HEADER), body.index(_PART_HEADER + b'file"'))

    def test_all_six_parts_are_in_the_contract_order(self):
        _header, body = self._build()
        self.assertEqual(
            [name.decode() for name in _PART_NAME_RE.findall(body)], list(FIELD_ORDER),
        )
        positions = [body.index(_PART_HEADER + name.encode() + b'"') for name in FIELD_ORDER]
        self.assertEqual(positions, sorted(positions))

    def test_policy_fields_are_mapped_to_the_oss_names(self):
        """`accessid → OSSAccessKeyId`、`success_action_status=200`（spec §2.4 的逐字形状）。"""
        _header, body = self._build()
        self.assertIn(b'name="OSSAccessKeyId"', body)
        self.assertIn(CANARY_ACCESSID.encode(), body)
        self.assertIn(b'name="success_action_status"', body)
        self.assertIn(b"\r\n200\r\n", body)

    def test_content_type_header_carries_the_same_boundary_as_the_body(self):
        header, body = self._build()
        self.assertTrue(header.startswith("multipart/form-data; boundary="))
        boundary = header.split("boundary=", 1)[1].encode()
        self.assertIn(b"--" + boundary, body)
        self.assertTrue(body.rstrip().endswith(b"--" + boundary + b"--"))

    def test_file_part_carries_the_bytes_and_the_content_type(self):
        marker = b"\xff\xd8\xffCANARY-BYTES"
        _header, body = self._build(data=marker, content_type="image/png")
        self.assertIn(marker, body)
        self.assertIn(b"Content-Type: image/png", body)

    def test_a_missing_policy_field_is_refused_before_any_upload(self):
        """策略里少一个键 ⇒ 响亮失败（⛔ 不发一个字段残缺的 multipart 上去）。"""
        broken = dict(self.FIELDS)
        del broken["signature"]
        with self.assertRaises(jidanpu_upstream_pics.OssUploadError):
            jidanpu_upstream_pics.build_oss_multipart(
                key="k.jpg", fields=broken, data=b"x", content_type="image/jpeg",
            )


# ══ 默认 `post_to_oss`：真 HTTP 边界（零网络）═════════════════════════════════


class DefaultPostToOssTests(SimpleTestCase):
    FIELDS = BuildOssMultipartTests.FIELDS

    def _post(self, handler, *, data=b"\xff\xd8\xffSYNTH"):
        seen: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        with mock.patch.object(
            jidanpu_upstream_pics.httpx, "Client", _http_factory(wrapped)
        ):
            jidanpu_upstream_pics.post_to_oss(
                OSS_HOST, self.FIELDS, "10001-1-12345-1790784000.jpg", data, "image/jpeg",
            )
        return seen

    def test_the_uploaded_body_really_has_the_contract_shape(self):
        """⭐ 把「编出来的字节」与「真发出去的字节」接上（⛔ 不是只测纯函数）。"""
        seen = self._post(lambda request: httpx.Response(200))
        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), OSS_HOST)
        body = request.content
        self.assertEqual(
            [name.decode() for name in _PART_NAME_RE.findall(body)], list(FIELD_ORDER),
        )
        self.assertEqual(body.rindex(_PART_HEADER), body.index(_PART_HEADER + b'file"'))
        self.assertIn(b"\xff\xd8\xffSYNTH", body)

    def test_no_cookie_and_no_authorization_go_to_the_third_party_host(self):
        """⛔ 换主机不能把鸡蛋铺的会话 cookie / 认证头带过去（这是两个不同的域）。"""
        seen = self._post(lambda request: httpx.Response(200))
        self.assertNotIn("cookie", {k.lower() for k in seen[0].headers})
        self.assertNotIn("authorization", {k.lower() for k in seen[0].headers})

    def test_a_non_200_from_oss_is_an_oss_failure(self):
        def handler(request):
            return httpx.Response(403, text="<Error>合成</Error>")

        with self.assertRaises(jidanpu_upstream_pics.OssUploadError):
            self._post(handler)

    def test_a_transport_error_is_an_oss_failure_not_a_jidanpu_failure(self):
        def handler(request):
            raise httpx.ConnectError("合成：连不上")

        with self.assertRaises(jidanpu_upstream_pics.OssUploadError) as ctx:
            self._post(handler)
        self.assertNotIsInstance(ctx.exception, JidanpuError)

    def test_the_oss_failure_text_never_echoes_the_policy_secrets(self):
        """⭐ spec §11-18：失败文案里**搜不到** `accessid` / `signature` / `policy` 的值。"""

        def handler(request):
            return httpx.Response(403, text="<Error>合成</Error>")

        with self.assertRaises(jidanpu_upstream_pics.OssUploadError) as ctx:
            self._post(handler)
        text = repr(ctx.exception)
        for canary in (CANARY_ACCESSID, CANARY_SIGNATURE, CANARY_POLICY):
            self.assertNotIn(canary, text)


# ══ `ViewPic`：`msg.data` 非空即已有图（⛔ `imgCount` 不是判据）═════════════════


class RemoteHasPicTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username=ACCOUNT, password=PASSWORD)
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account=ACCOUNT, password=PASSWORD,
        )

    def _has(self, envelope):
        client = _StubClient(pic_envelope=envelope)
        return jidanpu_upstream_pics.remote_has_pic(client, remote_order_id=REMOTE_ID), client

    def test_non_empty_msg_data_means_existing_pic(self):
        found, client = self._has({
            "code": 0, "msg": {"title": "", "data": [{"src": "https://oss.test.invalid/a.jpg"}]},
        })
        self.assertTrue(found)
        self.assertEqual(
            [(m, p) for m, p, _kw in client.calls], [("GET", VIEW_PIC_PATH)],
        )
        self.assertEqual(client.calls[0][2]["query"], {"orderId": REMOTE_ID})

    def test_empty_msg_data_means_no_pic(self):
        found, _client = self._has({"code": 0, "msg": {"title": "", "data": []}})
        self.assertFalse(found)

    def test_img_count_zero_does_not_turn_an_existing_pic_into_none(self):
        """⭐ spec §11-12：`msg.data` 非空而 `imgCount=0` ⇒ 仍判**已有图**（实测恒 0）。"""
        found, _client = self._has({
            "code": 0, "imgCount": 0,
            "msg": {"title": "", "data": [{"src": "https://oss.test.invalid/a.jpg"}]},
        })
        self.assertTrue(found)

    def test_a_positive_img_count_is_not_a_judgement_either(self):
        """⭐ 反向那一半：`imgCount>0` 而 `msg.data` 空 ⇒ 仍判**没有图**（⛔ 两个方向都不用 `imgCount`）。"""
        found, _client = self._has({"code": 0, "imgCount": 3, "msg": {"title": "", "data": []}})
        self.assertFalse(found)

    def test_a_missing_msg_data_means_no_pic(self):
        found, _client = self._has({"code": 0, "msg": "success", "data": []})
        self.assertFalse(found)

    def test_a_failure_envelope_without_a_pic_is_a_loud_failure(self):
        """壳里没有图 + 读失败 ⇒ 原样抛（⛔ 不能因为「壳存在」就当成「没有图」）。

        ⚠️ 这一条用的是**桩**（`msg` 是字符串 ⇒ 认不出是 ViewPic 的业务壳）——
        它只说明「非业务壳的失败照旧上行」；「空图壳也照旧上行」那个 bug 由下面
        `RemoteHasPicRealSdkTests` 的**真 SDK** 用例钉住（桩复现不出 SDK 的宽松规则）。
        """
        client = _StubClient(pic_envelope={"code": 0, "msg": "限流中"})
        client.last_envelope = {"code": 0, "msg": "限流中"}
        with mock.patch.object(
            client, "call", side_effect=ApiError("合成：限流中", code=0),
        ):
            with self.assertRaises(ApiError):
                jidanpu_upstream_pics.remote_has_pic(client, remote_order_id=REMOTE_ID)


class RemoteHasPicRealSdkTests(TenantTestCase):
    """⭐ **真 SDK + `MockTransport`**（零网络）：`ViewPic` 的三种形状都要钉。

    ⚠️ 为什么非真 SDK 不可：`remote_has_pic` 的难点**全在 SDK 的宽松规则**上 —— 对
    `{"code":0,"msg":{"title":"","data":[]}}`（`msg` 是**对象**、顶层没有 `data`/`count`/`total`）
    SDK 会判**失败并抛 `ApiError`**，而这**恰恰是 ViewPic 的正常回包**（有图 / 没图都是这个壳）
    ⇒ 用桩冒充的话，验的是桩、不是这条规则（评审实测：三档 = 有图 True / 空图 RAISED / `msg` 字符串 False）。
    """

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username=ACCOUNT, password=PASSWORD)
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account=ACCOUNT, password=PASSWORD,
        )

    def _handler(self, view_pic):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == LOGIN_PATH:
                return httpx.Response(
                    200, json={"code": 1, "msg": "ok", "data": "ok"},
                    headers={"Set-Cookie": "sl-session=synth-pics; Path=/"},
                )
            if path == VIEW_PIC_PATH:
                return view_pic(request)
            raise AssertionError(f"未预期的请求：{request.method} {path}")

        return handler

    def _has(self, handler):
        with mock.patch.object(jidanpu.httpx, "Client", _http_factory(handler)):
            with jidanpu.open_client_with_http(self.binding) as (client, _http):
                return jidanpu_upstream_pics.remote_has_pic(client, remote_order_id=REMOTE_ID)

    def test_a_pic_in_the_shell_is_true(self):
        """形状①：`msg.data` 非空 ⇒ **True**（页面把 `data.msg` 直接喂 `layer.photos`）。"""
        handler = self._handler(lambda _r: httpx.Response(200, json={
            "code": 0, "msg": {"title": "", "data": [{"src": "https://x.invalid/a.jpg"}]},
        }))
        self.assertTrue(self._has(handler))

    def test_an_empty_pic_shell_is_false_not_a_raised_failure(self):
        """⭐ 形状②（**本次修复的那一档**）：`msg.data` 是**空数组** ⇒ **False**，⛔ 不许抛。

        真机「上家还没图」⇒ 正是最常用的那条路（我们去传图）。SDK 的宽松规则把它判成失败，
        而旧代码只在「有图」方向吞异常、其余重抛 ⇒ 这条路**整个走不通**。
        ⚠️ 真机「没图」的确切形状**仓里没有原始记录**（真机未对拍）⇒ 空数组 / 缺 `data` 键
        两种候选形状都在下面各钉一条（`msg` 是对象即认作业务壳）。
        """
        handler = self._handler(lambda _r: httpx.Response(200, json={
            "code": 0, "msg": {"title": "", "data": []},
        }))
        self.assertFalse(self._has(handler))

    def test_an_empty_pic_shell_without_the_data_key_is_false_too(self):
        """形状②的另一种候选：`msg` 是对象但**没有 `data` 键** ⇒ 同样 **False**。"""
        handler = self._handler(lambda _r: httpx.Response(200, json={
            "code": 0, "msg": {"title": ""},
        }))
        self.assertFalse(self._has(handler))

    def test_a_transport_failure_still_raises_loudly(self):
        """形状③：**拿不到业务壳**（连不上）⇒ 原样上行 —— 不知道有没有图就⛔ 不能乱传，
        ⛔ 更不能把「读失败」说成「没有图」。"""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("合成：连不上")

        with self.assertRaises(TransportError):
            self._has(handler)


# ══ `choose_screenshots`：只认订单截图 + `ready`，按 `pk` ═════════════════════


class ChooseScreenshotsTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username=ACCOUNT, password=PASSWORD)
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account=ACCOUNT, password=PASSWORD,
        )
        self.order = Order.objects.create(
            game=Game.objects.create(name="合成游戏-传图"),
            order_no="20260928000000000701", status=OrderStatus.IN_PROGRESS,
        )

    def _shot(self, *, key, img_type=OrderScreenshot.IMG_ORDER,
              status=OrderScreenshot.STATUS_READY):
        return OrderScreenshot.objects.create(
            order=self.order, img_type=img_type, object_key=key, status=status,
            uploaded_by=self.user, remark="合成备注",
        )

    def test_only_order_screenshots_that_are_ready_and_in_pk_order(self):
        """⭐ spec §6.3：`img_type=订单截图`(0) 且 `status=ready`，**按 `pk` 最早在前**。

        ⛔ 群聊二维码（`img_type=1`）与未认领的行**都不许**传上去。
        """
        first = self._shot(key="shared-orders/x/1.jpg")
        self._shot(key="shared-orders/x/qr.jpg", img_type=OrderScreenshot.IMG_GROUP_QR)
        self._shot(key="shared-orders/x/pending.jpg", status=OrderScreenshot.STATUS_PENDING)
        second = self._shot(key="shared-orders/x/2.png")
        chosen = jidanpu_upstream_pics.choose_screenshots(self.order)
        self.assertEqual([row.pk for row in chosen], [first.pk, second.pk])


# ══ 三步 + 短路 + 闸门（主用例）═══════════════════════════════════════════════


class PushOrderScreenshotsTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 9, 28, 12, 0, tzinfo=dt_timezone.utc)
        self.user = User.objects.create_user(username=ACCOUNT, password=PASSWORD)
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account=ACCOUNT, password=PASSWORD,
            enabled=True,
        )
        self.order = Order.objects.create(
            game=Game.objects.create(name="合成游戏-传图"),
            order_no="20260928000000000701", status=OrderStatus.IN_PROGRESS,
        )
        self.link = JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=self.order, upstream_order_id=REMOTE_ID,
        )
        self.http = _StubHttp()
        self.sequence: list[str] = []

    # ── 夹具 ────────────────────────────────────────────────────────────────

    def shot(self, *, key=None, ext="jpg", size=8, remark="合成备注",
             img_type=OrderScreenshot.IMG_ORDER, status=OrderScreenshot.STATUS_READY):
        key = key or f"shared-orders/{self.order.order_no}/shot-{uuid.uuid4().hex[:8]}.{ext}"
        body = (b"\xff\xd8\xff" if ext in ("jpg", "jpeg") else b"\x89PNG\r\n\x1a\n") + b"\0" * max(
            size - 3, 0
        )
        get_storage().put_bytes(key, body, content_type="image/jpeg")
        return OrderScreenshot.objects.create(
            order=self.order, img_type=img_type, object_key=key, status=status,
            size_bytes=len(body), uploaded_by=self.user, remark=remark,
        )

    def push(self, *, shots=None, client=None, oss=None, http=None, pic_envelope=None):
        if shots is not None:
            for shot in shots:
                assert shot.order_id == self.order.pk
        client = client or _StubClient(pic_envelope=pic_envelope)
        oss = oss if oss is not None else _OssRecorder()
        original_call = client.call

        def recording_call(method, path, **kwargs):
            self.sequence.append(f"{method} {path}")
            return original_call(method, path, **kwargs)

        client.call = recording_call

        def recording_oss(*args, **kwargs):
            self.sequence.append("POST <oss>")
            return oss(*args, **kwargs)

        outcome = jidanpu_upstream_pics.push_order_screenshots(
            client, order=self.order, binding=self.binding, link=self.link,
            http=http or self.http, now=self.now, oss_post=recording_oss,
        )
        self.oss = oss
        self.client = client
        return outcome

    # ── ① 三步顺序 + ② `file` 最后 ──────────────────────────────────────────

    def test_three_steps_in_order_and_file_is_the_last_multipart_field(self):
        """⭐ spec §11-11：调用序列 = policy → **OSS 直传** → `UploadOrderPic`；
        且**编出来的 multipart 体里 `name="file"` 必须是最后一段**。"""
        self.shot()
        outcome = self.push()
        self.assertEqual(self.sequence, [
            f"GET {VIEW_PIC_PATH}", f"GET {OSS_POLICY_PATH}", "POST <oss>",
            f"POST {UPLOAD_PIC_PATH}",
        ])
        self.assertEqual(len(outcome.pushed), 1)
        host, fields, key, _data, _ctype = self.oss.calls[0]
        self.assertEqual(host, OSS_HOST)
        body = jidanpu_upstream_pics.build_oss_multipart(
            key=key, fields=fields, data=b"", content_type="image/jpeg",
        )[1]
        self.assertEqual(
            [name.decode() for name in _PART_NAME_RE.findall(body)], list(FIELD_ORDER),
        )
        self.assertEqual(body.rindex(_PART_HEADER), body.index(_PART_HEADER + b'file"'))

    def test_the_key_and_the_register_payload_are_the_verbatim_shapes(self):
        """⭐ spec §2.4 / §11-13 / GC 7：key 拼法、`imgListStr = <host>/<key>`、四个固定值。

        ⚠️ `key` 的时间戳是**毫秒**、以注入的 `now` 为锚点 + 本次 push 内真实流逝的毫秒
        ⇒ 断言「13 位、且 ≥ 锚点、且没跑出这次 push 的合理范围」，⛔ 不钉死一个会变的数。
        """
        shot = self.shot(remark="合成备注")
        outcome = self.push()
        _host, _fields, key, _data, content_type = self.oss.calls[0]
        match = re.fullmatch(rf"{SID_SYNTH}-{REMOTE_ID}-(\d{{5}})-(\d+)\.jpg", key)
        self.assertIsNotNone(match, key)
        self.assertRegex(match.group(2), r"^\d{13}$")
        base_ms = int(self.now.timestamp() * 1000)
        self.assertGreaterEqual(int(match.group(2)), base_ms)
        self.assertLess(int(match.group(2)) - base_ms, 60_000)  # 本次 push 内的真实流逝（毫秒）
        self.assertEqual(content_type, "image/jpeg")
        register = [kw for m, p, kw in self.client.calls if p == UPLOAD_PIC_PATH][0]
        self.assertEqual(register["data"], {
            "orderId": REMOTE_ID,
            "remark": "合成备注",
            "imgListStr": f"{OSS_HOST}/{key}",
            "gameAccId": GAME_ACC_SYNTH,
            "taskId": TASK_SYNTH,
            "imgType": "0",
        })
        # ⚠️ 对象名**不进**人话文案（spec §6.3 隐私）：只在结构化字段里。
        self.assertNotIn(key, outcome.pushed[0].reason)
        self.assertEqual(outcome.pushed[0].oss_key, key)
        self.assertEqual(outcome.pushed[0].object_key, shot.object_key)

    def test_a_failure_envelope_on_register_is_judged_by_our_own_rule(self):
        """⭐ spec §11-9 / E-0087：失败信封必须由**我们的**判据拦下（`code=0` ⇒ 失败）。

        ⚠️ 判据要能分辨**两层**：`register_pic` 显式传了 `success_rule="lenient"` ⇒
        SDK 自己**不再**按 `positive_code` 抛异常，唯一出口就是 `_is_write_success`。
        把 `_is_write_success` 改成 `return True` ⇒ 这一条**必须红**（评审的变异）。
        """
        client = _StubClient(register_code=0, register_msg="还没上传过截图，不能提交完成")
        self.shot()
        outcome = self.push(client=client)
        self.assertEqual([f.step for f in outcome.failed], ["register"])
        self.assertIsInstance(outcome.failed[0].error, ApiError)
        self.assertTrue(outcome.blocks_completion)
        # ⚠️ 这条也钉住「SDK 那一层被显式收手了」：桩收到的调用里带着 `success_rule="lenient"`。
        register = [kw for m, p, kw in client.calls if p == UPLOAD_PIC_PATH][0]
        self.assertEqual(register.get("success_rule"), "lenient")

    def test_each_shot_takes_its_own_millisecond_moment(self):
        """⭐ 逐张取时刻：一批图里每张 key 的时间戳**各自前进**，⛔ 不共用一个 stamp
        （共用 + 5 位随机串一撞 = OSS 同名覆盖、上家少一张）。

        ⚠️ 判据打在**真实流逝的毫秒**上：把模块的 `time` 换成假钟（入口 0ms / 第 1 张 10ms /
        第 2 张 20ms）⇒ 两张的 stamp 必须差 **10**。⛔ 失败方向：改回「一次 push 共用一个
        时刻」⇒ 这一条红。
        """
        self.shot()
        self.shot()
        fake_time = mock.Mock()
        fake_time.monotonic.side_effect = [0.0, 0.010, 0.020]  # 入口 + 两张图
        with mock.patch.object(jidanpu_upstream_pics, "time", fake_time):
            outcome = self.push()
        self.assertEqual(len(outcome.pushed), 2)
        stamps = [int(key.rsplit("-", 1)[1].split(".")[0]) for key in self.oss.keys]
        self.assertEqual(stamps[1] - stamps[0], 10)

    def test_sid_game_acc_and_task_all_come_from_the_upload_page_not_literals(self):
        """⭐ spec §11-13 / T7：三个值**都从上传页隐藏域读**；换一份页面 ⇒ 跟着变。"""
        self.shot()
        self.push()
        _host, _fields, key, _data, _ctype = self.oss.calls[0]
        self.assertTrue(key.startswith(f"{SID_SYNTH}-"))
        register = [kw for m, p, kw in self.client.calls if p == UPLOAD_PIC_PATH][0]
        self.assertEqual(register["data"]["gameAccId"], GAME_ACC_SYNTH)
        self.assertEqual(register["data"]["taskId"], TASK_SYNTH)
        # 换一份上传页（另外两个合成值）⇒ 读出来的必须跟着变（⛔ 写死 `0` 会被这一条抓住）。
        http = _StubHttp(html=_upload_page_html(sid="20002", game_acc_id="11", task_id="12"))
        self.push(http=http)
        _host2, _fields2, key2, _data2, _ctype2 = self.oss.calls[0]
        self.assertTrue(key2.startswith("20002-"))
        register2 = [kw for m, p, kw in self.client.calls if p == UPLOAD_PIC_PATH][0]
        self.assertEqual(register2["data"]["gameAccId"], "11")
        self.assertEqual(register2["data"]["taskId"], "12")

    def test_the_policy_query_is_the_verbatim_one(self):
        """`GET GetAliOssPolicy?bk=1&exsec=240`（spec §2.4 实测）。"""
        self.shot()
        self.push()
        policy_call = [kw for m, p, kw in self.client.calls if p == OSS_POLICY_PATH][0]
        self.assertEqual(policy_call["query"], {"bk": "1", "exsec": "240"})

    def test_upload_page_fields_are_read_from_the_upload_page_endpoint(self):
        self.shot()
        self.push()
        self.assertEqual(self.http.gets[0][0], UPLOAD_PAGE_PATH)
        self.assertEqual(self.http.gets[0][1]["params"], {"orderId": REMOTE_ID})

    # ── 短路：`ViewPic` 的 `msg.data` 非空 ⇒ 零上传 ─────────────────────────

    def test_existing_remote_pic_means_zero_uploads(self):
        """⭐ spec §6.3 / §11-11 后半：`msg.data` 非空 ⇒ **一张都不传**（连策略都不取）。"""
        self.shot()
        outcome = self.push(pic_envelope={
            "code": 0, "msg": {"title": "", "data": [{"src": "https://x.invalid/a.jpg"}]},
        })
        self.assertTrue(outcome.already_had)
        self.assertEqual(outcome.reason, ALREADY_HAD)
        self.assertEqual(self.oss.calls, [])
        self.assertEqual([m for m, _p, _kw in self.client.calls], ["GET"])
        self.assertEqual(self.http.gets, [])
        self.assertFalse(outcome.blocks_completion)

    def test_img_count_is_not_a_judgement(self):
        """⭐ spec §11-12：`msg.data` 非空而 `imgCount=0` ⇒ **仍然不传**。"""
        self.shot()
        outcome = self.push(pic_envelope={
            "code": 0, "imgCount": 0,
            "msg": {"title": "", "data": [{"src": "https://x.invalid/a.jpg"}]},
        })
        self.assertTrue(outcome.already_had)
        self.assertEqual(self.oss.calls, [])

    # ── 闸门：图种 / 大小 ───────────────────────────────────────────────────

    def test_oversized_or_wrong_extension_is_skipped_with_a_reason(self):
        """⭐ spec §6.3：扩展名 ∉ `jpg/jpeg/png`、或单张 > 18MB ⇒ `skipped` + 人话，⛔ 一张都不传。"""
        bad_ext = self.shot(ext="gif")
        oversized = self.shot(size=MAX_IMAGE_BYTES + 1)
        outcome = self.push(shots=[bad_ext, oversized])
        self.assertEqual([s.reason for s in outcome.skipped], [EXT_REASON, SIZE_REASON])
        self.assertEqual(self.oss.calls, [])
        self.assertEqual([m for m, _p, _kw in self.client.calls], ["GET"])  # 只有 ViewPic
        self.assertTrue(outcome.blocks_completion)

    def test_a_shot_at_the_exact_bound_is_allowed(self):
        """边界值：**恰好 18MiB 放行**（`>` 不是 `>=`；E-0021 的边界口径）。"""
        self.shot(size=MAX_IMAGE_BYTES)
        outcome = self.push()
        self.assertEqual(outcome.skipped, ())
        self.assertEqual(len(outcome.pushed), 1)

    def test_a_missing_storage_object_is_a_failure_with_a_reason(self):
        """对象存储里没有这个 key ⇒ `failed`（不是 `skipped`：那是真问题，会挡住提交）。"""
        row = OrderScreenshot.objects.create(
            order=self.order, img_type=OrderScreenshot.IMG_ORDER,
            object_key="shared-orders/not-there/missing.jpg",
            status=OrderScreenshot.STATUS_READY, uploaded_by=self.user,
        )
        outcome = self.push(shots=[row])
        self.assertEqual(len(outcome.failed), 1)
        self.assertEqual(outcome.failed[0].step, "read")
        self.assertEqual(self.oss.calls, [])
        self.assertTrue(outcome.blocks_completion)

    def test_no_local_shot_means_nothing_to_push(self):
        outcome = self.push()
        self.assertEqual(outcome.pushed, ())
        self.assertEqual(outcome.skipped, ())
        self.assertTrue(outcome.blocks_completion)
        self.assertEqual(self.oss.calls, [])
        # ⛔ 没有可传的图就不该去取上传页 / 取策略。
        self.assertEqual([m for m, _p, _kw in self.client.calls], ["GET"])

    def test_group_qr_is_never_uploaded(self):
        """⛔ 群聊二维码（`img_type=1`）**不在**本线的选图范围（spec §6.3）。"""
        self.shot(img_type=OrderScreenshot.IMG_GROUP_QR)
        outcome = self.push()
        self.assertEqual(self.oss.calls, [])
        self.assertTrue(outcome.blocks_completion)

    # ── 失败分档：OSS ≠ 鸡蛋铺 ──────────────────────────────────────────────

    def test_a_failed_oss_upload_is_an_oss_failure(self):
        """⭐ spec §13.1-R4 / §11-11：② 失败 ⇒ 点名**第②步 / 阿里云 / 不经鸡蛋铺**，
        异常类型是 `OssUploadError`（⛔ 不是 `ApiError`），且 `UploadOrderPic` **一次都没发**。"""
        self.shot()
        oss = _OssRecorder(fail_at=1)
        outcome = self.push(oss=oss)
        self.assertEqual([f.step for f in outcome.failed], ["oss"])
        self.assertIsInstance(outcome.failed[0].error, jidanpu_upstream_pics.OssUploadError)
        self.assertNotIsInstance(outcome.failed[0].error, JidanpuError)
        self.assertIn("阿里云 OSS", outcome.failed[0].reason)
        self.assertIn("不经鸡蛋铺", outcome.failed[0].reason)
        self.assertEqual([m for m, p, _kw in self.client.calls if p == UPLOAD_PIC_PATH], [])
        self.assertTrue(outcome.blocks_completion)

    def test_a_failed_policy_is_a_jidanpu_failure_not_an_oss_one(self):
        """⭐ ① 失败 ⇒ 点名第①步（鸡蛋铺）+ `ApiError`，⛔ **不许**伪装成阿里云那条独立故障面。"""
        self.shot()
        client = _StubClient(policy_error=ApiError("合成：policy 取不到", code=0))
        outcome = self.push(client=client)
        self.assertEqual([f.step for f in outcome.failed], ["policy"])
        self.assertIsInstance(outcome.failed[0].error, ApiError)
        self.assertNotIsInstance(outcome.failed[0].error, jidanpu_upstream_pics.OssUploadError)
        self.assertIn("上传签名", outcome.failed[0].reason)
        self.assertEqual(self.oss.calls, [])

    def test_a_failed_register_is_a_jidanpu_failure(self):
        self.shot()
        client = _StubClient(register_error=ApiError("合成：保存失败", code=0))
        outcome = self.push(client=client)
        self.assertEqual([f.step for f in outcome.failed], ["register"])
        self.assertIsInstance(outcome.failed[0].error, ApiError)
        self.assertNotIsInstance(outcome.failed[0].error, jidanpu_upstream_pics.OssUploadError)
        self.assertIn("登记图片", outcome.failed[0].reason)

    def test_the_failure_names_which_shot(self):
        """spec §6.3：失败文案要**点名是哪一步、哪一张**。"""
        self.shot()
        second = self.shot()
        oss = _OssRecorder(fail_at=2)
        outcome = self.push(oss=oss)
        # ⚠️ **对象名不进文案**（spec §6.3 隐私）⇒ 「哪一张」用本地 `pk` 指认。
        self.assertIn(str(second.pk), outcome.failed[0].reason)

    def test_a_register_failure_envelope_is_judged_as_a_failure(self):
        """⭐ spec §11-9 / E-0087：第③步走**真 SDK + `MockTransport`**（零网络）。

        失败信封 `{"code":0,"msg":"…","data":[]}`（**空 data 也算载荷**）会被 SDK 的**宽松**
        解包放行（不抛异常）⇒ 只有我们显式判 `code > 0` 才拦得住。
        ⚠️ `register_pic` 显式传了 `success_rule="lenient"`：SDK 端点清单里
        `UploadOrderPic` 是 `risk="write"`（默认 `positive_code`）会**抢在前面**自己抛异常
        ⇒ 那样我们这层就永不可达。⛔ 失败方向：把 `_is_write_success` 改成 `return True`
        ⇒ 这一条会变成「登记成功、继续提交」（评审实测该变异必须红）。
        """
        self.shot()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == LOGIN_PATH:
                return httpx.Response(
                    200, json={"code": 1, "msg": "ok", "data": "ok"},
                    headers={"Set-Cookie": "sl-session=synth-pics; Path=/"},
                )
            if path == VIEW_PIC_PATH:
                return httpx.Response(200, json={"code": 0, "msg": "success", "data": []})
            if path == OSS_POLICY_PATH:
                return httpx.Response(200, json={"code": 0, "msg": "success", "data": {
                    "accessid": CANARY_ACCESSID, "host": OSS_HOST, "policy": CANARY_POLICY,
                    "signature": CANARY_SIGNATURE, "expire": 1790784240,
                }})
            if path == UPLOAD_PIC_PATH:
                return httpx.Response(200, json={
                    "code": 0, "msg": "还没上传过截图，不能提交完成", "data": [],
                })
            if path == UPLOAD_PAGE_PATH:
                return httpx.Response(200, text=_upload_page_html())
            raise AssertionError(f"未预期的请求：{request.method} {path}")

        oss = _OssRecorder()
        with mock.patch.object(jidanpu.httpx, "Client", _http_factory(handler)):
            with jidanpu.open_client_with_http(self.binding) as (client, http):
                outcome = jidanpu_upstream_pics.push_order_screenshots(
                    client, order=self.order, binding=self.binding, link=self.link,
                    http=http, now=self.now, oss_post=oss,
                )
        self.assertEqual([f.step for f in outcome.failed], ["register"])
        self.assertIsInstance(outcome.failed[0].error, ApiError)
        self.assertEqual(len(oss.calls), 1)  # OSS 那一跳真的发了（这条测的是第③步的判据）
        self.assertTrue(outcome.blocks_completion)

    # ── 凭据不外泄 ─────────────────────────────────────────────────────────

    def test_credentials_never_reach_the_reason_or_the_exception_text(self):
        """⭐ spec §11-18：`accessid` / `signature` / `policy` 的值**不出现在**任何失败文案里。"""
        self.shot()
        oss = _OssRecorder(fail_at=1)
        outcome = self.push(oss=oss)
        blob = repr(outcome) + "".join(f.reason for f in outcome.failed) + repr(
            [f.error for f in outcome.failed]
        )
        for canary in (CANARY_ACCESSID, CANARY_SIGNATURE, CANARY_POLICY):
            self.assertNotIn(canary, blob)

    def test_the_incomplete_upload_page_fields_are_reported(self):
        """上传页隐藏域缺项 ⇒ 用户拿到 §9.2 那句人话（⛔ 不拿空 `sid` 去拼一个必坏的 key）。"""
        self.shot()
        outcome = self.push(http=_StubHttp(html=_upload_page_html(sid=None)))
        self.assertEqual([f.step for f in outcome.failed], ["fields"])
        self.assertIn("取不到上家的店铺标识", outcome.failed[0].reason)
        self.assertEqual(self.oss.calls, [])

    def test_a_no_permission_upload_page_is_a_fields_failure_not_an_exception(self):
        """⭐ spec §9.2 第 4 项：上传页 **302 到无权限页**（**已知业务形态**）⇒
        收成 `failed[step="fields"]` + 不带凭据的人话。⛔ 不许冒泡成异常 ——
        否则同一步（「隐藏域缺值」）会有**两种错误面**，调用方（Task 8）要同时处理两套。
        """
        self.shot()
        http = _StubHttp(status=302, location=jidanpu.NO_PERMISSION_PATH)
        outcome = self.push(http=http)
        self.assertEqual([f.step for f in outcome.failed], ["fields"])
        self.assertIn("取不到上家的店铺标识", outcome.failed[0].reason)
        self.assertIsInstance(outcome.failed[0].error, ApiError)
        self.assertEqual(self.oss.calls, [])
        self.assertTrue(outcome.blocks_completion)

    def test_an_infrastructure_failure_on_the_upload_page_still_raises_loudly(self):
        """基础设施类失败（连不上 / 5xx / 302 到登录页）**仍原样响亮上行** —— 那是
        「检测本身失败」，⛔ 不许伪装成「上家没给店铺标识」（会让人去改一个没坏的权限设置，E-0011）。
        """
        self.shot()

        class _Unreachable(_StubHttp):
            def get(self, path, **kwargs):
                raise httpx.ConnectError("合成：连不上")

        with self.assertRaises(TransportError):
            self.push(http=_Unreachable())
        with self.assertRaises(ServerError):
            self.push(http=_StubHttp(status=503))
        with self.assertRaises(SessionExpiredError):
            self.push(http=_StubHttp(status=302, location=LOGIN_PATH))


# ══ 代练员线：两个新参数（显式远端单号 / 图种）⛔ 老口径一个字都不许变 ══════


class PushNewArgumentsTests(TenantTestCase):
    """`remote_order_id` / `img_types`：新入口的能力 + **老口径的对照**。

    ⚠️ 老口径的**主证**是整个 `PushOrderScreenshotsTests` 那一组（它们一律**不传**这两个
    参数、逐条仍然全绿）；本组补的是「不传时取的仍是链路那一列」「传了时以显式值为准」
    这两条**双向**判据（只钉一侧的话，把默认值改成常量的实现也会全绿）。
    """

    #: 链路列（上家线口径）与显式远端单号用**两个不同的合成值** ⇒ 断言能认出是谁。
    LINK_REMOTE_ID = "20260928000000000711"
    EXPLICIT_REMOTE_ID = "20261005000000000712"

    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 10, 5, 12, 0, tzinfo=dt_timezone.utc)
        self.user = User.objects.create_user(username="pics-newargs", password=PASSWORD)
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account=ACCOUNT, password=PASSWORD,
            enabled=True,
        )
        self.order = Order.objects.create(
            game=Game.objects.create(name="合成游戏-新参数"),
            order_no="20261005000000000712", status=OrderStatus.IN_PROGRESS,
        )
        self.link = JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=self.order, upstream_order_id=self.LINK_REMOTE_ID,
        )
        self.http = _StubHttp()

    def _shot(self, *, ext="jpg", img_type=OrderScreenshot.IMG_ORDER):
        key = f"shared-orders/{self.order.order_no}/shot-{uuid.uuid4().hex[:8]}.{ext}"
        body = (b"\xff\xd8\xff" if ext in ("jpg", "jpeg") else b"\x89PNG\r\n\x1a\n") + b"\0" * 5
        get_storage().put_bytes(key, body, content_type="image/jpeg")
        return OrderScreenshot.objects.create(
            order=self.order, img_type=img_type, object_key=key, status=OrderScreenshot.STATUS_READY,
            size_bytes=len(body), uploaded_by=self.user, remark="合成备注",
        )

    def _push(self, **kwargs):
        client = kwargs.pop("client", None) or _StubClient()
        oss = _OssRecorder()
        outcome = jidanpu_upstream_pics.push_order_screenshots(
            client, order=self.order, binding=self.binding, link=self.link,
            http=self.http, now=self.now, oss_post=oss, **kwargs,
        )
        self.client = client
        self.oss = oss
        return outcome

    def _register_payloads(self):
        return [kw["data"] for _m, path, kw in self.client.calls if path == UPLOAD_PIC_PATH]

    def test_without_the_new_arguments_the_remote_id_still_comes_from_the_link(self):
        """⛔ 老口径：不传 `remote_order_id` ⇒ 取的**仍是** `link.upstream_order_id`。"""
        self._shot()
        outcome = self._push()
        self.assertEqual(len(outcome.pushed), 1)
        self.assertEqual(self._register_payloads()[0]["orderId"], self.LINK_REMOTE_ID)
        self.assertNotEqual(self._register_payloads()[0]["orderId"], self.EXPLICIT_REMOTE_ID)
        self.assertEqual(self._register_payloads()[0]["imgType"], "0")

    def test_the_explicit_remote_id_wins_over_the_link_column(self):
        """代练员线的链路列叫 `jidanpu_order_id` ⇒ 它**显式传**，必须以它为准。"""
        self._shot()
        outcome = self._push(remote_order_id=self.EXPLICIT_REMOTE_ID)
        self.assertEqual(len(outcome.pushed), 1)
        self.assertEqual(self._register_payloads()[0]["orderId"], self.EXPLICIT_REMOTE_ID)

    def test_the_default_image_types_still_exclude_the_group_qr(self):
        """⛔ 老口径：默认只传**订单截图** ⇒ 群聊二维码一行都不许上去。"""
        self._shot()
        self._shot(img_type=OrderScreenshot.IMG_GROUP_QR)
        outcome = self._push()
        self.assertEqual(len(outcome.pushed), 1)
        self.assertEqual(len(self.oss.calls), 1)
        self.assertEqual(self._register_payloads()[0]["imgType"], "0")

    #: 「对面已经有图」的合成壳（载荷在 `msg` 里；`src` 是合成直链）。
    PIC_SHELL = {"code": 0, "msg": {"title": "", "data": [
        {"pid": "0", "IsOpenView": "None", "src": "https://oss-synth.invalid/theirs.jpg",
         "alt": "【机器人01】上传于10-5 09:00，说明：群二维码"},
    ]}}

    def test_the_default_still_short_circuits_when_the_remote_already_has_a_picture(self):
        """⛔ 老口径（默认 `skip_if_remote_has_pic=True`）：对面已有图 ⇒ 一张都不传。

        ⚠️ 这一条与下一条**成对**：把默认值改成 `False` 而没改上游调用点，本条的
        `already_had` / 「register 调用 0 次」两条会红。
        """
        self._shot()
        outcome = self._push(client=_StubClient(pic_envelope=dict(self.PIC_SHELL)))
        self.assertTrue(outcome.already_had)
        self.assertEqual(outcome.pushed, ())
        self.assertEqual(self.oss.calls, [])
        self.assertEqual(self._register_payloads(), [])

    def test_turning_the_short_circuit_off_pushes_anyway_and_skips_the_view_pic_hop(self):
        """⭐ 阻塞 1（2026-10-05 修复轮）：`False` ⇒ 对面已有图**也照传**，且**不打** `ViewPic`。

        代练员线的 A 段就是这一格：业务方要的是「我的图到他那儿」，不是「他那儿有没有图」。
        """
        self._shot()
        client = _StubClient(pic_envelope=dict(self.PIC_SHELL))

        outcome = self._push(client=client, skip_if_remote_has_pic=False)

        self.assertFalse(outcome.already_had)
        self.assertEqual(len(outcome.pushed), 1)
        self.assertEqual([p["imgType"] for p in self._register_payloads()], ["0"])
        self.assertEqual(
            [path for _m, path, _kw in client.calls if path == VIEW_PIC_PATH], [],
        )

    def test_both_image_types_carry_their_own_img_type(self):
        """「全传」时**逐张**带上真实图种：订单截图 `"0"` / 群二维码 `"1"`。

        出处 = 对标上传页的 radio（`.tools/recon/media-storage/Order__UploadPic.html`）。
        """
        self._shot(img_type=OrderScreenshot.IMG_ORDER)
        self._shot(img_type=OrderScreenshot.IMG_GROUP_QR)
        outcome = self._push(
            remote_order_id=self.EXPLICIT_REMOTE_ID,
            img_types=(OrderScreenshot.IMG_ORDER, OrderScreenshot.IMG_GROUP_QR),
        )
        self.assertEqual(len(outcome.pushed), 2)
        self.assertEqual(
            [payload["imgType"] for payload in self._register_payloads()], ["0", "1"],
        )
        self.assertEqual(
            [payload["orderId"] for payload in self._register_payloads()],
            [self.EXPLICIT_REMOTE_ID, self.EXPLICIT_REMOTE_ID],
        )


# ══ 拉回来（B 段）——`fetch_remote_pics` 的信封形态 ═════════════════════


def _view_pic_shell(data):
    """真机形状的业务壳（载荷在 `msg` 里；spec §0.2 的实测样本）。"""
    return {"code": 0, "msg": {"start": 0, "title": None, "id": 0, "data": list(data)}}


def _raising_call(envelope):
    """照**真实 SDK** 那一档：`code==0` + `msg` 是对象 ⇒ `call` 抛 `ApiError`、但壳还在。

    ⚠️ 桩只用来钉「我们自己怎么解读壳」，SDK 的宽松规则本身由
    `RemoteHasPicRealSdkTests` 的真 SDK 用例钉（E-0029 的同族：桩接没接上要有对照）。
    """
    client = _StubClient(pic_envelope=envelope)
    client.last_envelope = envelope
    return client, mock.patch.object(
        client, "call", side_effect=ApiError("合成：ViewPic code=0", code=0),
    )


class FetchRemotePicsTests(SimpleTestCase):
    """`fetch_remote_pics`：**两种信封形态都认** + 空 + 形状读不出 + 非业务壳照旧抛。"""

    PIC = {"pid": "0", "IsOpenView": "None", "alt": "合成说明：群二维码",
           "src": "https://oss-synth.invalid/a.jpg", "thumb": "https://oss-synth.invalid/a.thumb"}

    def test_a_lenient_success_envelope_yields_the_items(self):
        """形态①：`code>0` 正常返回（未登记端点的宽松规则）⇒ 逐条解出来。"""
        envelope = {"code": 1, "msg": {"start": 0, "title": None, "id": 0,
                                       "data": [dict(self.PIC)]}}
        client = _StubClient(pic_envelope=envelope)
        client.last_envelope = envelope
        pics = jidanpu_upstream_pics.fetch_remote_pics(client, remote_order_id=REMOTE_ID)
        self.assertEqual(pics.rows, 1)
        self.assertEqual([p.src for p in pics.items], [self.PIC["src"]])
        self.assertEqual([p.alt for p in pics.items], [self.PIC["alt"]])
        self.assertTrue(pics)

    def test_the_code_zero_envelope_with_pics_is_recognised(self):
        """形态②：`code==0` ⇒ SDK 抛 `ApiError`，但**载荷在 `msg` 里** ⇒ 照常解出来。"""
        client, patcher = _raising_call(_view_pic_shell([dict(self.PIC)]))
        with patcher:
            pics = jidanpu_upstream_pics.fetch_remote_pics(client, remote_order_id=REMOTE_ID)
        self.assertEqual(pics.rows, 1)
        self.assertEqual([p.src for p in pics.items], [self.PIC["src"]])

    def test_an_empty_shell_is_not_a_failure(self):
        """形态③：`msg.data` 空 ⇒ **没图**（`rows=0`、`items` 空、⛔ 不抛）。"""
        client, patcher = _raising_call(_view_pic_shell([]))
        with patcher:
            pics = jidanpu_upstream_pics.fetch_remote_pics(client, remote_order_id=REMOTE_ID)
        self.assertEqual((pics.rows, pics.items), (0, ()))
        self.assertFalse(pics)

    def test_rows_without_a_usable_src_are_kept_counted_as_rows(self):
        """⚠️ 形状判据：`data` 有 2 条但都读不出地址 ⇒ `rows=2` 而 `items` 空
        （⇒ 调用方可以把这个**与「一张图都没有」分开**，⛔ 不许静默当没图）。"""
        client, patcher = _raising_call(_view_pic_shell([{"pid": "0"}, "不是对象"]))
        with patcher:
            pics = jidanpu_upstream_pics.fetch_remote_pics(client, remote_order_id=REMOTE_ID)
        self.assertEqual((pics.rows, pics.items), (2, ()))
        self.assertFalse(pics)

    def test_a_failure_envelope_that_is_not_this_endpoint_still_raises(self):
        """⛔ 拿不到业务壳（`msg` 是字符串）⇒ 原样上行：不知道有没有图就不能当没图。"""
        client = _StubClient(pic_envelope={"code": 0, "msg": "限流中"})
        client.last_envelope = {"code": 0, "msg": "限流中"}
        with mock.patch.object(client, "call", side_effect=ApiError("合成：限流中", code=0)):
            with self.assertRaises(ApiError):
                jidanpu_upstream_pics.fetch_remote_pics(client, remote_order_id=REMOTE_ID)

    def test_it_asks_for_the_given_order(self):
        """请求形状：`GET /OrderAjax/ViewPic?orderId=<给的那个号>`（⛔ 不另加参数）。"""
        client = _StubClient(pic_envelope=_view_pic_shell([]))
        jidanpu_upstream_pics.fetch_remote_pics(client, remote_order_id="REMOTE-SYNTH-1")
        self.assertEqual(client.calls[0][0], "GET")
        self.assertEqual(client.calls[0][1], VIEW_PIC_PATH)
        self.assertEqual(client.calls[0][2]["query"], {"orderId": "REMOTE-SYNTH-1"})


# ══ `img_type` / 确定性对象名 / 下载那一跳 ════════════════════════════


class AltToImgTypeTests(SimpleTestCase):
    """`img_type_of_alt`：判据只看 `说明：` 之后那一段（⛔ 不被上传者名字带偏）。"""

    def test_the_note_after_the_marker_decides(self):
        self.assertEqual(
            jidanpu_upstream_pics.img_type_of_alt("【群主小张】上传于10-4 21:53，说明：订单截图"),
            OrderScreenshot.IMG_ORDER,
        )
        self.assertEqual(
            jidanpu_upstream_pics.img_type_of_alt("【机器人01】上传于10-4 21:53，说明：群二维码"),
            OrderScreenshot.IMG_GROUP_QR,
        )

    def test_without_the_marker_the_whole_alt_is_judged(self):
        """没有 `说明：` ⇒ 整串回退（**只会多认成二维码**：那一档落在本租户前缀下、⛔ 不进共享段）。"""
        self.assertEqual(
            jidanpu_upstream_pics.img_type_of_alt("【机器人01】上传于10-4 21:53"),
            OrderScreenshot.IMG_ORDER,
        )
        self.assertEqual(
            jidanpu_upstream_pics.img_type_of_alt("【群二维码】"), OrderScreenshot.IMG_GROUP_QR,
        )

    def test_a_non_string_alt_is_an_order_screenshot(self):
        self.assertEqual(
            jidanpu_upstream_pics.img_type_of_alt(None), OrderScreenshot.IMG_ORDER,
        )


class StableIdentityTests(SimpleTestCase):
    """`stable_pic_identity`：丢掉查询串 / 片段（实测直链带 `?x-oss-process=style/…`）。"""

    def test_the_style_query_is_dropped(self):
        plain = "https://oss-synth.invalid/1-2-3.png"
        styled = "https://oss-synth.invalid/1-2-3.png?x-oss-process=style/geshi"
        self.assertEqual(
            jidanpu_upstream_pics.stable_pic_identity(styled),
            jidanpu_upstream_pics.stable_pic_identity(plain),
        )
        self.assertEqual(jidanpu_upstream_pics.stable_pic_identity(plain), plain)

    def test_the_fragment_is_dropped_too(self):
        self.assertEqual(
            jidanpu_upstream_pics.stable_pic_identity("https://oss-synth.invalid/a.jpg#x"),
            "https://oss-synth.invalid/a.jpg",
        )


class BuildInboundObjectKeyTests(TenantTestCase):
    """确定性对象名：同一张图恒得同一个 key；两种图种各走各的前缀（照认领端点的 builder）。"""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="pics-key", password=PASSWORD)
        self.order = Order.objects.create(
            game=Game.objects.create(name="合成游戏-键"),
            order_no="20261005000000000713", status=OrderStatus.IN_PROGRESS,
        )

    def test_the_same_source_url_yields_the_same_key(self):
        first = jidanpu_upstream_pics.build_inbound_object_key(
            order=self.order, img_type=OrderScreenshot.IMG_ORDER,
            src="https://oss-synth.invalid/1-2-3.png", ext="png",
        )
        second = jidanpu_upstream_pics.build_inbound_object_key(
            order=self.order, img_type=OrderScreenshot.IMG_ORDER,
            src="https://oss-synth.invalid/1-2-3.png?x-oss-process=style/geshi", ext="png",
        )
        self.assertEqual(first, second)
        self.assertTrue(first.startswith(f"shared-orders/{self.order.order_no}/"), first)
        self.assertTrue(first.endswith(".png"), first)
        self.assertEqual(len(first.split("/")[-1].split(".")[0]), 32)

    def test_a_different_source_url_yields_a_different_key(self):
        one = jidanpu_upstream_pics.build_inbound_object_key(
            order=self.order, img_type=OrderScreenshot.IMG_ORDER,
            src="https://oss-synth.invalid/1.jpg", ext="jpg",
        )
        two = jidanpu_upstream_pics.build_inbound_object_key(
            order=self.order, img_type=OrderScreenshot.IMG_ORDER,
            src="https://oss-synth.invalid/2.jpg", ext="jpg",
        )
        self.assertNotEqual(one, two)

    def test_the_group_qr_uses_the_shared_prefix(self):
        """⭐ D-0316（2026-10-27）：拉回的群聊二维码也走**共享段**（否则它永远不同步）。

        ⚠️ 旧口径（R-P4-7「二维码留在本租户前缀」）已废除；`digest` 仍是确定性去重身份
        ⇒ 同一张源图仍然产出同一个 key。
        """
        key = jidanpu_upstream_pics.build_inbound_object_key(
            order=self.order, img_type=OrderScreenshot.IMG_GROUP_QR,
            src="https://oss-synth.invalid/qr.png", ext="png",
        )
        self.assertTrue(
            key.startswith(f"shared-orders/{self.order.order_no}/qr."), key
        )
        self.assertRegex(key, r"/qr\.[0-9a-f]{32}\.png$")
        again = jidanpu_upstream_pics.build_inbound_object_key(
            order=self.order, img_type=OrderScreenshot.IMG_GROUP_QR,
            src="https://oss-synth.invalid/qr.png", ext="png",
        )
        self.assertEqual(key, again)


#: 一个**公网**地址（TEST-NET-2 之外、合成场景用；只做「解析结果」的夹具，⛔ 不真连）。
PUBLIC_ADDR = "93.184.216.34"


class _FakeFetched:
    """`download_pic` 的注入桩响应（形状照 `_Fetched`：status / headers / chunks / close）。"""

    def __init__(self, status=200, headers=None, chunks=()):
        self.status = status
        self.headers = dict(headers or {})
        self.chunks = iter(chunks)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class PicDownloadSsrfTests(SimpleTestCase):
    """⭐ H-2（2026-10-26）：scheme / 凭据 / 逐地址内网拒绝 / 重定向逐跳复检 / 20MiB 上限。

    ⚠️ **两层都注入**：`resolver`（DNS）与 `fetch`（HTTP）都是替身 ⇒ 整套用例**零真实网络**
    （既不查 DNS、也不建 TCP 连接）。
    """

    SRC = "https://oss-synth.invalid/a.jpg?signature=CANARY-SIGNATURE"

    def _resolve(self, *addresses):
        return mock.Mock(return_value=tuple(addresses))

    def _fetcher(self, *responses):
        calls: list[str] = []

        def fetch(url, *, timeout):
            calls.append(url)
            spec = responses[len(calls) - 1]
            if isinstance(spec, BaseException):
                raise spec
            return spec

        return fetch, calls

    def _download(self, *, resolver=None, fetch=None, src=None):
        return jidanpu_upstream_pics.download_pic(
            src or self.SRC,
            resolver=resolver or self._resolve(PUBLIC_ADDR),
            fetch=fetch,
        )

    def _assert_rejected(self, src, *, addresses=(PUBLIC_ADDR,), fragment):
        fetch, calls = self._fetcher(_FakeFetched(chunks=(b"x",)))
        with self.assertRaises(jidanpu_upstream_pics.PicDownloadError) as caught:
            self._download(resolver=self._resolve(*addresses), fetch=fetch, src=src)
        self.assertIn(fragment, str(caught.exception))
        self.assertEqual(calls, [], "闸门没过却已经发请求了")
        # ⛔ 文案里不许回显 URL / 主机 / 地址（查询串里是签名）。
        self.assertNotIn("oss-synth.invalid", str(caught.exception))
        self.assertNotIn("CANARY", str(caught.exception))

    def test_only_http_and_https_are_allowed(self):
        for src in (
            "ftp://oss-synth.invalid/a.jpg",
            "file:///etc/passwd",
            "gopher://oss-synth.invalid/a",
        ):
            with self.subTest(src=src):
                self._assert_rejected(src, fragment="http/https")

    def test_a_url_with_credentials_is_rejected(self):
        self._assert_rejected(
            "https://user:pass@oss-synth.invalid/a.jpg",
            fragment="用户名或密码",
        )

    def test_every_resolved_address_must_be_public(self):
        """逐地址拒：环回 / 私有 / 链路本地 / 保留 / CGNAT / 组播 / 未指定 一个都不放过。"""
        cases = {
            "127.0.0.1": "内网 / 本机 / 保留地址",
            "10.0.0.1": "内网 / 本机 / 保留地址",
            "192.168.1.1": "内网 / 本机 / 保留地址",
            "169.254.169.254": "内网 / 本机 / 保留地址",
            "100.64.0.1": "内网 / 本机 / 保留地址",       # RFC 6598（CGNAT）
            "0.0.0.0": "内网 / 本机 / 保留地址",
            "224.0.0.1": "内网 / 本机 / 保留地址",         # 组播（is_global 实测为真，单独拒）
            "::1": "内网 / 本机 / 保留地址",
            "fe80::1": "内网 / 本机 / 保留地址",
            "2001:db8::1": "内网 / 本机 / 保留地址",
        }
        for address, fragment in cases.items():
            with self.subTest(address=address):
                self._assert_rejected(self.SRC, addresses=(address,), fragment=fragment)

    def test_a_mixed_answer_is_rejected_even_if_one_address_is_public(self):
        """同时解析出公网 + 内网 ⇒ fail-closed（连哪一个不由我们定）。"""
        self._assert_rejected(
            self.SRC, addresses=(PUBLIC_ADDR, "10.0.0.1"), fragment="内网 / 本机 / 保留地址"
        )

    def test_a_redirect_is_followed_after_revalidating_the_hop(self):
        """正常 302 到公网地址 ⇒ 跟过去拿到字节（重定向本身**不是**被禁的功能）。"""
        fetch, calls = self._fetcher(
            _FakeFetched(status=302, headers={"location": "https://cdn-synth.invalid/b.jpg"}),
            _FakeFetched(chunks=(b"abc",)),
        )
        data = self._download(fetch=fetch)
        self.assertEqual(data, b"abc")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].startswith("https://cdn-synth.invalid/"))

    def test_a_redirect_to_a_private_address_is_rejected_before_the_second_request(self):
        """⭐ 逐跳复检：302 指向内网 ⇒ **第二跳一个请求都不发**。"""
        fetch, calls = self._fetcher(
            _FakeFetched(status=302, headers={"location": "http://169.254.169.254/latest/meta-data"}),
            _FakeFetched(chunks=(b"metadata",)),
        )
        resolver = mock.Mock(side_effect=lambda host, port: (PUBLIC_ADDR,))
        with self.assertRaises(jidanpu_upstream_pics.PicDownloadError) as caught:
            jidanpu_upstream_pics.download_pic(self.SRC, resolver=resolver, fetch=fetch)
        self.assertIn("内网 / 本机 / 保留地址", str(caught.exception))
        self.assertEqual(len(calls), 1, "重定向到内网还是被跟过去了")

    def test_at_most_three_redirects(self):
        """重定向 ≤ 3：第 4 个 3xx 判失败（⛔ 无限跟）。"""
        redirect = _FakeFetched(status=302, headers={"location": "/next"})
        fetch, calls = self._fetcher(redirect, redirect, redirect, redirect)
        with self.assertRaises(jidanpu_upstream_pics.PicDownloadError) as caught:
            self._download(fetch=fetch)
        self.assertIn("重定向超过 3 跳", str(caught.exception))
        self.assertEqual(len(calls), 4)

    def test_the_stream_is_capped_at_twenty_mib(self):
        """流式累计超上限 ⇒ 立刻失败（⛔ 不先读完再看大小）。"""
        with mock.patch.object(jidanpu_upstream_pics, "MAX_PIC_BYTES", 10):
            fetch, _calls = self._fetcher(_FakeFetched(chunks=(b"12345", b"67890", b"x")))
            with self.assertRaises(jidanpu_upstream_pics.PicDownloadError) as caught:
                self._download(fetch=fetch)
        self.assertIn("MiB", str(caught.exception))

    def test_an_http_error_from_the_fetcher_is_a_download_failure(self):
        fetch, _calls = self._fetcher(httpx.ConnectError("合成：连不上"))
        with self.assertRaises(jidanpu_upstream_pics.PicDownloadError) as caught:
            self._download(fetch=fetch)
        self.assertIn("ConnectError", str(caught.exception))
        self.assertNotIn("oss-synth.invalid", str(caught.exception))


class DownloadPicTests(SimpleTestCase):
    """`download_pic`：真 `httpx.Client` + `MockTransport`（零网络），并证明它**不带会话**。

    ⚠️ H-2：下载前会**解析主机并逐地址判公网** ⇒ 本类把 `_resolve_host`
    打桩成固定公网地址（`PUBLIC_ADDR`），⛔ 真 DNS 都不打；SSRF 那几档的解析由
    `PicDownloadSsrfTests` 注入（见那个类）。
    """

    SRC = "https://oss-synth.invalid/a.jpg?accessid=CANARY-ACCESSID&signature=CANARY-SIGNATURE"
    BODY = b"\xff\xd8\xff" + b"\0" * 8

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            jidanpu_upstream_pics, "_resolve_host", return_value=(PUBLIC_ADDR,)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _download(self, handler, src=None):
        with mock.patch.object(httpx, "Client", _http_factory(handler)):
            return jidanpu_upstream_pics.download_pic(src or self.SRC)

    def _timeout_used_by_the_download(self):
        """跑一次**真** `download_pic`，回它给 `httpx.Client(...)` 的 `timeout` 参数。

        ⚠️ 判据打在**真实出口**上（`httpx.Client` 收到的 kwargs），⛔ 不是「模块里有个常量 = 45」
        ——「常量对了但没被用过」那种改法同样会让页面拉不到图。
        """
        seen = {}

        def factory(**kwargs):
            seen["timeout"] = kwargs.get("timeout")
            kwargs.pop("transport", None)
            return _REAL_HTTPX_CLIENT(
                transport=httpx.MockTransport(
                    lambda _r: httpx.Response(200, content=self.BODY)
                ),
                **kwargs,
            )

        with mock.patch.object(httpx, "Client", factory):
            jidanpu_upstream_pics.download_pic(self.SRC)
        return seen["timeout"]

    def test_a_missing_setting_defaults_the_download_timeout_to_sixty(self):
        """设置**缺失** ⇒ 60 秒（业务方 2026-10-23 口径）。"""
        with override_settings():
            del settings.JIDANPU_IMAGE_TIMEOUT
            self.assertEqual(jidanpu_upstream_pics.pic_timeout_seconds(), 60)
            self.assertEqual(self._timeout_used_by_the_download(), 60)

    def test_a_broken_setting_falls_back_to_sixty_with_a_warning(self):
        """非整数 / ≤ 0 ⇒ 回落 60 **并记 warning**（⛔ 不静默按 0：那会让每次下载立刻超时）。"""
        for raw in ("abc", "0", "-5"):
            with self.subTest(raw=raw), override_settings(JIDANPU_IMAGE_TIMEOUT=raw):
                with self.assertLogs(
                    "apps.business.jidanpu_upstream_pics", level="WARNING"
                ):
                    self.assertEqual(jidanpu_upstream_pics.pic_timeout_seconds(), 60)

    def test_the_configured_timeout_reaches_the_httpx_client(self):
        """配置 **45** ⇒ 下载那一跳真的收到 45（⛔ 不是只断言常量）。"""
        with override_settings(JIDANPU_IMAGE_TIMEOUT="45"):
            self.assertEqual(jidanpu_upstream_pics.pic_timeout_seconds(), 45)
            self.assertEqual(self._timeout_used_by_the_download(), 45)

    def test_the_image_setting_does_not_touch_the_json_api_hop(self):
        """图片下载的设置**只影响下载那一跳**：鸡蛋铺 JSON API 仍是 `JidanpuConfig.timeout`（30）。

        判据打在 SDK 传输层**真正建出来的** `httpx.Client` 上（不是 `JidanpuConfig` 的默认值）：
        把 `JIDANPU_IMAGE_TIMEOUT` 设成 45、跑一次 `Transport.request`，它收到的仍是 30。
        """
        config = JidanpuConfig(
            base_url="https://jidanpu-api.test.invalid", account="a", password="p",
        )
        seen = {}

        def factory(**kwargs):
            seen["timeout"] = kwargs.get("timeout")
            kwargs.pop("transport", None)
            return _REAL_HTTPX_CLIENT(
                transport=httpx.MockTransport(
                    lambda _r: httpx.Response(
                        200, json={"code": 1, "msg": "", "data": {}}
                    )
                ),
                **kwargs,
            )

        with override_settings(JIDANPU_IMAGE_TIMEOUT="45"):
            with mock.patch.object(httpx, "Client", factory):
                transport = Transport(config)
                transport.request("GET", "/OrderAjax/ViewPic")
                transport.close()
        self.assertEqual(seen["timeout"], 30)

    def test_a_200_with_bytes_is_returned_and_the_hop_carries_no_session(self):
        seen = {}

        def handler(request):
            seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
            seen["url"] = str(request.url)
            return httpx.Response(200, content=self.BODY)

        data = self._download(handler)
        self.assertEqual(data, self.BODY)
        self.assertEqual(seen["url"], self.SRC)
        # ⛔ 绝不许把鸡蛋铺的会话带去第三方主机（照 `post_to_oss` 的同一条站姿）。
        for name in ("cookie", "authorization"):
            self.assertNotIn(name, seen["headers"])

    def test_a_non_200_is_a_download_failure_without_the_url_in_the_text(self):
        data = {"status": 403}

        def handler(_request):
            return httpx.Response(data["status"], content=b"")

        with self.assertRaises(jidanpu_upstream_pics.PicDownloadError) as caught:
            self._download(handler)
        self.assertIn("403", str(caught.exception))
        self.assertNotIn("CANARY", str(caught.exception))
        self.assertNotIn("oss-synth.invalid", str(caught.exception))

    def test_zero_bytes_is_a_download_failure(self):
        with self.assertRaises(jidanpu_upstream_pics.PicDownloadError):
            self._download(lambda _r: httpx.Response(200, content=b""))

    def test_a_transport_error_is_a_download_failure(self):
        def handler(_request):
            raise httpx.ConnectError("合成：连不上 https://oss-synth.invalid/a.jpg")

        with self.assertRaises(jidanpu_upstream_pics.PicDownloadError) as caught:
            self._download(handler)
        self.assertNotIn("CANARY", str(caught.exception))
        self.assertNotIn("oss-synth.invalid", str(caught.exception))


class ViewPicTimeoutTests(SimpleTestCase):
    """**读图列表（ViewPic）**单独一档超时 + 共 2 次尝试（2026-10-23 业务方拍板）。

    判据（逐条对上任务书）：

    1. 默认 60；设置缺失 ⇒ 60；
    2. 非法 / ≤ 0 ⇒ 回落 60 + warning（⛔ 不静默按 0）；
    3. 配置的值真的进了那一发请求的 `timeout`（httpx 的 per-request 扩展，⛔ 不断常量）；
    4. **只影响 ViewPic**：其它 JSON API 仍是 30 秒 / 全局 3 次尝试；
    5. ViewPic 的重试次数 = 2（用桩数**请求次数**，⛔ 不断常量）。

    ⚠️ 这些用例用的是**真** `JidanpuClient` + 真 `httpx.Client`（`MockTransport`，零网络），
    并手工挂上本仓建连处会挂的 `_tt_http`（`apps/business/jidanpu.py` 的两个落点）。
    """

    BASE = "https://viewpic-timeout.test.invalid"

    class _Auth:
        """桩认证：已登录标记；⛔ 不真的打 `/Login/Login`（这些用例只测超时/次数）。"""

        def __init__(self):
            self.calls = 0

        def get_token(self, *, force: bool = False) -> str:
            self.calls += 1
            return "synth-session-marker"

        def invalidate(self) -> None:
            return None

    def _client(self, handler):
        http = _REAL_HTTPX_CLIENT(
            transport=httpx.MockTransport(handler),
            timeout=30,
            follow_redirects=False,
        )
        config = JidanpuConfig(base_url=self.BASE, account="synth-a", password="synth-p")
        client = JidanpuClient(config, http_client=http, auth=self._Auth())
        # 本仓建连处的两个落点都会挂这一格（`jidanpu.open_client` / `_open_client_pair`）。
        client._tt_http = http
        return client

    @staticmethod
    def _read_timeout(request):
        """httpx 把 per-request timeout 放在 `extensions["timeout"]`（dict 形状）。"""
        raw = request.extensions.get("timeout")
        return raw["read"] if isinstance(raw, dict) else raw.read

    @staticmethod
    def _empty_view_pic(_request):
        return httpx.Response(
            200, json={"code": 0, "msg": {"title": "", "data": []}}
        )

    def test_a_missing_setting_defaults_to_sixty_seconds(self):
        with override_settings():
            del settings.JIDANPU_VIEWPIC_TIMEOUT
            self.assertEqual(jidanpu_upstream_pics.viewpic_timeout_seconds(), 60)

    def test_a_broken_setting_falls_back_to_sixty_with_a_warning(self):
        for raw in ("abc", "0", "-5"):
            with self.subTest(raw=raw), override_settings(JIDANPU_VIEWPIC_TIMEOUT=raw):
                with self.assertLogs(
                    "apps.business.jidanpu_upstream_pics", level="WARNING"
                ):
                    self.assertEqual(jidanpu_upstream_pics.viewpic_timeout_seconds(), 60)

    def test_the_default_timeout_reaches_the_viewpic_request(self):
        """设置缺失 ⇒ 那一发 ViewPic 的 per-request timeout 是 60（真出口，不是常量）。"""
        seen = []

        def handler(request):
            seen.append(self._read_timeout(request))
            return self._empty_view_pic(request)

        with override_settings():
            del settings.JIDANPU_VIEWPIC_TIMEOUT
            pics = jidanpu_upstream_pics.fetch_remote_pics(
                self._client(handler), remote_order_id=REMOTE_ID,
            )
        self.assertFalse(pics)
        self.assertEqual(seen, [60])

    def test_the_configured_timeout_reaches_the_viewpic_request(self):
        """设置 45 ⇒ 那一发 ViewPic 真的收到 45。"""
        seen = []

        def handler(request):
            seen.append(self._read_timeout(request))
            return self._empty_view_pic(request)

        with override_settings(JIDANPU_VIEWPIC_TIMEOUT="45"):
            jidanpu_upstream_pics.fetch_remote_pics(
                self._client(handler), remote_order_id=REMOTE_ID,
            )
        self.assertEqual(seen, [45])

    def test_remote_has_pic_goes_through_the_same_viewpic_hop(self):
        """`remote_has_pic`（传图前的短路）**同一个** ViewPic 口 ⇒ 同样是这一档超时。"""
        seen = []

        def handler(request):
            seen.append(self._read_timeout(request))
            return httpx.Response(
                200,
                json={"code": 0, "msg": {"data": [{"src": "https://oss-synth.invalid/a.jpg"}]}},
            )

        has_pic = jidanpu_upstream_pics.remote_has_pic(
            self._client(handler), remote_order_id=REMOTE_ID,
        )
        self.assertTrue(has_pic)
        self.assertEqual(seen, [60])

    def test_viewpic_is_attempted_twice(self):
        """ViewPic 超时 ⇒ **2 次尝试**（多给一次重试），走既有传输层退避。"""
        calls = []

        def handler(request):
            calls.append(request.url.path)
            raise httpx.ReadTimeout("合成：读图列表超时")

        with mock.patch("jidanpu.transport.time.sleep"):
            with self.assertRaises(JidanpuTimeoutError):
                jidanpu_upstream_pics.fetch_remote_pics(
                    self._client(handler), remote_order_id=REMOTE_ID,
                )
        self.assertEqual(calls, [VIEW_PIC_PATH, VIEW_PIC_PATH])

    def test_other_json_apis_keep_thirty_seconds_and_the_global_retry_ladder(self):
        """**只影响 ViewPic**：同一条客户端上的其它 API 仍是 30 秒 / 3 次尝试。"""
        seen = []

        def handler(request):
            seen.append((request.url.path, self._read_timeout(request)))
            raise httpx.ReadTimeout("合成：读图列表超时（非 ViewPic 不该走到这里）")

        client = self._client(handler)
        with mock.patch("jidanpu.transport.time.sleep"):
            with self.assertRaises(JidanpuTimeoutError):
                client.call("GET", "/OrderAjax/GetMyOrderList")
        self.assertEqual([path for path, _t in seen], ["/OrderAjax/GetMyOrderList"] * 3)
        self.assertEqual([t for _p, t in seen], [30, 30, 30])

    def test_the_production_entry_points_expose_the_http_handle(self):
        """**建连线的判据**：本仓两个生产建连口都把 `_tt_http` 挂上（否则整套会静默退回 30 秒）。

        ⚠️ 前面那些用例是**手工**挂 `_tt_http` 的，只证明「挂上以后行为对」；这一条证明
        `jidanpu.open_client`（下家线 / 请求路径）与 `open_client_with_http`（上家线池）
        这两个真实入口**真的挂了**。零网络：建客户端不登录、不发请求。
        """
        binding = JidanpuUpstreamBinding(
            jidanpu_base_url=self.BASE, account="synth-a", password="synth-p",
        )
        with jidanpu.open_client(binding) as client:
            self.assertIsNotNone(
                getattr(client, "_tt_http", None),
                "open_client 没挂 _tt_http ⇒ ViewPic 会静默退回 30 秒 / 3 次尝试",
            )
        with jidanpu.open_client_with_http(binding) as (client, http):
            self.assertIs(
                client._tt_http, http,
                "open_client_with_http 挂的不是那一份 httpx 客户端（会话 / 连接池会分叉）",
            )


# ══ `pull_order_screenshots`（下家 → 我方入库）══════════════════════════

#: 拉图用例的合成单号 / 源直链（**合成值**；真机读数只留在不进 git 的 `.tools/recon/`）。
PULL_ORDER_NO = "20261005000000000714"
PULL_SRC_A = "https://oss-synth.invalid/pull-a.jpg"
PULL_SRC_QR = "https://oss-synth.invalid/pull-qr.png"
PULL_ALT_SHOT = "【机器人01】上传于10-5 09:10，说明：订单截图"
PULL_ALT_QR = "【机器人01】上传于10-5 09:11，说明：群二维码"


def _pull_shell(*srcs):
    """一份「有图」的 ViewPic 壳（`alt` 逐条给，形状照 spec §0.2 的实测样本）。"""
    rows = []
    for src in srcs:
        alt = PULL_ALT_QR if "qr" in src else PULL_ALT_SHOT
        rows.append({"pid": "0", "IsOpenView": "None", "alt": alt, "src": src,
                     "thumb": src})
    return _view_pic_shell(rows)


class PullOrderScreenshotsTests(TenantTestCase):
    """B 段入库：两种图种 / 幂等 / 闸门 / 失败分档（读列表 vs 下载 vs 落库）。"""

    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 10, 5, 9, 30, tzinfo=dt_timezone.utc)
        self.user = User.objects.create_user(username="pics-pull", password=PASSWORD)
        self.order = Order.objects.create(
            game=Game.objects.create(name="合成游戏-拉图"),
            order_no=PULL_ORDER_NO, status=OrderStatus.IN_PROGRESS,
        )

    # ── 夹具 ────────────────────────────────────────────────────────────────

    def _client(self, envelope):
        client, patcher = _raising_call(envelope)
        self._patcher = patcher
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def _download(self, mapping, *, error=None):
        """下载替身：按 src 回字节；`error` 里给了 `src -> Exception` 的就抛。"""
        calls: list[str] = []
        errors = dict(error or {})

        def fake(src):
            calls.append(src)
            if src in errors:
                raise errors[src]
            return mapping.get(src, b"")

        fake.calls = calls
        return fake

    def _pull(self, envelope, *, download=None, client=None, uploaded_by=None):
        client = client or self._client(envelope)
        download = download or self._download({
            PULL_SRC_A: b"\xff\xd8\xff" + b"\0" * 8,
            PULL_SRC_QR: b"\x89PNG\r\n\x1a\n" + b"\0" * 8,
        })
        outcome = jidanpu_upstream_pics.pull_order_screenshots(
            client, order=self.order, remote_order_id=REMOTE_ID,
            uploaded_by=uploaded_by or self.user, now=self.now, download=download,
        )
        self.download = download
        return outcome

    def _rows(self):
        return list(OrderScreenshot.objects.filter(order=self.order).order_by("pk"))

    # ── 入库形状 ────────────────────────────────────────────────────────────

    def test_it_stores_both_image_types_ready_and_sniffed(self):
        """⭐ 两种图种都入库、`ready`、字节数与**嗅探出来的**类型照认领端点的口径填。"""
        outcome = self._pull(_pull_shell(PULL_SRC_A, PULL_SRC_QR))
        self.assertEqual([p.img_type for p in outcome.pulled], [0, 1])
        self.assertEqual(outcome.failed, ())
        self.assertEqual(outcome.reason, "")
        rows = self._rows()
        self.assertEqual([row.img_type for row in rows], [0, 1])
        self.assertEqual([row.status for row in rows], ["ready", "ready"])
        # JPEG 头 3 字节 + 8 个零 = 11；PNG 头 8 字节 + 8 个零 = 16。
        self.assertEqual([row.size_bytes for row in rows], [11, 16])
        self.assertEqual([row.content_type for row in rows], ["image/jpeg", "image/png"])
        self.assertEqual([row.uploaded_by_id for row in rows], [self.user.pk, self.user.pk])
        self.assertEqual([row.ready_at for row in rows], [self.now, self.now])
        # ⛔ `alt` 里有对端上传者的名字 ⇒ 一个字都不进 `remark`。
        self.assertEqual([row.remark for row in rows], ["", ""])
        # 前缀（⭐ D-0316）：**两类图都走共享段**（群码随订单同步；旧的"二维码留在本租户前缀"已废除）。
        self.assertTrue(rows[0].object_key.startswith(f"shared-orders/{PULL_ORDER_NO}/"))
        self.assertTrue(
            rows[1].object_key.startswith(
                f"shared-orders/{PULL_ORDER_NO}/qr."
            ),
            rows[1].object_key,
        )

    def test_the_second_call_stores_nothing_new(self):
        """⭐ 幂等：确定性对象名 ⇒ 第二轮**一张都不再存**。

        ⚠️ 2026-10-25 改准（计划 Rev.2）：第二轮里**群码**在下载前就被「本单已有 ready 群码」
        挡下（记 `skipped`，⛔ 不再是 `existing`）；订单截图仍按确定性对象名认出 `existing`。
        两种都表示「没存第二行」，但判据不同——别把这两档混成一档。
        """
        first = self._pull(_pull_shell(PULL_SRC_A, PULL_SRC_QR))
        keys = sorted(row.object_key for row in self._rows())
        order_key, qr_key = keys
        second = self._pull(_pull_shell(PULL_SRC_A, PULL_SRC_QR))
        self.assertEqual(len(first.pulled), 2)
        self.assertEqual(second.pulled, ())
        self.assertEqual(list(second.existing), [order_key])
        self.assertEqual([skip.object_key for skip in second.skipped], [""])
        self.assertIn("已有群二维码", second.skipped[0].reason)
        self.assertEqual(self.download.calls, [PULL_SRC_A])
        self.assertEqual(second.reason, "")
        self.assertEqual(len(self._rows()), 2)
        self.assertIn(qr_key, {row.object_key for row in self._rows()})

    def test_a_rotating_style_query_is_the_same_picture(self):
        """同一张图带/不带 `?x-oss-process=…` ⇒ **同一个**对象名（第二条算已有、不新建行）。"""
        styled = PULL_SRC_A + "?x-oss-process=style/geshi"
        body = b"\xff\xd8\xff" + b"\0" * 8
        outcome = self._pull(
            _pull_shell(PULL_SRC_A, styled),
            download=self._download({PULL_SRC_A: body, styled: body}),
        )
        self.assertEqual(len(outcome.pulled), 1)
        self.assertEqual(len(outcome.existing), 1)
        self.assertEqual(len(self._rows()), 1)

    def test_a_key_held_by_another_order_is_reported_not_overwritten(self):
        """对象名被**本租户另一张单**占着 ⇒ 记一条失败（⛔ 不改那一行、⛔ 不覆盖对象）。"""
        key = jidanpu_upstream_pics.build_inbound_object_key(
            order=self.order, img_type=OrderScreenshot.IMG_ORDER, src=PULL_SRC_A, ext="jpg",
        )
        other_order = Order.objects.create(
            game=Game.objects.create(name="合成游戏-别单"),
            order_no="20261005000000000715", status=OrderStatus.IN_PROGRESS,
        )
        held = OrderScreenshot.objects.create(
            order=other_order, img_type=OrderScreenshot.IMG_ORDER, object_key=key,
            status=OrderScreenshot.STATUS_READY, uploaded_by=self.user,
        )
        outcome = self._pull(_pull_shell(PULL_SRC_A))
        self.assertEqual(outcome.pulled, ())
        self.assertEqual([f.step for f in outcome.failed], ["store"])
        self.assertIn("另一张单", outcome.reason)
        held.refresh_from_db()
        self.assertEqual(held.order_id, other_order.pk)

    # ── 闸门 ────────────────────────────────────────────────────────────────

    def test_bad_ext_empty_oversized_and_non_image_are_skipped(self):
        """四种闸门各一档：扩展名 / 0 字节 / 超 18MB / 字节不是图片（最后一种要删掉孤儿对象）。"""
        gif = "https://oss-synth.invalid/x.gif"
        empty = "https://oss-synth.invalid/empty.jpg"
        huge = "https://oss-synth.invalid/huge.jpg"
        not_image = "https://oss-synth.invalid/not-image.jpg"
        good = "https://oss-synth.invalid/good.jpg"
        download = self._download(
            {empty: b"", huge: b"\xff\xd8\xff" + b"\0" * (MAX_IMAGE_BYTES + 1),
             not_image: b"this is not an image", good: b"\xff\xd8\xff" + b"\0" * 4},
        )
        outcome = self._pull(
            _pull_shell(gif, empty, huge, not_image, good), download=download,
        )
        self.assertEqual(len(outcome.pulled), 1)
        self.assertEqual(outcome.failed, ())
        self.assertEqual(len(outcome.skipped), 4)
        # ⛔ 扩展名不对的那张**一次都不该下载**（省掉一次注定用不上的往返）。
        self.assertNotIn(gif, download.calls)
        # 不是图片的那张：对象已删（⛔ 不留「行都没有」的孤儿）、也没建行。
        skipped_keys = [s.object_key for s in outcome.skipped]
        self.assertEqual(len(self._rows()), 1)
        for key in skipped_keys:
            if key:
                self.assertIsNone(get_storage().head(key), key)

    # ── 推送台账判重（skip_remote_keys）/ 图种过滤（img_types）────────────────

    #: 一张「我们自己推到下家」的远端对象名（合成值：推送侧 `${sid}-${orderId}-${rand5}-${epoch}.${ext}` 形状）。
    PUSHED_OSS_KEY = "10001-20261005000000000714-07790-1792908117091.png"

    def _pics_of(self, *srcs):
        """直接造 `RemotePics`（不走 ViewPic 壳）：判据只关心 `src` 与 `alt`。"""
        items = tuple(
            jidanpu_upstream_pics.RemotePic(
                src=src,
                alt=(
                    "【机器人01】上传于10-5 09:10，说明：群二维码" if "qr" in src
                    else "【机器人01】上传于10-5 09:10，说明：订单截图"
                ),
            )
            for src in srcs
        )
        return jidanpu_upstream_pics.RemotePics(items=items, rows=len(items))

    def test_skip_remote_keys_skips_our_own_pushed_picture(self):
        """远端对象 key 命中「我们推过的」⇒ 不下载、不入库（回环断点）。"""
        pushed_src = f"https://oss-synth.invalid/{self.PUSHED_OSS_KEY}"
        download = self._download({})
        outcome = jidanpu_upstream_pics.store_remote_pics(
            self._pics_of(pushed_src), order=self.order, uploaded_by=self.user,
            now=self.now, download=download, skip_remote_keys={self.PUSHED_OSS_KEY},
        )
        self.assertEqual(outcome.pulled, ())
        self.assertEqual(download.calls, [])
        self.assertEqual(self._rows(), [])
        self.assertIn("推过", outcome.skipped[0].reason)

    def test_skip_remote_keys_none_and_a_non_matching_key_still_pull(self):
        """⛔ 默认口径不变：`None` = 不跳过；集合里没有这张 ⇒ 照旧下载入库。"""
        src = f"https://oss-synth.invalid/{self.PUSHED_OSS_KEY}"
        body = b"\xff\xd8\xff" + b"\0" * 8
        for skip in (None, {"some-other-object.png"}):
            with self.subTest(skip=skip):
                OrderScreenshot.objects.all().delete()
                outcome = jidanpu_upstream_pics.store_remote_pics(
                    self._pics_of(src), order=self.order, uploaded_by=self.user,
                    now=self.now, download=self._download({src: body}),
                    skip_remote_keys=skip,
                )
                self.assertEqual(len(outcome.pulled), 1)
                self.assertEqual(len(self._rows()), 1)

    def test_img_types_filters_out_the_group_qr_before_download(self):
        """`img_types=(IMG_ORDER,)` ⇒ 群码那张**下载都不下**、更不入库；订单截图照旧。"""
        download = self._download({
            PULL_SRC_A: b"\xff\xd8\xff" + b"\0" * 8,
            PULL_SRC_QR: b"\x89PNG\r\n\x1a\n" + b"\0" * 8,
        })
        outcome = jidanpu_upstream_pics.store_remote_pics(
            self._pics_of(PULL_SRC_A, PULL_SRC_QR), order=self.order,
            uploaded_by=self.user, now=self.now, download=download,
            img_types=(OrderScreenshot.IMG_ORDER,),
        )
        self.assertEqual([p.img_type for p in outcome.pulled], [OrderScreenshot.IMG_ORDER])
        self.assertEqual(download.calls, [PULL_SRC_A])
        self.assertEqual([row.img_type for row in self._rows()], [OrderScreenshot.IMG_ORDER])

    def test_the_pull_entry_point_passes_both_arguments_through(self):
        """`pull_order_screenshots` 把两个新参数**真的透传**给 `store_remote_pics`。"""
        pushed_src = f"https://oss-synth.invalid/{self.PUSHED_OSS_KEY}"
        with mock.patch.object(
            jidanpu_upstream_pics, "store_remote_pics",
            return_value=jidanpu_upstream_pics.PullOutcome(),
        ) as store:
            jidanpu_upstream_pics.pull_order_screenshots(
                self._client(_pull_shell(pushed_src, PULL_SRC_QR)),
                order=self.order, remote_order_id=REMOTE_ID, uploaded_by=self.user,
                now=self.now, download=self._download({}),
                img_types=(OrderScreenshot.IMG_ORDER,),
                skip_remote_keys={self.PUSHED_OSS_KEY},
            )
        kwargs = store.call_args.kwargs
        self.assertEqual(kwargs["img_types"], (OrderScreenshot.IMG_ORDER,))
        self.assertEqual(kwargs["skip_remote_keys"], {self.PUSHED_OSS_KEY})

    def test_existing_ready_group_qr_is_skipped_before_download(self):
        """已有 `ready` 群码 ⇒ 第二张**下载都不下**、更不入库（业务依据：同口径的
        「本单已经有群二维码了，不用再同步」见 `api/my_orders_views.py`）。"""
        OrderScreenshot.objects.create(
            order=self.order, img_type=OrderScreenshot.IMG_GROUP_QR,
            object_key="test/orders/%s/qr.existing.png" % PULL_ORDER_NO,
            uploaded_by=self.user, status=OrderScreenshot.STATUS_READY,
        )
        download = self._download({PULL_SRC_QR: b"\x89PNG\r\n\x1a\n" + b"\0" * 8})
        outcome = jidanpu_upstream_pics.pull_order_screenshots(
            self._client(_pull_shell(PULL_SRC_QR)),
            order=self.order, remote_order_id=REMOTE_ID,
            uploaded_by=self.user, now=self.now, download=download,
        )
        self.assertEqual(outcome.pulled, ())
        self.assertEqual(download.calls, [])
        self.assertEqual(
            OrderScreenshot.objects.filter(
                order=self.order, img_type=OrderScreenshot.IMG_GROUP_QR).count(), 1,
        )

    def test_the_twentieth_order_screenshot_caps_the_pull(self):
        """订单截图已满 20 张 ⇒ 第 21 张不下载、不入库（与认领端点的数量闸同口径）。"""
        OrderScreenshot.objects.bulk_create([
            OrderScreenshot(
                order=self.order, img_type=OrderScreenshot.IMG_ORDER,
                object_key=f"test/orders/{PULL_ORDER_NO}/shot-{index}.jpg",
                uploaded_by=self.user, status=OrderScreenshot.STATUS_READY,
            )
            for index in range(20)
        ])
        download = self._download({PULL_SRC_A: b"\xff\xd8\xff" + b"\0" * 8})
        outcome = jidanpu_upstream_pics.pull_order_screenshots(
            self._client(_pull_shell(PULL_SRC_A)),
            order=self.order, remote_order_id=REMOTE_ID,
            uploaded_by=self.user, now=self.now, download=download,
        )
        self.assertEqual(outcome.pulled, ())
        self.assertEqual(download.calls, [])
        self.assertEqual(
            OrderScreenshot.objects.filter(
                order=self.order, img_type=OrderScreenshot.IMG_ORDER).count(), 20,
        )
        self.assertIn("20", outcome.skipped[0].reason)

    def test_a_ready_group_qr_conflict_deletes_the_uploaded_object(self):
        """并发兜底（`IntegrityError` 分支）：撞的是「每单一张 ready 群码」唯一索引
        ⇒ **删掉刚传的对象**（防孤儿）+ 记一条跳过（⛔ 不记失败：这不是故障）。"""
        src = "https://oss-synth.invalid/pull-qr.png"
        body = b"\x89PNG\r\n\x1a\n" + b"\0" * 8
        would_be_key = jidanpu_upstream_pics.build_inbound_object_key(
            order=self.order, img_type=OrderScreenshot.IMG_GROUP_QR, src=src, ext="png",
        )

        def create(**_kwargs):
            # 模拟「另一个 worker 在我们 INSERT 之前先插成功」：撞唯一索引。
            raise IntegrityError("uniq_order_ready_group_qr")

        def download(_src):
            # ⚠️ 竞争行要在拉图的**下载那一跳**插（它在 `transaction.atomic()` savepoint 之外）
            #    ——放进被回滚的 savepoint 里就等于没插，判据会变成「走失败分支」。
            OrderScreenshot.objects.bulk_create([OrderScreenshot(
                order=self.order, img_type=OrderScreenshot.IMG_GROUP_QR,
                object_key=f"test/orders/{PULL_ORDER_NO}/qr.winner.png",
                uploaded_by=self.user, status=OrderScreenshot.STATUS_READY,
            )])
            return body

        with mock.patch.object(OrderScreenshot.objects, "create", side_effect=create):
            outcome = jidanpu_upstream_pics.store_remote_pics(
                self._pics_of(src), order=self.order, uploaded_by=self.user,
                now=self.now, download=download,
            )

        self.assertEqual(outcome.pulled, ())
        self.assertEqual(outcome.failed, ())
        self.assertIn("已有群二维码", outcome.skipped[0].reason)
        self.assertIsNone(get_storage().head(would_be_key), "冲突那张图的字节成了孤儿")
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order).count(), 1)

    # ── 失败分档（⛔ 「没图」与「没拉成」不许混）──────────────────────────────

    def test_an_empty_shell_is_an_empty_outcome_not_a_failure(self):
        outcome = self._pull(_view_pic_shell([]))
        self.assertEqual(
            (outcome.pulled, outcome.existing, outcome.skipped, outcome.failed, outcome.reason),
            ((), (), (), (), ""),
        )
        self.assertEqual(self._rows(), [])

    def test_rows_without_a_usable_src_are_a_view_failure(self):
        """回包有 2 条却一条都读不出地址 ⇒ `step="view"` 的失败（对端换形状的信号）。"""
        outcome = self._pull(_view_pic_shell([{"pid": "0"}, {"pid": "1"}]))
        self.assertEqual([f.step for f in outcome.failed], ["view"])
        self.assertIn("读不出地址", outcome.reason)
        self.assertEqual(self._rows(), [])

    def test_an_unreadable_view_pic_is_a_view_failure_not_a_silent_empty(self):
        """连不上（拿不到业务壳）⇒ `step="view"` 的失败，⛔ 不抛、也⛔ 不当「没图」。"""
        envelope = {}
        client = _StubClient(pic_envelope=envelope)
        client.last_envelope = envelope
        with mock.patch.object(
            client, "call", side_effect=TransportError("合成：连不上")
        ):
            outcome = self._pull({}, client=client)
        self.assertEqual([f.step for f in outcome.failed], ["view"])
        self.assertIn("读下家订单图失败", outcome.reason)
        self.assertEqual(self._rows(), [])

    def test_a_failed_download_does_not_stop_the_other_pictures(self):
        download = self._download(
            {PULL_SRC_QR: b"\x89PNG\r\n\x1a\n" + b"\0" * 4},
            error={PULL_SRC_A: httpx.ConnectError(f"合成：连不上 {PULL_SRC_A}")},
        )
        outcome = self._pull(_pull_shell(PULL_SRC_A, PULL_SRC_QR), download=download)
        self.assertEqual([f.step for f in outcome.failed], ["download"])
        self.assertEqual(len(outcome.pulled), 1)
        self.assertEqual(len(self._rows()), 1)
        # ⛔ 隐私：源直链（查询串里是签名）不许进人话。
        self.assertNotIn(PULL_SRC_A, outcome.reason)
        self.assertNotIn("https", outcome.reason)
        for pic in (PULL_SRC_A, PULL_SRC_QR):
            self.assertNotIn(pic.rsplit("/", 1)[-1], outcome.reason)
