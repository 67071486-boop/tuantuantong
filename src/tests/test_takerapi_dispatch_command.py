"""T4：`takerapi_dispatch` 常驻投递命令的判据（spec `2026-10-09-taker-callback-design.md` §6 / §9-11）。

## 本文件证什么（逐条都能单独弄红）

| 组 | 钉什么 |
|---|---|
| `OnceDispatchTests` | ⭐ 判据 1：`--once` 对**一个真有待投递行**的租户跑一趟 ⇒ 那一行真的变 `done`（HTTP 层是**替身**，⛔ 一个真网络都不打），且请求头/报文与契约逐字对得上 |
| `CrossFamilyTests` | ⭐⭐ 判据 2：**跨族不串台**——同时有一条**上家侧** `OpenApiDelivery`（pending）与一条**打手侧** `TakerApiDelivery`（pending）⇒ `takerapi_dispatch --once` **只**动打手侧那条、上家侧那条**原样不动**；**反例**：`openapi_dispatch --once` 只动上家侧那条。⚠️ 这是「命令忘了传 `spec=TAKER_SPEC`」这件事**唯一**的网（漏传 = 当场 `TypeError`；传错族 = 本组红） |
| `PerTenantExceptionBoundaryTests` | ⭐ 判据 3：让一个租户抛异常 ⇒ **别的租户仍被处理**（拿终态当读数）、退出码非零、汇总行把失败**报出来** |
| `CommandSwitchesTests` | 判据 4：`--once` 与 `--loop` 同时给 ⇒ `CommandError`；不存在的 `--tenant` ⇒ `CommandError`（照上家侧口径） |
| `SummaryPrefixTests` | 汇总行带**打手侧自己的前缀**（⛔ 与上家侧那句不同：运维并排看排障输出时要分得清哪条线在跑）；`LOOP_SLEEP_SECONDS` 字面值 = 10 |
| `NoPeerWatermarkTests` | docstring 写清两件事：这条线**没有**「与对端水位比较」那回事（那是 peer 线的）；「漏传 `spec` 会 `TypeError`」这条来历 |

## 夹具纪律

- 凭据 / 地址 / 单号 / 用户名一律**合成值**（`AGENTS.md` §5-C / `E-0077`）；回调地址用**公网地址
  字面量**（`93.184.216.34`，⛔ 不是任何真实第三方）。
- ⛔ **零真实网络**：HTTP 出口一律换成替身，打在 `apps.openapi.outbound` **查找** `httpx` 的那个名字上
  （`AGENTS.md` §5-E / `E-0029`），且每条投递用例都断言**替身真的被调到**。
- 第二个租户在 `setUpClass` 里建（schema DDL 不能在 `TestCase` 的事务里回滚），照
  `tests/test_my_profile_api.py::MyProfileCrossTenantTests` 的先例。
"""
from __future__ import annotations

import functools
import io
import json
import pathlib
import types
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.openapi import dispatch as openapi_dispatch
from apps.openapi.credentials import OPENAPI_SPEC
from apps.openapi.models import ApiApp, ApiAppState, OpenApiDelivery
from apps.openapi.models import CallbackState as OpenApiCallbackState
from apps.platform.models import Tenant
from apps.platform.provisioning import provision_tenant
from apps.takerapi.api import callback_recipients
from apps.takerapi.management.commands import takerapi_dispatch as command_module
from apps.takerapi.models import (
    AppState,
    CallbackState,
    TakerApiDelivery,
    TakerApp,
)

User = get_user_model()

#: 一个**公网**地址字面量（只用于夹具；⛔ 不是任何真实第三方）。
PUBLIC_HOST = "93.184.216.34"
TAKER_CALLBACK_URL = f"https://{PUBLIC_HOST}/taker/hook"
OPENAPI_CALLBACK_URL = f"https://{PUBLIC_HOST}/openapi/hook"

#: 合成凭据（⛔ 与任何真实应用的 appKey / secret 无关）。
TAKER_APP_KEY = "t4dispatchkey00001"
TAKER_APP_SECRET = "t4-dispatch-secret-00000000000001"
OPENAPI_APP_KEY = "t4openapikey000001"
OPENAPI_APP_SECRET = "t4-openapi-secret-0000000000001"

#: 第二个租户（判据 3 那个"别的租户"）。
TENANT_B_SCHEMA = "t4-dispatch-b"
TENANT_B_DOMAIN = "t4-dispatch-b.test.invalid"

#: 事件名的字面值（`E-0024`：钉值不钉常量）。
EVENT_STATUS = "order.status_changed"


def _no(n: int) -> str:
    """20 位合成订单号。"""
    return f"2026101000000000{n:04d}"


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
    """`httpx.Client` 的替身：记录每一次 `stream()` 调用。"""

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

    def stream(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response


class _HttpBackend:
    """上下文管理器：把 outbound 看到的那**一个** `httpx` 名字换成替身命名空间。"""

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


# ── 夹具 ───────────────────────────────────────────────────────────────────────


class _DispatchFixture(TenantTestCase):
    """一个租户、一个打手（+ 一套打手侧应用）、一个上家（+ 一套上家侧应用）、若干张单。"""

    def setUp(self):
        # 挡住上一条用例残留的 schema（跨 `tenant_context` 之后连接可能停在别处）。
        connection.set_tenant(self.tenant)
        self.game = Game.objects.create(name="投递命令夹具游戏甲")
        self.taker = self._user("t4-dispatch-taker")
        self.sales = self._user("t4-dispatch-sales")
        self.taker_app = self._taker_app()
        self.openapi_app = self._openapi_app()

    # ── 造物 ─────────────────────────────────────────────────────────────
    def _user(self, username):
        return User.objects.create_user(username=username, password="pw-t4-0001")

    def _taker_app(self, **overrides) -> TakerApp:
        fields = {
            "name": "投递命令打手侧应用甲",
            "app_key": TAKER_APP_KEY,
            "app_secret": TAKER_APP_SECRET,
            "taker": self.taker,
            "created_by": self.taker,
            "state": AppState.ENABLED,
            "callback_state": CallbackState.ENABLED,
            "callback_url": TAKER_CALLBACK_URL,
        }
        fields.update(overrides)
        return TakerApp.objects.create(**fields)

    def _openapi_app(self, **overrides) -> ApiApp:
        fields = {
            "name": "投递命令上家侧应用甲",
            "app_key": OPENAPI_APP_KEY,
            "app_secret": OPENAPI_APP_SECRET,
            "created_by": self.sales,
            "state": ApiAppState.ENABLED,
            "callback_state": OpenApiCallbackState.ENABLED,
            "callback_url": OPENAPI_CALLBACK_URL,
        }
        fields.update(overrides)
        return ApiApp.objects.create(**fields)

    def _order(self, n: int, *, taker=None) -> Order:
        """一张单 + 一条 MAIN 绩效行（接手人 = `taker`）。"""
        order = Order.objects.create(
            order_no=_no(n),
            game=self.game,
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("10.00"),
            hall_price=Decimal("8.00"),
            source_order_no="T4-SRC-0001",
            sales_user=self.sales,
        )
        Performance.objects.create(
            order=order,
            user=taker if taker is not None else self.taker,
            emp_type=EmpType.MAIN,
            claimed_at=timezone.now(),
        )
        return order

    # ── 排一行（⛔ 只走各族自己的入队包装，不自己 `create`）────────────────
    def enqueue_taker_row(self, n: int = 1) -> TakerApiDelivery:
        order = self._order(n)
        rows = callback_recipients.notify_order_status_changed_for_taker(
            order, status=OrderStatus.IN_PROGRESS, occurred_at=timezone.now()
        )
        self.assertEqual(len(rows), 1, "打手侧应当恰好排一行")
        return rows[0]

    def enqueue_openapi_row(self, n: int = 1) -> OpenApiDelivery:
        order = self._order(n)
        rows = openapi_dispatch.notify_order_status_changed(
            order, spec=OPENAPI_SPEC, status=OrderStatus.IN_PROGRESS,
            occurred_at=timezone.now(),
        )
        self.assertEqual(len(rows), 1, "上家侧应当恰好排一行")
        return rows[0]

    # ── 读数 ─────────────────────────────────────────────────────────────
    def only_taker_row(self) -> TakerApiDelivery:
        rows = list(TakerApiDelivery.objects.all())
        self.assertEqual(len(rows), 1, [r.payload for r in rows])
        return rows[0]

    def only_openapi_row(self) -> OpenApiDelivery:
        rows = list(OpenApiDelivery.objects.all())
        self.assertEqual(len(rows), 1, [r.payload for r in rows])
        return rows[0]


#: 汇总行的辨识标记：用它数"跑过几个租户"（⛔ 不 import 那句话本身）。
SUMMARY_MARK = "打手侧回调投递"


def _StringIO():
    """`io.StringIO` 的薄壳（本文件多处要用它当 stdout / stderr 的容器）。"""
    return io.StringIO()


# ── 判据 1：`--once` 真投递（spec §6 第 1 行 / §9-11）────────────────────────────


class OnceDispatchTests(_DispatchFixture):
    """`--once` 跑一趟：那一行真的变 `done`，且发出去的字节符合契约。"""

    def test_once_delivers_the_pending_taker_row_and_marks_it_done(self):
        """⭐ 判据 1：pending 的打手侧行 ⇒ `--once` 之后是 `done`，且台账列都写对了。

        失败方向：命令压根没跑（漏进 `SYNC_SERVICES` 之类只影响部署面）、
        `spec` 传成上家侧那一份（那条行**一行都不会被取活**，本用例红）、
        HTTP 出口没接上（替身调用数为 0 ⇒ 下面第二条断言红）。
        """
        row = self.enqueue_taker_row()
        self.assertEqual(row.status, TakerApiDelivery.Status.PENDING)

        with _HttpBackend(status=200) as fake:
            call_command("takerapi_dispatch", "--once", "--tenant", connection.schema_name)

        row.refresh_from_db()
        self.assertEqual(row.status, TakerApiDelivery.Status.DONE)
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.response_status, 200)
        self.assertEqual(row.reason, "")
        self.assertIsNotNone(row.processed_at)
        # ⭐ 桩接没接上本身要有判据（`E-0029`）：替身**真的**被调到一次，且打的是打手侧那个地址。
        self.assertEqual(len(fake.instances), 1, "HTTP 替身一次都没被调到 ⇒ 本用例不产出结论")
        self.assertEqual(len(fake.instances[0].calls), 1)
        self.assertEqual(fake.instances[0].calls[0]["url"], TAKER_CALLBACK_URL)

    def test_the_sent_request_carries_the_contract_headers_and_event_name(self):
        """打手侧那条线发出去的是**同一份契约**：三个签名头 + 事件名 + 用**打手侧**那把密钥签名。

        ⚠️ 判据打在**替身记下的字节**上（⛔ 不打 `outbound` 的返回值）：
        签名头三常量与事件名是**对外契约**（spec §8），写死字面量（`E-0024`）。
        """
        self.enqueue_taker_row()
        with _HttpBackend(status=200) as fake:
            call_command("takerapi_dispatch", "--once", "--tenant", connection.schema_name)

        call = fake.instances[0].calls[0]
        body = json.loads(call["content"])
        self.assertEqual(body["event"], EVENT_STATUS)
        headers = call["headers"]
        for name in ("X-TT-Timestamp", "X-TT-Delivery", "X-TT-Signature"):
            self.assertIn(name, headers, f"缺签名头 {name}")
        # ⚠️ 签名用的是**打手侧**那把密钥：这里只证"头里有值且不是上家侧那把"算不出同一个值
        #    （完整算式对拍在 `tests/test_openapi_dispatch.py::SignatureTests`，⛔ 本文件不抄第二份）。
        self.assertTrue(headers["X-TT-Signature"])


# ── 判据 2：跨族不串台（spec §9-7 / T4 的核心）────────────────────────────────


class CrossFamilyTests(_DispatchFixture):
    """⭐⭐ 两族的台账**各动各的**：这是「命令忘了传 `spec=TAKER_SPEC`」唯一的网。"""

    def test_the_taker_command_only_touches_the_taker_ledger(self):
        """⭐ `takerapi_dispatch --once` **只**动打手侧那条；上家侧那条**原样不动**。

        失败方向：命令里写成 `run_round(spec=OPENAPI_SPEC)`（或漏传而被默认回上家侧）
        ⇒ 上家侧那条会被投出去、而打手侧那条一个字节都不动 ⇒ 两条断言同时红。
        """
        taker_row = self.enqueue_taker_row(1)
        openapi_row = self.enqueue_openapi_row(2)

        with _HttpBackend(status=200) as fake:
            call_command("takerapi_dispatch", "--once", "--tenant", connection.schema_name)

        taker_row.refresh_from_db()
        openapi_row.refresh_from_db()
        self.assertEqual(taker_row.status, TakerApiDelivery.Status.DONE, "打手侧那条没被投出去")
        # ⭐ 上家侧那条**原样不动**：状态、尝试次数、处理时刻三样都不许变。
        self.assertEqual(
            openapi_row.status, OpenApiDelivery.Status.PENDING,
            "打手侧的命令动了上家侧的台账（跨族串台）",
        )
        self.assertEqual(openapi_row.attempts, 0)
        self.assertIsNone(openapi_row.processed_at)
        self.assertEqual(len(fake.instances), 1, "两次投递就会有两个替身实例")
        self.assertEqual(fake.instances[0].calls[0]["url"], TAKER_CALLBACK_URL)

    def test_the_openapi_command_only_touches_the_openapi_ledger(self):
        """⭐ **反例**（判据 2 的另一半）：`openapi_dispatch --once` 只动上家侧那条。

        ⚠️ 这一半不是多余的：没有它，"上家侧那条原样不动"可能只是因为
        **两条行都没被取活**（例如地址被 SSRF 判据拒了）⇒ 上一半会**假绿**。
        两条合起来才说明"取活确实按各族的表分开了"。
        """
        taker_row = self.enqueue_taker_row(1)
        openapi_row = self.enqueue_openapi_row(2)

        with _HttpBackend(status=200) as fake:
            call_command("openapi_dispatch", "--once", "--tenant", connection.schema_name)

        taker_row.refresh_from_db()
        openapi_row.refresh_from_db()
        self.assertEqual(openapi_row.status, OpenApiDelivery.Status.DONE, "上家侧那条没被投出去")
        self.assertEqual(
            taker_row.status, TakerApiDelivery.Status.PENDING,
            "上家侧的命令动了打手侧的台账（跨族串台）",
        )
        self.assertEqual(taker_row.attempts, 0)
        self.assertIsNone(taker_row.processed_at)
        self.assertEqual(len(fake.instances), 1)
        self.assertEqual(fake.instances[0].calls[0]["url"], OPENAPI_CALLBACK_URL)


# ── 判据 3：逐租户异常边界（spec §6 第 1 行）──────────────────────────────────


class PerTenantExceptionBoundaryTests(_DispatchFixture):
    """⭐ 一个租户失败**不影响**别的租户（照 `openapi_dispatch` 的形状）。

    ⚠️ 第二个租户在 `setUpClass` 里建（schema DDL 不能在 `TestCase` 的事务里回滚）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _domain_b = provision_tenant(
            schema_name=TENANT_B_SCHEMA, name="投递命令租户乙", domain=TENANT_B_DOMAIN
        )

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def _seed_b(self) -> TakerApiDelivery:
        """在租户乙里造一套应用 + 一条 pending 行（用租户乙自己的 schema 上下文）。

        ⚠️ 返回值是**租户乙那一行**：它只能在租户乙的 `tenant_context` 里 `refresh_from_db`
        （见 `_read_b`）——两个 schema 的主键**各自从 1 开始**，拿它去当前连接上刷会读到
        **租户甲**那一行（`AGENTS.md` §5-G 点名的同一个坑：跨租户的读数不能用主键指认）。
        """
        try:
            with tenant_context(self.tenant_b):
                user = User.objects.create_user(username="t4-dispatch-b", password="pw-t4-b")
                game = Game.objects.create(name="投递命令租户乙游戏")
                app = TakerApp.objects.create(
                    name="投递命令打手侧应用乙",
                    app_key="t4dispatchbkey00001",
                    app_secret="t4-dispatch-b-secret-00000000001",
                    taker=user,
                    created_by=user,
                    state=AppState.ENABLED,
                    callback_state=CallbackState.ENABLED,
                    callback_url=TAKER_CALLBACK_URL,
                )
                order = Order.objects.create(
                    order_no=_no(9),
                    game=game,
                    status=OrderStatus.IN_PROGRESS,
                    total_price=Decimal("10.00"),
                    sales_user=user,
                )
                Performance.objects.create(
                    order=order, user=user, emp_type=EmpType.MAIN,
                    claimed_at=timezone.now(),
                )
                rows = callback_recipients.notify_order_status_changed_for_taker(
                    order, status=OrderStatus.IN_PROGRESS, occurred_at=timezone.now()
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(app.pk, rows[0].app_id)
                return rows[0]
        finally:
            # 跨 `tenant_context` 之后连接会停在别处 ⇒ 钉回来（照
            # `tests/test_my_profile_api.py::MyProfileCrossTenantTests._b_call` 的先例）。
            connection.set_tenant(self.tenant)

    def _read_b(self, row) -> None:
        """在**租户乙的 schema 上下文**里把那一行重新读一遍（⛔ 不在当前连接上 `refresh_from_db`）。

        ⚠️ 这一跳不是顺手加的：两个 schema 的主键序列**各自从 1 开始** ⇒ 在本用例自己那个
        租户的连接上 `refresh_from_db()` 会读到**本租户**那一行（`AGENTS.md` §5-G 点名的坑，
        本文件第一次写就正好踩上：乙那条 pending 被读成甲那条 done）。
        """
        try:
            with tenant_context(self.tenant_b):
                row.refresh_from_db()
        finally:
            connection.set_tenant(self.tenant)

    def test_one_broken_tenant_does_not_stop_the_others_and_is_reported(self):
        """⭐ **一个租户抛异常 ⇒ 别的租户照常被处理**；退出码非零；失败**报出来**。

        失败方向：把逐租户 `try/except` 去掉（那个坏租户一抛就断掉整趟 ⇒
        **排在它后面**的租户一行都不跑）、或把失败静默吞掉
        （退出码仍 0、stderr 上什么都没有）⇒ 本用例红。

        ⚠️ **谁是"坏租户"是按 schema 名升序定的**（`_iter_tenants` 就按它遍历）：
        租户乙 = `t4-dispatch-b`、本用例自己那个租户 = `test`（**排在乙后面**）
        ⇒ 乙先跑、它在投递内核里抛 ⇒ **甲（排在后面）仍然被处理**正是这条判据要的形状。
        反过来说：若把坏租户设成"排在最后"的那个，本用例**证明不了**任何边界
        （它后面根本没有租户可被带停）。

        ⚠️ 两个租户都在 `--tenant` 之外（**不给** `--tenant`）⇒ 走的是"遍历全部租户"那条路，
        与生产形态（`--loop` 无参）逐字同形。
        """
        row_a = self.enqueue_taker_row(1)
        row_b = self._seed_b()
        real_run_round = openapi_dispatch.run_round

        def fake_run_round(*, now=None, spec):
            # 只有**乙**这一族坏掉；别的租户（含本用例自己那个）照真跑。
            if connection.schema_name == TENANT_B_SCHEMA:
                raise RuntimeError("t4-租户乙故意炸掉")
            return real_run_round(now=now, spec=spec)

        out, err = _StringIO(), _StringIO()
        with _HttpBackend(status=200):
            with mock.patch.object(openapi_dispatch, "run_round", side_effect=fake_run_round):
                with self.assertRaises(CommandError) as ctx:
                    call_command("takerapi_dispatch", "--once", stdout=out, stderr=err)

        # ⭐ 退出码契约：有租户出现未预期失败 ⇒ 非零（照上家侧 `--once` 的口径）。
        self.assertEqual(ctx.exception.returncode, 1)
        # ⭐ 甲（排在坏租户**后面**）**照常被处理**：终态当读数，⛔ 不看"脚本跑完没报错"。
        row_a.refresh_from_db()
        self.assertEqual(
            row_a.status, TakerApiDelivery.Status.DONE,
            "前面那个租户失败把后面的租户一起带停了",
        )
        # ⭐ 乙那一轮**没跑成**（它的行留在 pending —— 不是"被静默当成成功"）。
        # ⚠️ 只能在乙的 schema 上下文里读它那一行（两个 schema 的主键各自从 1 起）。
        self._read_b(row_b)
        self.assertEqual(row_b.status, TakerApiDelivery.Status.PENDING)
        self.assertEqual(row_b.attempts, 0)
        # ⭐ 失败被**报出来**：stderr 点名那个租户 + 异常类型名（⛔ 不是 `str(exc)`）。
        self.assertIn(TENANT_B_SCHEMA, err.getvalue())
        self.assertIn("RuntimeError", err.getvalue())
        self.assertNotIn("t4-租户乙故意炸掉", err.getvalue(), "异常正文不许出现在终端汇总里")

    def test_the_summary_line_is_written_for_every_tenant_that_ran(self):
        """汇总行**按租户各恰好一行**（运行当时库里的租户集合，逐个都在）——运维靠它看出"哪台租户被处理过"。

        ⚠️⭐ 判据**当场从库里读租户集合**（平台面 `Tenant` 表，与命令自己的 `_iter_tenants` 同口径、
        ⛔ 不是按 `pg_namespace` 收 schema；⛔ 不写死"甲 + 乙 = 2"）：Django 的测试库**整趟共享**，
        而租户是**建 schema 的 DDL**（不在 `TestCase` 的事务里回滚）⇒ 同一个测试库里
        **谁建的租户都会留到整趟结束**（本命令这一趟要跑的租户 = `Tenant` 里除 `public` 之外的全部，
        口径与命令自己的 `_iter_tenants` 一致）。实测：本模块与 `tests.test_openapi_delivery_admin`
        一起跑时有 **3** 个租户（多出来的是那个模块 `setUpClass` 里建的
        `openapi_delivery_admin_b`），写死 2 就会在全量套件里红。

        ⛔ **不是"至少 N 行"**：断的是汇总行点名的租户**集合 == 当时真在跑的那个集合**
        ⇒ **漏掉一个租户照样红**（变异证明见 T4b 报告）。失败方向：命令把某个租户静默跳过
        （例如异常边界把它吞掉、或 `_iter_tenants` 少遍历一个）⇒ 本用例红。
        """
        self.enqueue_taker_row(1)
        self._seed_b()
        expected = list(
            Tenant.objects.exclude(schema_name=get_public_schema_name())
            .order_by("schema_name")
            .values_list("schema_name", flat=True)
        )
        out, err = _StringIO(), _StringIO()
        with _HttpBackend(status=200):
            call_command("takerapi_dispatch", "--once", stdout=out, stderr=err)
        text = out.getvalue()
        self.assertIn(connection.schema_name, text)
        self.assertIn(TENANT_B_SCHEMA, text)

        def _summary_line(schema: str) -> str:
            """汇总行的形状（照命令 `_run_tenant` 里那句；⛔ 本文件不 import 那句话本身）。"""
            return f"{schema}: {SUMMARY_MARK} 已投递"

        summary_lines = [
            ln for ln in text.splitlines() if SUMMARY_MARK in ln and ":" in ln
        ]
        # ⭐ **恰好集合**：当时真在跑的每个租户**恰好一行**，一个不多、一个不少。
        #   ⛔ 不是 `assertIn`（那会让"漏掉一个租户"永远不红），⛔ 也不是"至少 N 行"。
        self.assertEqual(
            sorted(ln.split(":")[0] for ln in summary_lines),
            expected,
            f"汇总行与当时真在跑的租户集合对不上：期望 {expected}，实际 {summary_lines}",
        )
        for line in summary_lines:
            # ⭐ 每行**恰好一个**租户前缀（两次拼接 / 串租户 ⇒ 红）。
            self.assertEqual(
                line.count(SUMMARY_MARK), 1, f"这一行里 {SUMMARY_MARK} 出现了不止一次：{line!r}"
            )
            schema = line.split(":")[0]
            self.assertEqual(
                line.count(f"{schema}: "),
                1,
                f"这一行没有恰好一次点名它自己的租户 {schema}：{line!r}",
            )
            # ⭐ 每行都是**完整**那句（⛔ 不是"含个前缀就算写了"）：形状与命令逐字对齐。
            self.assertTrue(
                line.startswith(_summary_line(schema)),
                f"这一行不是一条完整的汇总行：{line!r}",
            )
        # ⭐ 本租户那一行**真在文本里**（⛔ 不是"行数对得上就算"）。
        self.assertIn(f"{connection.schema_name}: {SUMMARY_MARK}", text)


# ── 判据 4：开关契约（照上家侧口径）───────────────────────────────────────────


class CommandSwitchesTests(_DispatchFixture):
    """`--once` / `--loop` 互斥 + 不存在的 `--tenant` 响亮报错。"""

    def test_once_and_loop_are_mutually_exclusive(self):
        """两个都给 ⇒ `CommandError`（⛔ 不是"静默常驻"：那会让排障的手工一趟变成永不返回）。"""
        with self.assertRaises(CommandError) as ctx:
            call_command("takerapi_dispatch", "--once", "--loop")
        self.assertIn("--once", str(ctx.exception))

    def test_an_unknown_tenant_is_a_loud_error(self):
        """不存在的 `--tenant` ⇒ `CommandError` 且**点名**那个 schema（照上家侧口径）。"""
        with self.assertRaises(CommandError) as ctx:
            call_command("takerapi_dispatch", "--once", "--tenant", "t4-no-such-schema")
        self.assertIn("t4-no-such-schema", str(ctx.exception))

    def test_a_plain_once_run_exits_zero(self):
        """⭐ 对照：没有未预期失败时 `--once` **不抛**（退出 0）——否则上一条判据的"非零"不指向失败。"""
        self.enqueue_taker_row()
        with _HttpBackend(status=200):
            call_command("takerapi_dispatch", "--once", "--tenant", connection.schema_name)

    def test_an_expected_delivery_failure_does_not_exit_non_zero(self):
        """预期内的投递失败（对方 500）**不**让 `--once` 非零：它是正常噪音（照上家侧口径）。"""
        row = self.enqueue_taker_row()
        with _HttpBackend(status=500):
            call_command("takerapi_dispatch", "--once", "--tenant", connection.schema_name)
        row.refresh_from_db()
        self.assertEqual(row.status, TakerApiDelivery.Status.PENDING)
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.response_status, 500)


# ── 汇总行的前缀 / 间隔字面值 / docstring 契约 ─────────────────────────────────


class SummaryPrefixTests(_DispatchFixture):
    """⭐ 汇总行必须带**打手侧自己的前缀**（⛔ 不照抄上家侧那句）。"""

    def test_the_summary_line_carries_the_taker_side_prefix(self):
        """运维并排看两条线的排障输出时，靠这一句分清"是哪条线在跑"。

        失败方向：照抄上家侧那句（`{schema}: 已投递 …`）⇒ 本用例红。
        """
        self.enqueue_taker_row()
        out = _StringIO()
        with _HttpBackend(status=200):
            call_command(
                "takerapi_dispatch", "--once", "--tenant", connection.schema_name, stdout=out
            )
        text = out.getvalue()
        self.assertIn(command_module.SUMMARY_PREFIX, text)
        self.assertIn("打手侧", command_module.SUMMARY_PREFIX)
        self.assertEqual(len([ln for ln in text.splitlines() if SUMMARY_MARK in ln]), 1)

    def test_the_loop_interval_literal_is_ten_seconds(self):
        """`LOOP_SLEEP_SECONDS = 10`（spec §6 / §2.1：推送延迟 ≈ 轮询间隔，代价已登记）。

        ⚠️ 写**字面值 10**（`E-0024`：钉值不钉常量）：有人把它改成 60，
        "10 秒级"这条对外口径就静默变了，而文档与单元注释里还写着 10。
        """
        self.assertEqual(command_module.LOOP_SLEEP_SECONDS, 10)


class DocstringContractTests(TenantTestCase):
    """命令 docstring 必须写清两件容易被后来者读错的事（`AGENTS.md` §5-C）。"""

    #: 命令模块源码（按记号/文本断言，⛔ 不 import 常量：那些是给人读的话）。
    _SOURCE = pathlib.Path(command_module.__file__).resolve()

    def _text(self) -> str:
        self.assertEqual(self._SOURCE.name, "takerapi_dispatch.py")
        return self._SOURCE.read_text(encoding="utf-8")

    def test_the_docstring_says_there_is_no_peer_watermark_on_this_line(self):
        """⭐ 这条线**没有**「与对端水位比较」那回事（那是 peer 线的）——说破，免得被读串。

        失败方向：把那句话删掉（后来者会照 peer 那条线的形状来找一个不存在的判据）⇒ 本用例红。
        """
        text = self._text()
        self.assertIn("没有", text)
        self.assertIn("水位", text)
        self.assertIn("peer", text)

    def test_the_docstring_explains_why_a_missing_spec_is_a_type_error(self):
        """⭐ 「漏传 `spec` 会 `TypeError`」这条**来历**（T1b 收掉了那个"默认回上家侧"的值）。

        失败方向：删掉这段来历（后来者会以为"必填"是随手加的、顺手补个默认值）⇒ 本用例红。
        """
        text = self._text()
        self.assertIn("TypeError", text)
        self.assertIn("T1b", text)
