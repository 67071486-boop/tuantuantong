"""T8：开放平台**主动回调推送**的判据（设计文档 §7.2 / §7.3 / §7.4 / §4-V6 / §9）。

## 本文件证什么（逐条都能失败）

| 组 | 钉什么 |
|---|---|
| `SignatureTests` | §7.3 的报文与签名**逐字**：顶层字段 / `timestamp` 是整数 / 三个请求头 / 签名可被**独立复算**；⭐ 签名在**投递那一刻**用**当时的**密钥现算（轮换密钥 ⇒ 旧行用新密钥重签）；`payload` 里**没有** `sign` / `timestamp`；`delivery_id` 跨重投**稳定** |
| `DeliveryOutcomeTests` | 成功判据 = **HTTP 2xx**；3xx（**禁跳转**）/ 5xx / 超时**都算失败**，且把回执码与原因写回 |
| `RetryScheduleTests` | §7.4 的退避表**逐档对拍**（`10s/60s/5m/15m/1h/6h` 六档**全部可达**）+ 第 7 次失败终态 `failed` 且**保留行** |
| `SsrfGuardTests` | §4-V6：仅 https / 每次投递前解析并校验 IP / 私网·回环·链路本地·保留段一律拒 / 解析不出就拒；⚠️ **投递前的拒收发生在任何 HTTP 之前**（判据打在「一个请求都没发出去」上） |
| `OutboxTransactionTests` | ⭐ §7.4 的**改准块**：outbox 行与业务写入**同一个事务** ⇒ **业务回滚不产生推送行**（⛔ 不是 `on_commit`） |
| `EventCoverageTests` | 三类事件各自的**真实写入点**都真的排了行：状态机 / 软删 / 进度图（同步接收侧 + 内部认领两条路） |
| `DedupTests` | ⭐ 去重判据的**两半**：同一状态在 pending 窗口里重复 ⇒ 只一行；**删 → 恢复 → 再删**的第二删**不许**被吃掉 |
| `EligibilityTests` | §7.1：地址空 / 回调停用 / 应用停用 ⇒ **不排队**；行已排上而应用随后不可用 ⇒ 终态 + 人话 + **一个 HTTP 都不发** |
| `NotifierFamilyGuardTests` | ⭐ **第二期 T2 的族守卫**：三个上家侧 `notify_*` 传**别族**那一份 spec ⇒ 当场 `ValueError` + **零建行**；⭐ 对照传 `OPENAPI_SPEC` ⇒ 照常建行；通用引擎入口（取活 / 清理 / 一轮）**不受收窄** |
| `OrderingAndPruneTests` | §7.4 的**同一订单按产生顺序**（更早那条没投出去，更晚那条不许抢先）+ 机会式清理只删老的 `done` 行、一次最多 500 |
| `LoopCommandTests` | 常驻命令的 `--once` / `--loop` 互斥 + `--once` 退出码契约（预期内的投递失败**不**非零） |

## 夹具纪律

- 凭据与地址一律**合成值**（`AGENTS.md` §5-C）：回调地址用公网地址**字面量**
  （`93.184.216.34`，⛔ 不是任何真实第三方），**绝不打真实网络**——HTTP 层一律用替身，
  而且每条投递用例都断言**替身真的被调到**（`AGENTS.md` §5-E / E-0029：桩接没接上本身要有判据）。
- ⚠️ 投递 URL 用 **IP 字面量**而不是域名：`getaddrinfo` 对字面量**不查 DNS**
  ⇒ 用例在**无网环境**下也能跑出确定的 SSRF 读数（域名在沙箱里会解析失败，
  那会把「SSRF 判据」与「这里没有网络」两件事混成一件）。
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import json
import pathlib
import tokenize
import types
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connection, transaction
from django.test import SimpleTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business import recycle
from apps.business.models import (
    Game,
    Order,
    OrderScreenshot,
    OrderStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
)
from apps.business.order_status import apply_transition
from apps.business.peer_inbound import _apply_screenshot, apply_events
from apps.business.peer_sync import hash_token, new_token
from apps.core.storage import SHARED_ORDER_PREFIX, get_storage
from apps.openapi import dispatch, outbound
from apps.openapi import credentials as credentials_module
from apps.openapi.models import ApiApp, ApiAppState, CallbackState, OpenApiDelivery
from apps.openapi.credentials import OPENAPI_SPEC
# ⚠️⭐ 第二期 T2：族守卫要拿**别族那一份** spec 去撞（`NotifierFamilyGuardTests`），
# 以及"别族那张台账一行都不许有"的判据（`TakerApiDelivery`）。
from apps.takerapi.credentials import TAKER_SPEC
from apps.takerapi.models import TakerApiDelivery
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: 一个**公网**地址字面量（IANA 的示例段之外，仅用于夹具；⛔ 不是任何真实第三方）。
PUBLIC_HOST = "93.184.216.34"
#: 配好的回调地址（合成值）。
CALLBACK_URL = f"https://{PUBLIC_HOST}/openapi/hook"
#: 合成凭据（⛔ 与任何真实应用的 appKey / secret 无关）。
APP_KEY = "t8dispatchkey0001"
APP_SECRET = "t8-dispatch-secret-0000000000000001"
APP_NAME = "推送夹具应用甲"

#: 事件名的字面值（E-0024：钉值不钉常量）。
EVENT_STATUS = "order.status_changed"
EVENT_DELETED = "order.deleted"
EVENT_IMAGE = "order.image_added"

#: 一个真的 JPEG 魔数开头的最小字节串（内部认领那条路要过魔数嗅探）。
JPEG = b"\xff\xd8\xff" + b"t8-fake-jpeg-body"


# ── HTTP 替身（打在 `apps.openapi.outbound` **查找** `httpx` 的那个名字上）──────────────


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes = b""):
        self.status_code = status_code
        self._body = body

    def iter_bytes(self, chunk_size=None):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeClient:
    """`httpx.Client` 的替身：记录每一次 `stream()` 调用，按配置回放或抛异常。"""

    instances: list["_FakeClient"] = []

    def __init__(self, *, response=None, error=None, timeout=None, follow_redirects=None):
        self.response = response
        self.error = error
        self.timeout = timeout
        self.follow_redirects = follow_redirects
        self.calls: list[dict] = []
        _FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def stream(self, method, url, content=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "content": content, "headers": dict(headers or {})}
        )
        if self.error is not None:
            raise self.error
        return self.response


class _HttpBackend:
    """上下文管理器：把 outbound 看到的那**一个** `httpx` 名字换成替身命名空间。

    ⚠️ 换的是 `apps.openapi.outbound.httpx`（该模块**查找**它的地方，`AGENTS.md` §5-E / E-0029），
    ⛔ 不是真的去改全局的 `httpx` 模块（那会波及其它模块）。
    """

    def __init__(self, *, status: int = 200, body: bytes = b"", error=None):
        self.response = _FakeResponse(status, body)
        self.error = error

    def __enter__(self):
        _FakeClient.instances = []
        stub = types.SimpleNamespace(
            Client=functools.partial(
                _FakeClient, response=self.response, error=self.error
            ),
            HTTPError=httpx.HTTPError,
            InvalidURL=httpx.InvalidURL,
            Response=httpx.Response,
        )
        self._patch = mock.patch("apps.openapi.outbound.httpx", stub)
        self._patch.start()
        return _FakeClient

    def __exit__(self, *exc_info):
        self._patch.stop()
        return False


def _sent_body(client: _FakeClient) -> tuple[dict, bytes]:
    """替身记下的那次请求：`(解析后的报文, 原始字节)`。"""
    assert client.calls, "替身一次都没被调到（桩没接上 ⇒ 本用例不产出结论）"
    raw = client.calls[0]["content"]
    return json.loads(raw), raw


def _expected_signature(secret: str, timestamp: int, delivery_id: str, body: dict) -> str:
    """**独立**复算签名（照 §7.3 的公式，⛔ 不 import `outbound` 里的任何实现）。

    ⚠️ 规范化口径是**契约的一部分**（`sort_keys` + 固定分隔符 + `ensure_ascii=False`）：
    这里把它**再写一遍**，正是为了证「第三方按文档也能算出同一个值」。
    """
    text = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(text).hexdigest()
    message = f"{timestamp}\n{delivery_id}\n{digest}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


class _DispatchFixture(TenantTestCase):
    """公共夹具：一个租户、一个用户、一个游戏、一张订单、一个配好回调的应用。"""

    def setUp(self):
        self.user = User.objects.create_user(username="t8-dispatch", password="pw-t8")
        self.game = Game.objects.create(name="推送夹具游戏甲")
        self.order = Order.objects.create(
            order_no="20261008000000000001",
            game=self.game,
            status=OrderStatus.WAITING_GRAB,
            total_price=Decimal("10.00"),
            source_order_no="SRC-0001",
            sales_user=self.user,
        )

    # ── 夹具构造器 ────────────────────────────────────────────────────────
    def make_app(self, **overrides) -> ApiApp:
        fields = {
            "name": APP_NAME,
            "app_key": APP_KEY,
            "app_secret": APP_SECRET,
            "callback_url": CALLBACK_URL,
            "callback_state": CallbackState.ENABLED,
            "created_by": self.user,
        }
        fields.update(overrides)
        return ApiApp.objects.create(**fields)

    def enqueue_status(self, *, status=OrderStatus.IN_PROGRESS, occurred_at=None, order=None):
        """排一行状态事件（**只**走领域入口，⛔ 不自己 `create` —— 那会绕开被测的去重）。"""
        order = order or self.order
        return dispatch.notify_order_status_changed(
            order,
            spec=OPENAPI_SPEC,
            status=status,
            occurred_at=occurred_at or timezone.now(),
        )

    def only_row(self) -> OpenApiDelivery:
        rows = list(OpenApiDelivery.objects.all())
        self.assertEqual(len(rows), 1, [r.payload for r in rows])
        return rows[0]


# ── §7.3 报文与签名 ────────────────────────────────────────────────────────────


class SignatureTests(_DispatchFixture):
    """报文形状、签名可复算、以及「签名在投递那一刻现算」这条语义。"""

    #: §7.3 的**恰好**顶层字段（`note` 是样例里就有的一格）。
    EXPECTED_KEYS = {
        "event", "delivery_id", "order_no", "source_order_no", "status", "note",
        "occurred_at", "timestamp", "sign",
    }

    def test_the_request_body_and_headers_match_the_frozen_contract(self):
        """一次真实投递发出的**字节**：字段恰好那几个 + `timestamp` 是整数 + 三个头对得上。"""
        self.make_app()
        row = self.enqueue_status()[0]
        with _HttpBackend(status=200) as fake:
            outcome = dispatch.deliver_one(row.pk, spec=OPENAPI_SPEC)
        self.assertEqual(outcome, "delivered")
        client = fake.instances[0]
        self.assertEqual(len(client.calls), 1, "替身应当恰好被调到一次")
        sent, raw = _sent_body(client)

        self.assertEqual(set(sent), self.EXPECTED_KEYS, sorted(sent))
        self.assertEqual(sent["event"], EVENT_STATUS)
        self.assertEqual(sent["order_no"], self.order.order_no)
        self.assertEqual(sent["source_order_no"], "SRC-0001")
        self.assertEqual(sent["status"], str(OrderStatus.IN_PROGRESS))
        self.assertEqual(sent["note"], "")
        # ⚠️ `timestamp` 是**整数**（§7.3 逐字），⛔ 不是字符串、⛔ 不是毫秒。
        self.assertIsInstance(sent["timestamp"], int)
        self.assertNotIsInstance(sent["timestamp"], bool)
        # `occurred_at` 是带时区的 ISO8601。
        self.assertIsNotNone(datetime.fromisoformat(sent["occurred_at"]).tzinfo)

        # 三个头（§7.3 逐字）。
        headers = client.calls[0]["headers"]
        self.assertEqual(headers[outbound.HEADER_TIMESTAMP], str(sent["timestamp"]))
        self.assertEqual(headers[outbound.HEADER_DELIVERY], sent["delivery_id"])
        self.assertEqual(headers[outbound.HEADER_SIGNATURE], sent["sign"])

        # ⭐ 签名可被**独立**复算（第三方按文档也能算出同一个值）。
        without_sign = {key: value for key, value in sent.items() if key != "sign"}
        self.assertEqual(
            sent["sign"],
            _expected_signature(
                APP_SECRET, sent["timestamp"], sent["delivery_id"], without_sign
            ),
        )
        # ⚠️ 请求体**逐字节**就是规范化之后的那一份（否则对端复算的摘要不同）。
        self.assertEqual(
            raw,
            json.dumps(
                sent, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"),
        )

    def test_the_signature_is_recomputed_at_delivery_time_with_the_current_secret(self):
        """⭐ 轮换密钥之后，**尚未投递**的行用**新**密钥重签（旧密钥签不过）。

        失败方向：把 `sign` 存进 `payload`（写行那一刻算好）⇒ 本用例红——
        而那正是模型 docstring 点名的形态（对端一律验不过，且看起来像"对方不认我们的签名"）。
        """
        app = self.make_app()
        row = self.enqueue_status()[0]
        app.app_secret = "t8-rotated-secret-000000000000000002"
        app.save(update_fields=["app_secret"])
        with _HttpBackend(status=200) as fake:
            dispatch.deliver_one(row.pk, spec=OPENAPI_SPEC)
        sent, _raw = _sent_body(fake.instances[0])
        without_sign = {key: value for key, value in sent.items() if key != "sign"}
        self.assertEqual(
            sent["sign"],
            _expected_signature(
                "t8-rotated-secret-000000000000000002",
                sent["timestamp"],
                sent["delivery_id"],
                without_sign,
            ),
        )
        self.assertNotEqual(
            sent["sign"],
            _expected_signature(
                APP_SECRET, sent["timestamp"], sent["delivery_id"], without_sign
            ),
            "旧密钥居然也能签出同一个值（那说明签名没跟着密钥走）",
        )

    def test_the_stored_payload_never_carries_the_signature_or_the_timestamp(self):
        """⛔ `payload` 列里**不许**有 `sign` / `timestamp`（模型 docstring 逐字）。"""
        self.make_app()
        row = self.enqueue_status()[0]
        self.assertNotIn("sign", row.payload)
        self.assertNotIn("timestamp", row.payload)

    def test_the_delivery_id_is_stable_across_retries(self):
        """`delivery_id` 是对方**去重**的键 ⇒ 同一条行重投必须沿用同一颗。"""
        self.make_app()
        row = self.enqueue_status()[0]
        with _HttpBackend(status=500) as fake:
            dispatch.deliver_one(row.pk, spec=OPENAPI_SPEC)
        first, _raw = _sent_body(fake.instances[0])
        row.refresh_from_db()
        with _HttpBackend(status=500) as fake2:
            dispatch.deliver_one(row.pk, now=row.next_attempt_at, spec=OPENAPI_SPEC)
        second, _raw2 = _sent_body(fake2.instances[0])
        self.assertEqual(first["delivery_id"], second["delivery_id"])
        self.assertEqual(first["delivery_id"], row.payload["delivery_id"])
        # ⚠️ 而 `timestamp` **会**变（它是投递那一刻现算的）——两者一起证明"签名不落库"这条设计。
        self.assertGreaterEqual(second["timestamp"], first["timestamp"])


# ── 成功 / 失败判据 ────────────────────────────────────────────────────────────


class DeliveryOutcomeTests(_DispatchFixture):
    """§7.3 的最后一句：**只有 2xx 算成功**。"""

    def test_a_2xx_marks_the_row_done_and_records_the_code(self):
        self.make_app()
        row = self.enqueue_status()[0]
        with _HttpBackend(status=204) as fake:
            outcome = dispatch.deliver_one(row.pk, spec=OPENAPI_SPEC)
        self.assertEqual(outcome, "delivered")
        self.assertEqual(len(fake.instances[0].calls), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, OpenApiDelivery.Status.DONE)
        self.assertEqual(row.response_status, 204)
        self.assertEqual(row.reason, "")
        self.assertEqual(row.attempts, 1)
        self.assertIsNone(row.next_attempt_at, "终态行必须摘掉租约")
        self.assertIsNotNone(row.processed_at)

    def test_a_500_is_a_failure_and_records_the_code_and_reason(self):
        self.make_app()
        row = self.enqueue_status()[0]
        with _HttpBackend(status=500, body=b"upstream exploded") as fake:
            outcome = dispatch.deliver_one(row.pk, spec=OPENAPI_SPEC)
        self.assertEqual(outcome, "retried")
        self.assertEqual(len(fake.instances[0].calls), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, OpenApiDelivery.Status.PENDING)
        self.assertEqual(row.response_status, 500)
        self.assertIn("500", row.reason)
        self.assertIn("upstream exploded", row.reason)

    def test_a_3xx_is_a_failure_and_we_do_not_follow_the_redirect(self):
        """⭐ 禁跳转（§9）：3xx **算失败**，而且客户端必须是 `follow_redirects=False`。

        失败方向：把 `follow_redirects` 打开（或删掉那个参数）⇒ 本用例红
        ——那正好绕过投递前那次 SSRF 校验（跟着跳到内网地址去）。
        """
        self.make_app()
        row = self.enqueue_status()[0]
        with _HttpBackend(status=302) as fake:
            outcome = dispatch.deliver_one(row.pk, spec=OPENAPI_SPEC)
        self.assertEqual(outcome, "retried")
        client = fake.instances[0]
        self.assertIs(client.follow_redirects, False)
        row.refresh_from_db()
        self.assertEqual(row.response_status, 302)

    def test_a_timeout_is_a_failure_without_a_status_code(self):
        self.make_app()
        row = self.enqueue_status()[0]
        with _HttpBackend(error=httpx.ConnectTimeout("too slow")) as fake:
            outcome = dispatch.deliver_one(row.pk, spec=OPENAPI_SPEC)
        self.assertEqual(outcome, "retried")
        self.assertEqual(len(fake.instances[0].calls), 1, "超时也算一次投递尝试")
        row.refresh_from_db()
        self.assertEqual(row.attempts, 1)
        self.assertIsNone(row.response_status)
        self.assertIn("ConnectTimeout", row.reason)

    def test_the_timeout_is_the_documented_ten_seconds(self):
        """§9 逐字「超时 10s」——判据打在**交给 httpx 的那个值**上。"""
        self.make_app()
        row = self.enqueue_status()[0]
        with _HttpBackend(status=200) as fake:
            dispatch.deliver_one(row.pk, spec=OPENAPI_SPEC)
        self.assertEqual(fake.instances[0].timeout, outbound.CALLBACK_TIMEOUT_SECONDS)
        self.assertEqual(outbound.CALLBACK_TIMEOUT_SECONDS, 10.0)


# ── 退避表 ────────────────────────────────────────────────────────────────────


class RetryScheduleTests(_DispatchFixture):
    """§7.4 的退避表：`10s / 60s / 5m / 15m / 1h / 6h`，**逐档对拍**。"""

    def test_the_backoff_schedule_is_exactly_the_documented_six_steps(self):
        """六档**全部可达**：第 1–6 次失败各取一档（⛔ 不许写成 `>= len(...)` 判终态，
        那会让最后一档 `6h` 永远用不上）。"""
        self.make_app()
        row = self.enqueue_status()[0]
        now = timezone.now()
        for index, interval in enumerate(dispatch.BACKOFF_SECONDS, start=1):
            with _HttpBackend(status=503):
                outcome = dispatch.deliver_one(row.pk, now=now, spec=OPENAPI_SPEC)
            self.assertEqual(outcome, "retried", f"第 {index} 次失败应当是重试")
            row.refresh_from_db()
            self.assertEqual(row.attempts, index)
            self.assertEqual(row.status, OpenApiDelivery.Status.PENDING)
            self.assertEqual(
                row.next_attempt_at,
                now + timedelta(seconds=interval),
                f"第 {index} 次失败的退避时刻不对（期望 {interval} 秒）",
            )
            now = now + timedelta(seconds=interval)
        # 把文档里那六个数字**逐字**钉一遍（防止有人"顺手调参"而没改文档）。
        self.assertEqual(dispatch.BACKOFF_SECONDS, (10, 60, 300, 900, 3600, 21600))

    def test_the_seventh_failure_is_terminal_and_the_row_is_kept(self):
        self.make_app()
        row = self.enqueue_status()[0]
        now = timezone.now()
        for interval in dispatch.BACKOFF_SECONDS:
            with _HttpBackend(status=503):
                dispatch.deliver_one(row.pk, now=now, spec=OPENAPI_SPEC)
            now = now + timedelta(seconds=interval)
        with _HttpBackend(status=503) as fake:
            outcome = dispatch.deliver_one(row.pk, now=now, spec=OPENAPI_SPEC)
        self.assertEqual(outcome, "failed")
        self.assertEqual(len(fake.instances[0].calls), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, OpenApiDelivery.Status.FAILED)
        self.assertEqual(row.attempts, len(dispatch.BACKOFF_SECONDS) + 1)
        self.assertIsNone(row.next_attempt_at)
        self.assertIsNotNone(row.processed_at)
        self.assertIn("503", row.reason)
        # ⛔ **保留行**（页面可见、可手工重投）：不是删掉。
        self.assertEqual(OpenApiDelivery.objects.filter(pk=row.pk).count(), 1)


# ── SSRF（§4-V6 / §9）────────────────────────────────────────────────────────


class SsrfGuardTests(_DispatchFixture):
    """仅 https + 私网 / 回环 / 链路本地 / 保留段一律拒 + 解析不出就拒。"""

    #: 每一种"必须被拒"的地址形状（合成值；公网那一档单独作正对照）。
    REJECTED = (
        "http://93.184.216.34/hook",          # 协议不是 https
        "https://127.0.0.1/hook",             # 回环（本机 gunicorn 就在 127.0.0.1:8001）
        "https://10.1.2.3/hook",              # 私网
        "https://172.16.0.9/hook",            # 私网
        "https://192.168.1.10/hook",          # 私网
        "https://169.254.169.254/latest",     # 链路本地（云元数据地址）
        "https://100.64.0.1/hook",            # CGNAT
        "https://0.0.0.0/hook",               # 未指定
        "https://192.0.2.5/hook",             # TEST-NET（保留段）
        "https://[::1]/hook",                 # IPv6 回环
        "https://[fd00::1]/hook",             # IPv6 唯一本地
        "https://user:pw@93.184.216.34/hook",  # 带用户信息
    )

    def test_each_rejected_shape_is_refused_by_the_shared_validator(self):
        """保存时 / 投递前**共用**的那一个函数逐条拒（⛔ 不许两处各写一份判据）。"""
        # 正对照：公网 https 放行（没有它，"全拒"也能让本用例绿）。
        self.assertEqual(outbound.validate_callback_url(CALLBACK_URL), CALLBACK_URL)
        # 空地址合法（= 没配回调）。
        self.assertEqual(outbound.validate_callback_url(""), "")
        for url in self.REJECTED:
            with self.subTest(url=url):
                with self.assertRaises(outbound.CallbackUrlError) as ctx:
                    outbound.validate_callback_url(url)
                self.assertTrue(str(ctx.exception), "拒收必须有**人话**文案")

    def test_an_unresolvable_host_is_refused_fail_closed(self):
        """解析不出来 ⇒ **拒**（⛔ 不许把"解析不出来"当成"安全"）。"""
        with self.assertRaises(outbound.CallbackUrlError) as ctx:
            outbound.validate_callback_url("https://t8-no-such-host.invalid/hook")
        # 本机代理的 fake-ip DNS 会把 .invalid 解析进 198.18.0.0/15 ⇒ 走「保留段拒绝」支；
        # 两支都是 fail-closed 拒绝，「非公网」支由 REJECTED 清单与 169.254 那条用例单独钉。
        msg = str(ctx.exception)
        self.assertTrue(
            outbound.RESOLVE_FAILED_MSG in msg or outbound.NON_PUBLIC_IP_MSG in msg,
            msg,
        )

    def test_a_queued_row_pointing_at_a_private_address_is_refused_before_any_http(self):
        """⭐ 已存在的行**在投递前也被拒**（§12 点名的那一条独立用例）。

        判据打在**一个 HTTP 都没发出去**上（⛔ 不是"最终失败了"——那两种形态都能让
        "结果是 failed"成立，只有"零请求"能分辨实现到底有没有把地址交给网络层）。

        ⚠️ **这一档按普通失败走退避表**（⛔ 不是立刻终态）：地址判据是**每次投递前**重跑的
        （§4-V6 的理由就是 DNS 可变）⇒ "这次解析到内网"不等于"永远是内网"。
        终态由"试满 7 次"兜住（下面那条用例）。
        """
        app = self.make_app(callback_url="https://169.254.169.254/latest/meta-data")
        row = self.enqueue_status()[0]
        self.assertEqual(row.app_id, app.pk)
        with _HttpBackend(status=200) as fake:
            outcome = dispatch.deliver_one(row.pk, spec=OPENAPI_SPEC)
        self.assertEqual(outcome, "retried")
        self.assertEqual(fake.instances, [], "私网地址居然被交给了 HTTP 层")
        row.refresh_from_db()
        self.assertEqual(row.status, OpenApiDelivery.Status.PENDING)
        self.assertIn(outbound.NON_PUBLIC_IP_MSG, row.reason)
        self.assertIsNone(row.response_status, "一个请求都没发出去 ⇒ 不许有回执码")
        self.assertIsNotNone(row.next_attempt_at)

    def test_a_permanently_invalid_address_ends_terminal_after_the_schedule(self):
        """地址永远不合法 ⇒ 试满 7 次之后落终态 `failed` + 人话原因，**行保留**。"""
        self.make_app(callback_url="https://169.254.169.254/latest/meta-data")
        row = self.enqueue_status()[0]
        now = timezone.now()
        for interval in dispatch.BACKOFF_SECONDS:
            with _HttpBackend(status=200) as fake:
                dispatch.deliver_one(row.pk, now=now, spec=OPENAPI_SPEC)
            self.assertEqual(fake.instances, [], "内网地址一次都不许被交给 HTTP 层")
            now = now + timedelta(seconds=interval)
        with _HttpBackend(status=200) as fake:
            self.assertEqual(dispatch.deliver_one(row.pk, now=now, spec=OPENAPI_SPEC), "failed")
        self.assertEqual(fake.instances, [])
        row.refresh_from_db()
        self.assertEqual(row.status, OpenApiDelivery.Status.FAILED)
        self.assertIn(outbound.NON_PUBLIC_IP_MSG, row.reason)
        self.assertEqual(OpenApiDelivery.objects.filter(pk=row.pk).count(), 1)

    def test_a_queued_row_whose_address_became_non_https_is_refused(self):
        """同一个洞的另一半：地址是 `http://` ⇒ 投递前同样拒、同样零请求。"""
        self.make_app(callback_url="http://93.184.216.34/hook")
        row = self.enqueue_status()[0]
        with _HttpBackend(status=200) as fake:
            dispatch.deliver_one(row.pk, spec=OPENAPI_SPEC)
        self.assertEqual(fake.instances, [])
        row.refresh_from_db()
        self.assertEqual(row.reason, outbound.NOT_HTTPS_MSG)
        self.assertEqual(row.status, OpenApiDelivery.Status.PENDING)


# ── outbox 与业务写入同一个事务 ─────────────────────────────────────────────────


class OutboxTransactionTests(_DispatchFixture):
    """⭐ §7.4 的改准块：**同一个事务**（⛔ 不是 `on_transaction_commit`）。"""

    ANY_NODES = frozenset({
        "order.change_status", "grab_hall.take_order", "order.assign_taker",
    })

    def test_a_rolled_back_transaction_leaves_no_delivery_row(self):
        """⭐ **业务回滚 ⇒ 不产生推送行**（弄红证明①的目标用例）。

        驱动的是**真实**领域函数（`apply_transition`），⛔ 不是直接调 `notify_*`：
        后者证明不了"入队点在业务事务里"这件事。
        """
        self.make_app()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                apply_transition(
                    self.order, OrderStatus.IN_PROGRESS,
                    nodes=self.ANY_NODES, actor="t8-actor",
                )
                self.assertEqual(
                    OpenApiDelivery.objects.count(), 1,
                    "入队必须发生在**同一个**事务里（此刻应已看到那一行）",
                )
                raise RuntimeError("故意回滚")
        self.assertEqual(
            OpenApiDelivery.objects.count(), 0,
            "业务回滚了、推送行却留下了 ⇒ 入队不在同一个事务里（`on_commit` 的形态）",
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)

    def test_a_committed_transition_leaves_exactly_one_row(self):
        """正对照：同一段代码提交之后**恰好一行**（否则上面那条会因为"压根不入队"而假绿）。"""
        self.make_app()
        with transaction.atomic():
            apply_transition(
                self.order, OrderStatus.IN_PROGRESS,
                nodes=self.ANY_NODES, actor="t8-actor",
            )
        row = self.only_row()
        self.assertEqual(row.event, EVENT_STATUS)
        self.assertEqual(row.payload["status"], str(OrderStatus.IN_PROGRESS))
        self.assertEqual(row.order_no, self.order.order_no)


# ── 三类事件在真实写入点上真的排了行 ─────────────────────────────────────────────


class EventCoverageTests(_DispatchFixture):
    """§7.2.1 的覆盖面：三类事件各自的真实路径都排得出行。"""

    ANY_NODES = OutboxTransactionTests.ANY_NODES

    def test_the_state_machine_enqueues_a_status_event(self):
        self.make_app()
        with transaction.atomic():
            apply_transition(
                self.order, OrderStatus.IN_PROGRESS,
                nodes=self.ANY_NODES, actor="t8-actor",
            )
        self.assertEqual(self.only_row().event, EVENT_STATUS)

    def test_the_soft_delete_enqueues_a_deleted_event_with_the_explicit_moment(self):
        """⭐ 软删**不改 `Order.updated_at`** ⇒ 报文时刻必须是**显式取**的那个删除时刻。

        失败方向：改成从 `order.updated_at` 取 ⇒ 本用例红（库里那一列一个字都没改）。
        """
        self.make_app()
        recycle.soft_delete_order(order=self.order, actor=self.user, is_del_emp="N")
        row = self.only_row()
        self.assertEqual(row.event, EVENT_DELETED)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.deleted_at)
        self.assertEqual(row.payload["occurred_at"], self.order.deleted_at.isoformat())
        self.assertEqual(row.source_order_no, "SRC-0001")

    def test_the_soft_delete_is_idempotent_and_does_not_enqueue_twice(self):
        """幂等的第二次删除**不排**第二行（本次没有新的删除动作发生）。"""
        self.make_app()
        recycle.soft_delete_order(order=self.order, actor=self.user, is_del_emp="N")
        self.assertEqual(OpenApiDelivery.objects.count(), 1)
        self.order.refresh_from_db()
        recycle.soft_delete_order(order=self.order, actor=self.user, is_del_emp="N")
        self.assertEqual(OpenApiDelivery.objects.count(), 1)

    def test_the_peer_screenshot_write_enqueues_an_image_event(self):
        """进度图写入点②（上下游同步接收侧）——⚠️ 这条路径今天**不走**既有入队函数。"""
        self.make_app()
        key = f"{SHARED_ORDER_PREFIX}/{self.order.order_no}/{uuid.uuid4().hex}.jpg"
        applied = _apply_screenshot(
            self.order,
            kind="order.screenshot.add",
            payload={"object_key": key, "img_type": 0, "remark": "同步进来的图"},
            binding=None,               # `order.sales_user` 非空 ⇒ 用不到绑定
            target_ref="A-1",
            note_skip=lambda *args, **kwargs: None,
            direction="down",
        )
        self.assertTrue(applied)
        self.assertEqual(OrderScreenshot.objects.count(), 1)
        row = self.only_row()
        self.assertEqual(row.event, EVENT_IMAGE)
        self.assertEqual(row.payload["images"][0]["object_key"], key)
        # ⚠️ 库里存的是**稳定身份**（`object_key`），⛔ 不是 300 秒就过期的预签名 URL。
        self.assertNotIn("url", row.payload["images"][0])


class SourceOrderNoColumnClipTests(_DispatchFixture):
    """JS-03（2026-10-20 夜间审查）：投递台账列宽 64 < `Order.source_order_no` 的 128。

    超宽来源单号原来**原样**写列 ⇒ `varchar(64)` 溢出、投递行建不出来、连带同事务
    业务动作一起 500。修法 = 入列前按**模型元数据**裁（`peer_inbound._clip_to_column`，
    裁决 R19 的「有意截断、有意不报错」口径）；**报文里保持全量**（接收方拿到的
    `source_order_no` 不因我方列宽而缺尾）。

    判别力：把 create 那一行的裁剪去掉 ⇒ 本条红（列溢出，投递行建不出来）。
    """

    def test_an_over_wide_source_order_no_is_clipped_in_the_row_and_whole_in_the_payload(self):
        self.make_app()
        Order.objects.filter(pk=self.order.pk).update(source_order_no="S" * 128)
        row = self.enqueue_status(order=Order.objects.get(pk=self.order.pk))[0]
        #: 台账列：按模型元数据裁到 64（⛔ 不是手抄 64、也不是丢空）。
        self.assertEqual(row.source_order_no, "S" * 64)
        #: 报文：**全量** 128——列宽是我方台账的事，不克扣接收方。
        self.assertEqual(row.payload["source_order_no"], "S" * 128)

    def test_the_image_wire_payload_carries_a_fresh_presigned_url(self):
        """投递那一刻才把 `object_key` 换成预签名 URL（§9「图片 URL」那一格）。"""
        self.make_app()
        key = f"{SHARED_ORDER_PREFIX}/{self.order.order_no}/{uuid.uuid4().hex}.jpg"
        get_storage().put_bytes(key, JPEG, content_type="image/jpeg")
        row = dispatch.notify_order_image_added(
            self.order, spec=OPENAPI_SPEC,
            object_key=key, img_type=0, remark="", occurred_at=timezone.now()
        )[0]
        with _HttpBackend(status=200) as fake:
            dispatch.deliver_one(row.pk, spec=OPENAPI_SPEC)
        sent, _raw = _sent_body(fake.instances[0])
        self.assertEqual(len(sent["images"]), 1)
        entry = sent["images"][0]
        self.assertTrue(entry["url"], "投递报文里必须有可下载的 URL")
        self.assertNotIn("object_key", entry)
        self.assertEqual(entry["img_type"], 0)
        # 库里那一份仍是稳定身份（⛔ 没被这次投递写脏）。
        row.refresh_from_db()
        self.assertEqual(row.payload["images"][0]["object_key"], key)

    def test_the_internal_claim_enqueues_an_image_event(self):
        """进度图写入点①（内部认领，走真 HTTP 端点）。"""
        self.make_app()
        client = make_logged_in_client(
            username="t8-shot-admin",
            groups=("系统管理员",),
            nodes=("order.upload_screenshot", "order.view_screenshot"),
        )
        base = f"/api/business/orders/{self.order.order_no}/screenshots"
        ticket = client.post(f"{base}/ticket", {"ext": "jpg", "img_type": 0}, format="json")
        row_id = ticket.data["data"]["screenshot_id"]
        shot = OrderScreenshot.objects.get(pk=row_id)
        get_storage().put_bytes(shot.object_key, JPEG, content_type="image/jpeg")
        claimed = client.post(base, {"screenshot_id": row_id}, format="json")
        self.assertEqual(claimed.status_code, 200, claimed.content[:300])
        self.assertEqual(claimed.data["code"], 0, claimed.data)
        delivery = self.only_row()
        self.assertEqual(delivery.event, EVENT_IMAGE)
        self.assertEqual(delivery.payload["images"][0]["object_key"], shot.object_key)


# ── 去重判据的两半 ────────────────────────────────────────────────────────────


class DedupTests(_DispatchFixture):
    """⭐ 去重判据（`dispatch._is_duplicate`）——两半都要能红。"""

    #: 上游事件的固定时刻（`T1` 是本地已应用到的时刻；`T4` 留给"去重之后再来一次真变化"）。
    T1 = datetime(2026, 10, 8, 9, 0, tzinfo=dt_timezone.utc)
    T2 = datetime(2026, 10, 8, 10, 0, tzinfo=dt_timezone.utc)
    T3 = datetime(2026, 10, 8, 11, 0, tzinfo=dt_timezone.utc)
    T4 = datetime(2026, 10, 8, 12, 0, tzinfo=dt_timezone.utc)

    def _link(self):
        # ⭐ D-0364（案 A）：本组测的是**去重**、不是池内豁免；夹具单原为 `WAITING_GRAB`
        #    ⇒ 池内豁免上线后「本地池内态 + 入站 IN_PROGRESS」不再改状态、也不再推
        #    （那是对的），本组两条就空转了。把本地单摆成**代练中**（豁免只护池内两态）
        #    ⇒ 去重场景照旧成立、判别力不变。
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        binding = PeerUpstreamBinding.objects.create(
            peer_name="推送夹具工作室甲",
            peer_base_url="https://peer-t8.test.invalid",
            peer_account="peer-t8-account",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-t8-local.test.invalid",
            bound_by=self.user,
        )
        PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=binding,
            peer_order_no="T8-PEER-1",
            last_applied_at=self.T1,
        )
        return binding

    def _event(self, *, occurred_at, status=OrderStatus.IN_PROGRESS):
        return {
            "kind": "order.update",
            "target_ref": "T8-PEER-1",
            "occurred_at": occurred_at.isoformat(),
            "payload": {
                "status": status,
                "hall_price": "8.00",
                "total_price": "10.00",
                "game_name": self.game.name,
            },
        }

    def test_a_duplicate_status_event_in_the_pending_window_is_enqueued_once(self):
        """⭐⭐ 真实路径：上游**两次**送来**同一个状态**（at-least-once 的常态），
        第二次**不许**再排一行（判据 = 同一订单 + 同一事件 + 同一状态）。

        失败方向：把 `_is_duplicate` 去掉 ⇒ 本用例红（两次 ⇒ 两行）。
        """
        self.make_app()
        binding = self._link()
        first = apply_events([self._event(occurred_at=self.T2)], binding=binding)
        second = apply_events([self._event(occurred_at=self.T3)], binding=binding)
        self.assertEqual((first.applied, second.applied), (1, 1), (first, second))
        rows = list(OpenApiDelivery.objects.all())
        self.assertEqual(
            len(rows), 1,
            "同一订单 + 同一事件 + 同一状态被重复入队了（对方会收到两条一样的推送）",
        )
        self.assertEqual(rows[0].payload["status"], str(OrderStatus.IN_PROGRESS))

    def test_a_real_status_change_after_a_duplicate_is_still_enqueued(self):
        """正对照：同状态被去重**之后**，一个**真的**新状态照样排得出去。

        没有这条，'去重'可以退化成'整条路只排一行'而本组用例全绿。
        """
        self.make_app()
        binding = self._link()
        apply_events([self._event(occurred_at=self.T2)], binding=binding)
        apply_events([self._event(occurred_at=self.T3)], binding=binding)   # 同一状态 ⇒ 去重
        self.assertEqual(OpenApiDelivery.objects.count(), 1)
        apply_events(
            [self._event(occurred_at=self.T4, status=OrderStatus.ISSUE)], binding=binding
        )
        statuses = [row.payload["status"] for row in OpenApiDelivery.objects.order_by("id")]
        self.assertEqual(
            statuses, [str(OrderStatus.IN_PROGRESS), str(OrderStatus.ISSUE)]
        )

    def test_a_price_only_update_does_not_fake_a_status_change(self):
        """⭐ 只改价 / 状态不在接受集里的事件**不推**状态事件。

        这一支的 `save()` 无条件写 `status` 列（不在接受集里就是**把旧值写回**）⇒
        把推送放在无条件分支会对着第三方说一句假话（「状态变成 PENDING_REVIEW」，
        而库里那个状态一个字都没变）。
        """
        self.make_app()
        binding = self._link()
        outcome = apply_events(
            [self._event(occurred_at=self.T2, status=OrderStatus.PENDING_REVIEW)],
            binding=binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)   # 事件照旧算 applied（裁定 R3）
        self.assertEqual(OpenApiDelivery.objects.count(), 0)

    def test_a_second_delete_in_the_pending_window_is_enqueued_once(self):
        self.make_app()
        row = self.enqueue_status()[0]
        self.assertEqual(row.event, EVENT_STATUS)
        OpenApiDelivery.objects.all().delete()
        recycle.soft_delete_order(order=self.order, actor=self.user, is_del_emp="N")
        self.assertEqual(OpenApiDelivery.objects.count(), 1)

    def test_a_delete_after_a_restore_is_enqueued_again(self):
        """⭐ **删 → 恢复 → 再删**：第二次删除**必须**再排一行。

        失败方向：把删除那一档的判据放宽成"最近一条行存在就跳过"（⛔ 不看它是不是在飞）
        ⇒ 本用例红——而症状是**第三方永远不知道这单现在是删的**（一条静默丢事件）。
        """
        self.make_app()
        recycle.soft_delete_order(order=self.order, actor=self.user, is_del_emp="N")
        first = self.only_row()
        # 模拟"第一行已经投出去了"（终态 `done`）。
        first.status = OpenApiDelivery.Status.DONE
        first.processed_at = timezone.now()
        first.save(update_fields=["status", "processed_at"])
        self.order.refresh_from_db()
        recycle.restore_order(order=self.order, actor=self.user)
        self.order.refresh_from_db()
        recycle.soft_delete_order(order=self.order, actor=self.user, is_del_emp="N")
        rows = list(OpenApiDelivery.objects.order_by("id"))
        self.assertEqual(
            len(rows), 2,
            "删 → 恢复 → 再删 的第二次删除被'最近一条行'吃掉了（第三方永远不知道这单是删的）",
        )
        self.assertTrue(all(row.event == EVENT_DELETED for row in rows))

    def test_the_same_image_key_is_enqueued_once(self):
        self.make_app()
        key = f"{SHARED_ORDER_PREFIX}/{self.order.order_no}/{uuid.uuid4().hex}.jpg"
        for _ in range(2):
            dispatch.notify_order_image_added(
                self.order, spec=OPENAPI_SPEC, object_key=key, img_type=0, remark="",
                occurred_at=timezone.now(),
            )
        self.assertEqual(OpenApiDelivery.objects.count(), 1)

    def test_a_different_image_key_is_a_different_event(self):
        """正对照：不同的图是**不同的事**，⛔ 不许被去重吃掉。"""
        self.make_app()
        for _ in range(2):
            dispatch.notify_order_image_added(
                self.order,
                spec=OPENAPI_SPEC,
                object_key=f"{SHARED_ORDER_PREFIX}/{self.order.order_no}/{uuid.uuid4().hex}.jpg",
                img_type=0, remark="",
                occurred_at=timezone.now(),
            )
        self.assertEqual(OpenApiDelivery.objects.count(), 2)


class PoolHeldPushGateTests(_DispatchFixture):
    """⭐ 案 A / D-0364（评审 M1 必修）：池内豁免保住本地态时，第三方推送闸**必须闭着**。

    ⚠️ 本条是**第三方推送闸的判别性测试**（评审实锤：闸回退成裸接受集后全套件
    55 条相关用例全绿——`DedupTests` 被迫把夹具单摆成 `IN_PROGRESS` 绕开豁免后，
    「池内态 ⇒ 不推」这半边就没有任何用例钉着了）。本条把它钉回来：
    **本地单保持 `WAITING_GRAB`（池内态）+ 入站 `order.update(IN_PROGRESS)`**
    ⇒ 状态不被写、开放平台**零推送行**。

    ⚠️ **变异自证口径（必须能红）**：把 `apps/business/peer_inbound.py` 推送闸的
    `if inbound_status_written:` 临时回退成裸接受集闸
    `if str(raw_status) in DOWNSTREAM_ACCEPTED_STATUSES:`（⭐ 只回退**推送闸**这一处，
    状态赋值那处的豁免不动 ⇒ 状态仍被保住、闸却放行 = 「对第三方推一个没发生的
    状态变化」的说假话形态）⇒ 本用例在 `OpenApiDelivery.count()` 上红
    （0 != 1，载荷 status=IN_PROGRESS）。原样还原后复绿。
    """

    #: 上游事件的固定时刻（`T1` 是本地已应用到的时刻；语义照 `DedupTests`）。
    T1 = datetime(2026, 10, 8, 9, 0, tzinfo=dt_timezone.utc)
    T2 = datetime(2026, 10, 8, 10, 0, tzinfo=dt_timezone.utc)

    def _pool_link(self):
        """建链但**不**动夹具单的状态（它保持 `WAITING_GRAB` = 池内态——本条的
        被测形状；⛔ 不要复用 `DedupTests._link`，那一条会先把单摆成代练中）。"""
        binding = PeerUpstreamBinding.objects.create(
            peer_name="推送夹具工作室乙",
            peer_base_url="https://peer-t8-pool.test.invalid",
            peer_account="peer-t8-pool-account",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-t8-pool-local.test.invalid",
            bound_by=self.user,
        )
        PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=binding,
            peer_order_no="T8-POOL-1",
            last_applied_at=self.T1,
        )
        return binding

    def _event(self, *, occurred_at, status=OrderStatus.IN_PROGRESS):
        return {
            "kind": "order.update",
            "target_ref": "T8-POOL-1",
            "occurred_at": occurred_at.isoformat(),
            "payload": {
                "status": status,
                "hall_price": "8.00",
                "total_price": "10.00",
                "game_name": self.game.name,
            },
        }

    def test_a_pool_held_update_pushes_nothing_to_the_openapi(self):
        """本地待抢单 + 入站 IN_PROGRESS（豁免）⇒ 状态保住、开放平台零推送行。"""
        self.make_app()
        binding = self._pool_link()

        outcome = apply_events([self._event(occurred_at=self.T2)], binding=binding)

        # 事件照旧 applied（R3 同款：豁免不是跳过）。
        self.assertEqual(outcome.applied, 1, outcome)
        self.order.refresh_from_db()
        # 状态被池内豁免保住（对照哨兵：没有豁免它会被写成 IN_PROGRESS）。
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        # ⭐ 判别力所在：闸闭着 ⇒ 开放平台**零**推送行
        #（变异自证见类 docstring：闸回退成裸接受集 ⇒ 这里 0 != 1，红）。
        self.assertEqual(OpenApiDelivery.objects.count(), 0)


# ── 配置面 / 可用性（§7.1）────────────────────────────────────────────────────


class EligibilityTests(_DispatchFixture):
    """§7.1：地址空 / 回调停用 / 应用停用 ⇒ 不排队；行排上了而应用不可用 ⇒ 终态。"""

    def test_no_callback_url_means_no_row(self):
        self.make_app(callback_url="")
        self.assertEqual(self.enqueue_status(), [])
        self.assertEqual(OpenApiDelivery.objects.count(), 0)

    def test_a_disabled_callback_means_no_row(self):
        self.make_app(callback_state=CallbackState.DISABLED)
        self.assertEqual(self.enqueue_status(), [])
        self.assertEqual(OpenApiDelivery.objects.count(), 0)

    def test_a_disabled_app_means_no_row(self):
        """停用应用 ⇒ **整条对外通道都不通**（`CallbackState` 的 docstring 逐字）。"""
        self.make_app(state=ApiAppState.DISABLED)
        self.assertEqual(self.enqueue_status(), [])
        self.assertEqual(OpenApiDelivery.objects.count(), 0)

    def test_only_the_eligible_app_gets_a_row(self):
        """多应用租户：只有配了回调的那个拿到行（⛔ 不许给没配的应用排空行）。"""
        eligible = self.make_app()
        self.make_app(name="推送夹具应用乙", app_key="t8dispatchkey0002", callback_url="")
        rows = self.enqueue_status()
        self.assertEqual([row.app_id for row in rows], [eligible.pk])

    def test_a_row_whose_app_became_unavailable_is_terminal_without_http(self):
        """行已排上、应用随后被停用 ⇒ 终态 `failed` + 人话 + **零 HTTP**（⛔ 不重试、⛔ 不报错）。"""
        app = self.make_app()
        row = self.enqueue_status()[0]
        app.callback_state = CallbackState.DISABLED
        app.save(update_fields=["callback_state"])
        with _HttpBackend(status=200) as fake:
            outcome = dispatch.deliver_one(row.pk, spec=OPENAPI_SPEC)
        self.assertEqual(outcome, "dropped")
        self.assertEqual(fake.instances, [])
        row.refresh_from_db()
        self.assertEqual(row.status, OpenApiDelivery.Status.FAILED)
        self.assertEqual(row.reason, dispatch.CALLBACK_DISABLED_REASON)
        self.assertEqual(OpenApiDelivery.objects.count(), 1, "行必须保留（页面要看得到）")

    def test_an_app_disabled_and_then_reenabled_keeps_the_old_rows_terminal(self):
        """⚠️ 如实登记的一条口径：终态行**不会**因为应用重新启用而复活（手工重投是显式动作）。"""
        app = self.make_app()
        row = self.enqueue_status()[0]
        app.state = ApiAppState.DISABLED
        app.save(update_fields=["state"])
        with _HttpBackend(status=200) as fake:
            dispatch.deliver_one(row.pk, spec=OPENAPI_SPEC)
        self.assertEqual(fake.instances, [])
        row.refresh_from_db()
        self.assertEqual(row.status, OpenApiDelivery.Status.FAILED)
        self.assertEqual(row.reason, dispatch.APP_DISABLED_REASON)


# ── ⭐ 族守卫：上家侧那三个 `notify_*` 不收别族的 spec（第二期 T2）──────────────


class NotifierFamilyGuardTests(_DispatchFixture):
    """⭐ 三个上家侧 `notify_*` 包装的**族守卫**（第二期 T2；计划文件「T1b 收口后的追加裁定」第 2 条）。

    ## 这条用例挡的是"必填 `spec`"挡不住的那一半

    T1b 把 `spec` 改成必填 ⇒ 只挡住**漏传**（`TypeError`）。**传错族**照样能跑到深处：
    那三个函数内部算收件人用的是**本族那一套**（`eligible_apps()` 查上家侧的应用表），
    而建行走的是传入 spec 的 `delivery_model` ⇒ 传打手侧那一份必然"**半对半错**"，
    且两种结果**都说不到点子上**（⚠️ 实测，见 `_require_openapi_family` 的 docstring）：

    - 租户**没有**上家侧应用 ⇒ 收件人空 ⇒ **一行都不写、没有任何异常**（静默）；
    - 租户**有**上家侧应用 ⇒ 抛一句**关于模型类型**的话（实测原文：
      `Cannot query "…": Must be "TakerApp" instance.`）—— 响亮但**指错地方**。

    ⇒ 本组用例要的读数是"**一句点名两族**的 `ValueError`"＋"**零建行**"，
    ⛔ 不是上面那两种（所以下面第 1 条除了断言异常类型，还断言消息里的两个名字）。

    ## 两半都要有

    1. 传 `TAKER_SPEC` ⇒ 当场 `ValueError`，且**一行都没建**（⛔ 不是"抛了但还是写了"）；
    2. ⭐ **对照**：传 `OPENAPI_SPEC` ⇒ **不抛**且真的建了行 —— 否则上一条会被
       "函数永远抛"这种坏实现蒙绿（`AGENTS.md` §5-E）。

    ⚠️ 夹具用**合成值**；这一组**不发任何 HTTP**（守卫在入队之前，与投递无关）。

    ⚠️ **弄红它**：把 `_require_openapi_family` 里的判断改成恒假（`if False:`）⇒ 三条守卫用例
    当场红、对照仍绿；或把某一个包装里的 `_require_openapi_family(...)` 那一行删掉
    ⇒ 只有**那一个**包装的用例红（证明三个包装各自都真的调了守卫）。
    """

    def test_the_status_wrapper_rejects_the_other_family_spec(self):
        """`order.status_changed` 包装：传打手侧那一份 ⇒ `ValueError`，且**零建行**。"""
        self.make_app()          # 上家侧配好回调：守卫若失效，这里会**真建行**（而不是碰巧空）
        with self.assertRaises(ValueError) as caught:
            dispatch.notify_order_status_changed(
                self.order,
                spec=TAKER_SPEC,
                status=OrderStatus.IN_PROGRESS,
                occurred_at=timezone.now(),
            )
        message = str(caught.exception)
        # ⚠️ 消息必须**点名**：这是上家侧的包装 + 打手侧该用哪一族（⛔ 不是一句无参数的 ValueError）。
        self.assertIn("上家侧", message)
        self.assertIn("takerapi", message)
        self.assertIn("notify_order_status_changed", message)
        self.assertEqual(OpenApiDelivery.objects.count(), 0)
        self.assertEqual(TakerApiDelivery.objects.count(), 0)

    def test_the_deleted_wrapper_rejects_the_other_family_spec(self):
        """`order.deleted` 包装：同款守卫（第二期 spec §2.2 第 6 项：三个包装各归各族）。"""
        self.make_app()
        with self.assertRaises(ValueError) as caught:
            dispatch.notify_order_deleted(
                self.order, spec=TAKER_SPEC, occurred_at=timezone.now()
            )
        self.assertIn("notify_order_deleted", str(caught.exception))
        self.assertEqual(OpenApiDelivery.objects.count(), 0)
        self.assertEqual(TakerApiDelivery.objects.count(), 0)

    def test_the_image_wrapper_rejects_the_other_family_spec(self):
        """`order.image_added` 包装：同款守卫。"""
        self.make_app()
        with self.assertRaises(ValueError) as caught:
            dispatch.notify_order_image_added(
                self.order,
                spec=TAKER_SPEC,
                object_key="shared/taker-guard/aaaaaaaa.jpg",
                img_type=0,
                remark="",
                occurred_at=timezone.now(),
            )
        self.assertIn("notify_order_image_added", str(caught.exception))
        self.assertEqual(OpenApiDelivery.objects.count(), 0)
        self.assertEqual(TakerApiDelivery.objects.count(), 0)

    def test_the_control_openapi_spec_still_enqueues(self):
        """⭐ **对照**：传 `OPENAPI_SPEC` ⇒ **不抛**，而且真的建了行（守卫必须是"收窄"、⛔ 不是"堵死"）。

        ⚠️ 缺了这条，上面三条会被"函数改成永远抛"蒙绿 —— 而那正是把上家侧整条推送打死的形状。
        """
        self.make_app()
        rows = dispatch.notify_order_status_changed(
            self.order,
            spec=OPENAPI_SPEC,
            status=OrderStatus.IN_PROGRESS,
            occurred_at=timezone.now(),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].event, EVENT_STATUS)
        self.assertEqual(OpenApiDelivery.objects.count(), 1)
        self.assertEqual(TakerApiDelivery.objects.count(), 0, "⛔ 别族的台账一行都不许有")

    def test_the_generic_engine_entries_still_take_any_family_spec(self):
        """⭐ 守卫**不许**扩大到通用引擎入口（计划文件那条裁定的后半句）。

        判据：`claim_due_deliveries` / `prune_done_deliveries` / `run_round` 收打手侧那一份
        **不抛** TypeError/ValueError（第二族的常驻命令正是这么调它们的）。
        ⚠️ 这里**不打桩投递**：当前租户里没有任何投递行 ⇒ 读数是空的，足以分辨"收下了"
        与"被守卫挡住"（`E-0029`：桩接没接上本身要有判据）。
        ⚠️ 反过来说：**⛔ 不许**为了过这条而在引擎入口加"内部再兜一层"的软化。
        """
        now = timezone.now()
        self.assertEqual(dispatch.claim_due_deliveries(now=now, spec=TAKER_SPEC), [])
        self.assertEqual(dispatch.prune_done_deliveries(now=now, spec=TAKER_SPEC), 0)
        self.assertEqual(
            dispatch.run_round(now=now, spec=TAKER_SPEC),
            {
                "delivered": 0, "retried": 0, "failed": 0, "dropped": 0,
                "pruned": 0, "errors": [],
            },
        )


# ── 顺序与清理 ────────────────────────────────────────────────────────────────


class OrderingAndPruneTests(_DispatchFixture):
    """§7.4「同一订单按产生顺序」+ 机会式清理。"""

    def test_the_full_round_delivers_and_prunes(self):
        """`run_round()` 端到端：取活 ⇒ 投递 ⇒ 记账 ⇒ 清理（一条正向读数）。"""
        self.make_app()
        row = self.enqueue_status()[0]
        with _HttpBackend(status=200) as fake:
            summary = dispatch.run_round(spec=OPENAPI_SPEC)
        self.assertEqual(summary["delivered"], 1, summary)
        self.assertEqual(summary["errors"], [])
        self.assertEqual(len(fake.instances[0].calls), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, OpenApiDelivery.Status.DONE)

    def test_a_newer_row_for_the_same_order_waits_for_the_older_one(self):
        """⭐ 同一订单**按产生顺序**：更早那条还没投出去，更晚那条本轮**不许**被取。

        失败方向：去掉 `_drop_out_of_order_rows` ⇒ 本轮会把两行都取走（顺序由 HTTP 往返决定）。
        """
        self.make_app()
        older = self.enqueue_status(status=OrderStatus.IN_PROGRESS)[0]
        newer = self.enqueue_status(status=OrderStatus.PENDING_REVIEW)[0]
        now = timezone.now()
        claimed = dispatch.claim_due_deliveries(now=now, spec=OPENAPI_SPEC)
        self.assertEqual(claimed, [older.pk], "同一订单不许并发/乱序投递")
        # 更早那条投完（变终态）之后，更晚那条才取得到。
        older.status = OpenApiDelivery.Status.DONE
        older.next_attempt_at = None
        older.save(update_fields=["status", "next_attempt_at"])
        self.assertEqual(
            dispatch.claim_due_deliveries(now=now + timedelta(minutes=10), spec=OPENAPI_SPEC), [newer.pk]
        )

    def test_claiming_takes_a_lease_and_skips_rows_that_are_not_due(self):
        """取件打租约：没到点的行取不到（租约把"投递中"表达在 `next_attempt_at` 上）。"""
        self.make_app()
        row = self.enqueue_status()[0]
        now = timezone.now()
        self.assertEqual(dispatch.claim_due_deliveries(now=now, spec=OPENAPI_SPEC), [row.pk])
        row.refresh_from_db()
        self.assertEqual(
            row.next_attempt_at, now + timedelta(seconds=dispatch.LEASE_SECONDS)
        )
        self.assertEqual(dispatch.claim_due_deliveries(now=now, spec=OPENAPI_SPEC), [], "租约期内不许被再取一次")

    def test_prune_deletes_only_old_done_rows_and_caps_at_five_hundred(self):
        """清理：只删**老 + `done`**，而且一次最多 500 行。"""
        self.make_app()
        now = timezone.now()
        old = now - timedelta(days=dispatch.RETENTION_DAYS + 1)
        fresh = now - timedelta(days=1)
        OpenApiDelivery.objects.bulk_create(
            [
                OpenApiDelivery(
                    app_id=self.app_id, event=EVENT_STATUS, order_no=f"OLD-{index:04d}",
                    payload={"event": EVENT_STATUS}, status=OpenApiDelivery.Status.DONE,
                )
                for index in range(501)
            ]
            + [
                OpenApiDelivery(
                    app_id=self.app_id, event=EVENT_STATUS, order_no="PENDING-1",
                    payload={"event": EVENT_STATUS}, status=OpenApiDelivery.Status.PENDING,
                ),
                OpenApiDelivery(
                    app_id=self.app_id, event=EVENT_STATUS, order_no="FAILED-1",
                    payload={"event": EVENT_STATUS}, status=OpenApiDelivery.Status.FAILED,
                ),
                OpenApiDelivery(
                    app_id=self.app_id, event=EVENT_STATUS, order_no="FRESH-1",
                    payload={"event": EVENT_STATUS}, status=OpenApiDelivery.Status.DONE,
                ),
            ]
        )
        OpenApiDelivery.objects.filter(order_no__startswith="OLD-").update(created_at=old)
        OpenApiDelivery.objects.filter(order_no__in=("PENDING-1", "FAILED-1")).update(
            created_at=old
        )
        OpenApiDelivery.objects.filter(order_no="FRESH-1").update(created_at=fresh)

        deleted = dispatch.prune_done_deliveries(now=now, spec=OPENAPI_SPEC)
        self.assertEqual(deleted, dispatch.MAX_PRUNE_ROWS)
        self.assertEqual(OpenApiDelivery.objects.filter(order_no__startswith="OLD-").count(), 1)
        # ⛔ `pending`（待办）与 `failed`（告警面）**不许**被清掉；`done` 但没到期的不清。
        self.assertTrue(OpenApiDelivery.objects.filter(order_no="PENDING-1").exists())
        self.assertTrue(OpenApiDelivery.objects.filter(order_no="FAILED-1").exists())
        self.assertTrue(OpenApiDelivery.objects.filter(order_no="FRESH-1").exists())

    @property
    def app_id(self) -> int:
        return ApiApp.objects.get(name=APP_NAME).pk


# ── 常驻命令 ──────────────────────────────────────────────────────────────────


class LoopCommandTests(_DispatchFixture):
    """`openapi_dispatch` 的开关契约与退出码契约。"""

    def test_once_and_loop_are_mutually_exclusive(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("openapi_dispatch", "--once", "--loop")
        self.assertIn("--once", str(ctx.exception))

    def test_an_unknown_tenant_is_a_loud_error(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("openapi_dispatch", "--once", "--tenant", "t8-no-such-schema")
        self.assertIn("t8-no-such-schema", str(ctx.exception))

    def test_once_runs_a_round_and_exits_zero_on_expected_failures(self):
        """⭐ 退出码契约：**预期内**的投递失败（对方 500）**不**让 `--once` 非零。

        失败方向：把投递失败也算成"未预期失败"⇒ 手工跑这一趟永远非零 ⇒ 运维不再看它。
        """
        self.make_app()
        self.enqueue_status()
        with _HttpBackend(status=500):
            call_command("openapi_dispatch", "--once", "--tenant", connection.schema_name)
        row = self.only_row()
        self.assertEqual(row.status, OpenApiDelivery.Status.PENDING)
        self.assertEqual(row.attempts, 1)

    def test_once_exits_non_zero_on_an_unexpected_failure(self):
        """未预期失败（这里用"预签名炸掉"模拟）⇒ 非零 + 台账行**不被记账**。"""
        self.make_app()
        row = dispatch.notify_order_image_added(
            self.order,
            spec=OPENAPI_SPEC,
            object_key=f"{SHARED_ORDER_PREFIX}/{self.order.order_no}/x.jpg",
            img_type=0, remark="",
            occurred_at=timezone.now(),
        )[0]
        with mock.patch(
            "apps.openapi.dispatch._wire_payload", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    "openapi_dispatch", "--once", "--tenant", connection.schema_name
                )
        self.assertEqual(ctx.exception.returncode, 1)
        row.refresh_from_db()
        self.assertEqual(row.status, OpenApiDelivery.Status.PENDING, "没发出去的行不许假装投过")


# ── 剥注释探针：引擎的行为位里不再有具体模型名（T1b）─────────────────────────────


class ModelNameProbeTests(SimpleTestCase):
    """⭐ 共用投递引擎 `apps/openapi/dispatch.py` 的**代码位**里不许再出现具体模型名（T1b）。

    ## 这条用例的来历

    T1（把投递引擎改成经 `CredentialSpec` 驱动那一轮）用一条**即跑即弃的脚本**证明了这件事
    （"剥掉注释与 docstring 之后，`dispatch.py` 里不再有具体模型名"），但那条脚本**没进仓库**
    ⇒ 这条约束只活在一份报告里。本用例把它落成**真判据**（spec §2.2 末段点名的那条探针）。

    ## 判据（写在"记号"上，⛔ 不是"文件文本"）

    用 `tokenize` 收 `NAME` 记号（照
    `tests/test_openapi_token.py::test_the_ttl_literal_appears_only_once_in_code_sites`）：
    注释是 `COMMENT` 记号、docstring 是 `STRING` 记号 ⇒ **它们本来就不在 `NAME` 里**，
    所以"剥掉注释与 docstring"这条口径**由记号类型本身保证**，
    ⛔ 不是拿正则去删 `#` / 三引号（那种写法会被字符串里的 `#` 或嵌套引号骗到，
    与 `AGENTS.md` §5-G「断言…要打在标记上，⛔ 不是文件文本」同族）。

    ## 两条腿（⭐ 第二条腿是"这条钉子能红"的证明）

    1. `apps/openapi/dispatch.py` ⇒ 命中**必须 0**（那一族的表 / 枚举只能经 `spec` 现取）；
    2. `apps/openapi/credentials.py`（= spec 定义处）⇒ 命中**必须 ≥1**：模型名**允许**住在这里。
       ⚠️ 缺了这条腿，"探针本身写坏了"（名字拼错 / 扫错文件 / 永远返回空）也会绿 ——
       那正是 `AGENTS.md` §5-E「结构性检查自己的断言也必须能失败」那条要防的形状。
    """

    #: 被搜的四个名字（spec §2.2 点名的具体模型与它那两个枚举）。
    #: ⚠️ 按**记号**整字比对：`ApiApp` 不会命中 `ApiAppState`（后者是**另一个** NAME 记号）。
    MODEL_NAMES = frozenset({"OpenApiDelivery", "ApiApp", "CallbackState", "ApiAppState"})

    @staticmethod
    def _code_name_sites(path: pathlib.Path) -> list[tuple[str, int]]:
        """`path` 的**代码位**（⛔ 不含注释 / docstring）里出现的模型名 ⇒ `[(名字, 行号)]`。"""
        sites: list[tuple[str, int]] = []
        with path.open("rb") as handle:
            for tok in tokenize.tokenize(handle.readline):
                if tok.type == tokenize.NAME and tok.string in ModelNameProbeTests.MODEL_NAMES:
                    sites.append((tok.string, tok.start[0]))
        return sites

    def test_the_engine_has_no_concrete_model_name_in_code_sites(self):
        """第一腿：引擎文件 0 命中（⛔ 一个具体模型名都不许出现在行为位里）。"""
        engine = pathlib.Path(dispatch.__file__).resolve()
        self.assertEqual(engine.name, "dispatch.py")
        sites = self._code_name_sites(engine)
        self.assertEqual(
            sites, [],
            f"{engine.name} 的代码位里出现了具体模型名（应当只经 `spec` 现取）：{sites}",
        )

    def test_the_probe_itself_can_hit_something(self):
        """第二腿（对照）：同一个探针指向 spec 定义处 ⇒ **有**命中（≥1）。

        ⚠️ 这条不是"多余的正面用例"：它把"探针恒空"这个**假绿**形态排除掉
        —— 第一腿说"0 命中"，只有这一腿能说明"0"是**扫到了但没命中**，
        而不是"扫描器坏了 / 扫了个空文件"。
        """
        spec_module = pathlib.Path(credentials_module.__file__).resolve()
        self.assertEqual(spec_module.name, "credentials.py")
        sites = self._code_name_sites(spec_module)
        self.assertGreaterEqual(
            len(sites), 1,
            f"{spec_module.name} 里一个模型名都没有 ⇒ 探针多半坏了（它才是 spec 定义处）",
        )
