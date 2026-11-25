"""多平台适配层（dl-F，2026-10-27）：按平台分派 + 未接入平台的中文错误。

## 这条线证明什么

1. `platform_adapters.get_adapter` 对**未实现平台**抛中文错误（`PlatformNotConnected`），
   且该异常仍是 `DailiantongPushError` 的子类 ⇒ 接口层既有失败出口直接接住（⛔ 不 500）；
2. 走到**真实调用点**时，未接入平台**零上游请求**：撤回 / 改价 / 补时 / 留言 / 预览
   全部断言「上游客户端一次都没构造」，推送行与审计一行都没变；
3. 代练通行为**逐字一致**：新增的 `?platform=` 入参默认 10，带不带它回的预览完全相同；
   四个动作端点的既有用例（`tests/test_dailiantong_actions.py`）一个字都没改就绿；
4. 同步命令**按行分派**：代练通行走真实现、另一个平台走**假适配器**；一个平台失败
   不影响另一个平台的行；未接入平台的行跳过并写可读原因（`qr_last_error` /
   `auto_reply_last_error`），⛔ 不算「发送失败」；
5. 推送列表**逐行**分派（假适配器）：具备能力的平台才下发 `qr_*` / `auto_reply_last_error`
   这三列，未接入平台回空值，**键集不变**。

## 不访问真实网络

所有上游都 patch 在**被查找的那个模块**上（`apps.business.dailiantong_push._open_client` /
`apps.business.dailiantong_actions._open_client`）。「零上游请求」的判据是
**这两个构造点一次都没被调用**，⛔ 不是「调用没抛异常」。

## 夹具纪律

凭据 / 单号 / 订单号一律**合成值**（不是任何真实数据）。
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.db import connection
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import dailiantong_push as push_module
from apps.business import platform_adapters as adapters
from apps.business.dailiantong_push import DailiantongPushError
from apps.business.models import (
    DailiantongPush,
    Game,
    GameServer,
    Order,
    OrderStatus,
    PlatformIntegration,
)
from apps.business.platform_adapters import (
    DailiantongAdapter,
    PlatformNotConnected,
    adapter_for_order,
    get_adapter,
    register_adapter,
    unregister_adapter,
)
from tests.p5_auth_helpers import make_logged_in_client

#: 平台 id：代练通 / 一个本版**未实现**的平台（代练妈妈，见 `PlatformIntegration.Platform`）。
DAILIANTONG = 10
UNSUPPORTED = 30

#: 与视图里的模块常量同值（测试里写死字面量，⛔ 不 import 视图常量）。
NODE = "order.list"

#: 合成订单号 / 上游单号（不是任何真实数据）。
ORDER_NO = "20261027000000000901"
SERIAL_DLT = "19000000000000000901"
SERIAL_OTHER = "19000000000000000902"


class FakeAdapter:
    """一个**假适配器**：记录调用，可按需失败；只实现本文件用到的能力。

    ⚠️ 它存在的意义是证明「调用点按行分派到**平台自己的适配器**」——
    ⛔ 不是再造一套代练通实现（真实现永远走 `DailiantongAdapter`）。
    """

    def __init__(
        self,
        platform_id: int = UNSUPPORTED,
        *,
        supports_accept_send: bool = False,
        supports_auto_reply: bool = False,
    ):
        self.platform_id = int(platform_id)
        self.platform_name = f"假平台{self.platform_id}"
        self.supports_accept_send = supports_accept_send
        self.supports_auto_reply = supports_auto_reply
        self.refreshed: list[int] = []
        self.qr_sent: list[int] = []
        self.auto_replied: list[int] = []
        #: 非空时 `refresh_push_status` 抛它（验证「一个平台失败不影响别的平台」）。
        self.fail_with: Exception | None = None

    def refresh_push_status(self, push, *, client=None):
        self.refreshed.append(push.pk)
        if self.fail_with is not None:
            raise self.fail_with
        return push

    def send_accept_qr(self, push):
        self.qr_sent.append(push.pk)
        return SimpleNamespace(sent=False, failed=False, status="fake", reason="")

    def dispatch_for_push(self, push):
        self.auto_replied.append(push.pk)
        return SimpleNamespace(sent=False, failed=False, status="fake", reason="")


class _RegisteredFakeMixin:
    """登记 / 还原一个假适配器（⛔ 不 patch 私有字典）。"""

    def register_fake(self, fake: FakeAdapter) -> FakeAdapter:
        register_adapter(fake)
        self.addCleanup(unregister_adapter, fake.platform_id)
        return fake


# ── ① 注册表本身：未实现平台 = 中文错误 ─────────────────────────────────


class AdapterRegistryTests(SimpleTestCase):
    """`get_adapter` 的契约（不碰数据库、不碰网络）。"""

    def test_dailiantong_returns_the_wrapping_adapter(self):
        adapter = get_adapter(DAILIANTONG)
        self.assertIsInstance(adapter, DailiantongAdapter)
        self.assertEqual(adapter.platform_id, DAILIANTONG)
        self.assertEqual(adapter.platform_name, "代练通")
        self.assertIs(adapter.supports_accept_send, True)
        self.assertIs(adapter.supports_auto_reply, True)

    def test_the_registry_returns_the_same_instance_every_time(self):
        self.assertIs(get_adapter(DAILIANTONG), get_adapter("10"))

    def test_unimplemented_platform_raises_the_chinese_error(self):
        with self.assertRaises(PlatformNotConnected) as ctx:
            get_adapter(UNSUPPORTED)
        message = str(ctx.exception)
        self.assertIn("该交易平台尚未接入", message)
        self.assertIn(str(UNSUPPORTED), message)
        # 接口层统一 catch `DailiantongPushError` ⇒ 未接入平台走同一条失败出口（⛔ 不 500）。
        self.assertIsInstance(ctx.exception, DailiantongPushError)

    def test_a_non_numeric_platform_id_is_a_chinese_error_not_a_value_error(self):
        with self.assertRaises(PlatformNotConnected) as ctx:
            get_adapter("abc")
        self.assertIn("尚未接入", str(ctx.exception))

    def test_adapter_or_none_swallows_only_the_unimplemented_case(self):
        self.assertIsNone(adapters.adapter_or_none(UNSUPPORTED))
        self.assertIsNotNone(adapters.adapter_or_none(DAILIANTONG))

    def test_a_fake_adapter_can_be_registered_and_removed(self):
        fake = FakeAdapter()
        register_adapter(fake)
        try:
            self.assertIs(get_adapter(UNSUPPORTED), fake)
        finally:
            unregister_adapter(UNSUPPORTED)
        with self.assertRaises(PlatformNotConnected):
            get_adapter(UNSUPPORTED)


# ── ② 未接入平台的真实调用点：中文错误 + 零上游请求 ──────────────────────


class _TenantFixture(TenantTestCase):
    """租户 A：游戏 / 区服 / 订单 / 启用且默认的代练通配置。

    ⚠️ 这里**故意**不预建推送行：各用例自己决定推送行的平台（这正是被测的东西）。
    """

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.game = Game.objects.create(name="王者荣耀")
        self.server = GameServer.objects.create(game=self.game, name="安卓QQ区")
        self.order = Order.objects.create(
            game=self.game,
            server=self.server,
            order_no=ORDER_NO,
            status=OrderStatus.WAITING_GRAB,
            project_text="星耀上王者",
            total_price=Decimal("120.00"),
        )
        self.integration = PlatformIntegration.objects.create(
            platform=DAILIANTONG,
            account="USR-SYNTH-ADAPTER-01",
            app_id="synth-adapter-app-01",
            app_secret="synth-adapter-secret-9a1c",
            pay_password_md5="0123456789abcdef0123456789abcdef",
            is_enabled=True,
            is_default=True,
            contact_phone="",
            contact_qq="",
        )

    def _api_client(self):
        # ⭐ M5：这几个动作是**写**端点（专属节点 `order.platform_write`），
        # 夹具同时给 `order.list`（同模块的读端点用）。
        return make_logged_in_client(
            username="platform-adapter-user", groups=("客服",),
            nodes=(NODE, "order.platform_write"),
        )

    def _push(self, *, platform: int, serial_no: str | None, state: str | None = None):
        return DailiantongPush.objects.create(
            order=self.order,
            platform=platform,
            state=state or DailiantongPush.State.SENT,
            serial_no=serial_no,
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=ORDER_NO,
            attempts=1,
        )


class UnsupportedPlatformActionTests(_TenantFixture):
    """单笔动作：推送行的平台未接入 ⇒ `code=1002` 中文 + 零上游 + 本地一行不动。"""

    def test_the_adapter_for_an_order_uses_the_push_row_platform(self):
        self._push(platform=UNSUPPORTED, serial_no=SERIAL_OTHER)
        with self.assertRaises(PlatformNotConnected):
            adapter_for_order(self.order)

    def test_no_push_row_falls_back_to_dailiantong(self):
        """没有推送行时落到默认平台（这样「还没抛过」的错误文案与改前逐字一致）。"""
        adapter = adapter_for_order(self.order)
        self.assertIsInstance(adapter, DailiantongAdapter)

    def test_every_platform_action_is_a_chinese_error_and_never_opens_a_client(self):
        self._push(platform=UNSUPPORTED, serial_no=SERIAL_OTHER)
        client = self._api_client()
        cases = (
            ("withdraw", f"/api/business/orders/{ORDER_NO}/platform-push/withdraw/", {}),
            ("price", f"/api/business/orders/{ORDER_NO}/platform-push/price/", {"price": "8.00"}),
            ("time", f"/api/business/orders/{ORDER_NO}/platform-push/time/", {"hours": 2}),
            ("message", f"/api/business/orders/{ORDER_NO}/platform-push/message/", {"msg": "在吗"}),
        )
        for label, url, payload in cases:
            with self.subTest(action=label):
                with mock.patch(
                    "apps.business.dailiantong_actions._open_client",
                    side_effect=AssertionError("未接入平台不该构造任何上游客户端"),
                ) as open_client:
                    response = client.post(url, payload)
                self.assertEqual(response.status_code, 200, response.content[:400])
                body = response.json()
                self.assertEqual(body["code"], 1002, body)
                self.assertIn("该交易平台尚未接入", body["msg"])
                open_client.assert_not_called()
        # 本地一行不动：状态 / 单号 / 错误列都没被改；也没写平台动作审计。
        row = DailiantongPush.objects.get(platform=UNSUPPORTED)
        self.assertEqual(row.state, DailiantongPush.State.SENT)
        self.assertEqual(row.serial_no, SERIAL_OTHER)
        self.assertEqual(row.last_error, "")
        self.assertIsNone(row.last_synced_at)
        self.assertEqual(
            OperationLog.objects.filter(action__startswith="order.dailiantong_").count(), 0
        )

    def test_preview_for_an_unimplemented_platform_is_a_chinese_error_with_zero_upstream(self):
        client = self._api_client()
        with mock.patch(
            "apps.business.dailiantong_push._open_client",
            side_effect=AssertionError("未接入平台不该构造任何上游客户端"),
        ) as open_client:
            response = client.get(
                f"/api/business/orders/{ORDER_NO}/platform-push/preview/?platform={UNSUPPORTED}"
            )
        self.assertEqual(response.status_code, 200, response.content[:400])
        body = response.json()
        self.assertEqual(body["code"], 1002, body)
        self.assertIn("该交易平台尚未接入", body["msg"])
        open_client.assert_not_called()


# ── ③ 代练通行为不变：新增的 ?platform= 参数默认 10 ──────────────────────


class DailiantongPreviewUnchangedTests(_TenantFixture):
    def test_preview_without_the_platform_param_equals_the_explicit_dailiantong_one(self):
        client = self._api_client()
        widget = SimpleNamespace(rows=())
        fake_client = SimpleNamespace(
            zones=lambda: widget,
            close=lambda: None,
        )
        with mock.patch.object(
            push_module, "_open_client", return_value=fake_client
        ):
            without = client.get(f"/api/business/orders/{ORDER_NO}/platform-push/preview/")
            with_param = client.get(
                f"/api/business/orders/{ORDER_NO}/platform-push/preview/?platform={DAILIANTONG}"
            )
        self.assertEqual(without.status_code, 200, without.content[:400])
        self.assertEqual(without.json()["code"], 0, without.content[:400])
        # 逐字一致：默认平台就是代练通，带不带这个新参数回的载荷完全相同。
        self.assertEqual(without.json()["data"], with_param.json()["data"])


# ── ④ 同步命令：按行分派（假适配器）+ 未接入平台跳过并写原因 ──────────────

#: 延迟 import：`_SyncTestBase` 会建租户 / 假上游 / 回调临时目录，属于重夹具。
from tests.test_dailiantong_sync import (  # noqa: E402 —— 见上一行的理由
    SERIAL_GOOD,
    _SyncTestBase,
)


class SyncDispatchesPerPlatformTests(_RegisteredFakeMixin, _SyncTestBase):
    """`dailiantong_sync` 的轮询段：每行按**自己的 platform** 取适配器。"""

    def _other_push(self, *, index: int = 2, serial: str = "19000000000000000911", **extra):
        return DailiantongPush.objects.create(
            order=self._order(index),
            platform=UNSUPPORTED,
            state=DailiantongPush.State.SENT,
            serial_no=serial,
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=f"20261026000000{index:04d}",
            attempts=1,
            **extra,
        )

    def test_a_mixed_push_list_dispatches_each_row_to_its_own_adapter(self):
        dailiantong = self._push(
            index=1, state=DailiantongPush.State.SENT, serial=SERIAL_GOOD
        )
        other = self._other_push()
        fake = self.register_fake(FakeAdapter())

        out, err = self._run_once()

        self.assertEqual(err, "")
        # 代练通那一行走真实现（假上游只被打了这一行）。
        dailiantong.refresh_from_db()
        self.assertEqual(dailiantong.remote_status, 12)
        self.assertEqual(self.fake.detail_calls_for(SERIAL_GOOD), 1)
        self.assertEqual(self.fake.detail_calls_for(other.serial_no), 0)
        # 另一个平台走**它自己的**适配器（假适配器收到的是它那条行）。
        self.assertEqual(fake.refreshed, [other.pk])
        other.refresh_from_db()
        self.assertIsNone(other.last_synced_at, "假适配器没写库，真实现也没碰它")
        self.assertIn("轮询 2", out)
        self.assertIn("失败 0", out)

    def test_one_platform_failing_does_not_stop_the_other_platforms_rows(self):
        dailiantong = self._push(
            index=1, state=DailiantongPush.State.SENT, serial=SERIAL_GOOD
        )
        other = self._other_push()
        fake = self.register_fake(FakeAdapter())
        fake.fail_with = RuntimeError("fake platform boom")

        out, _err = self._run_once()

        # 另一个平台那一行失败只计一次失败；代练通那一行照常回读成功。
        self.assertEqual(fake.refreshed, [other.pk])
        dailiantong.refresh_from_db()
        self.assertEqual(dailiantong.remote_status, 12)
        self.assertEqual(self.fake.detail_calls_for(SERIAL_GOOD), 1)
        self.assertIn("轮询 1", out)
        self.assertIn("失败 1", out)

    def test_an_unimplemented_platform_row_is_skipped_without_failing(self):
        self._push(index=1, state=DailiantongPush.State.SENT, serial=SERIAL_GOOD)
        other = self._other_push()

        out, _err = self._run_once()

        other.refresh_from_db()
        self.assertIsNone(other.last_synced_at)
        self.assertEqual(other.last_error, "")
        self.assertIn("轮询 1", out)
        self.assertIn("失败 0", out)
        self.assertIn("未接入平台跳过 1", out)

    def test_qr_section_skips_an_unimplemented_platform_and_writes_the_reason(self):
        other = self._other_push(remote_status=12, remote_cancel_status=10)

        out, _err = self._run_once()

        other.refresh_from_db()
        self.assertIn("该交易平台尚未接入", other.qr_last_error)
        self.assertIn("接手后发送", other.qr_last_error)
        self.assertIsNone(other.qr_sent_at)
        self.assertEqual(self.fake.calls, [], "未接入平台的行不该打上游")
        self.assertIn("轮询 0", out)

    def test_auto_reply_section_skips_an_unimplemented_platform_and_writes_the_reason(self):
        other = self._other_push(remote_status=13, remote_cancel_status=10)

        out, _err = self._run_once()

        other.refresh_from_db()
        self.assertIn("该交易平台尚未接入", other.auto_reply_last_error)
        self.assertIn("自动回复", other.auto_reply_last_error)
        self.assertEqual(self.fake.calls, [], "未接入平台的行不该打上游")
        self.assertIn("轮询 0", out)


# ── ⑤ 推送列表逐行分派（假适配器）+ 键集不变 ─────────────────────────────


class PushListPerRowDispatchTests(_RegisteredFakeMixin, _TenantFixture):
    """`GET platform-pushes` 的行序列化按行取适配器：没能力的平台不下发能力列。"""

    def test_mixed_rows_dispatch_per_platform_without_changing_the_key_set(self):
        from apps.business.api.platform_push_views import PlatformPushRowSerializer

        dailiantong = self._push(platform=DAILIANTONG, serial_no=SERIAL_DLT)
        other = self._push(platform=UNSUPPORTED, serial_no=SERIAL_OTHER)
        for push in (dailiantong, other):
            push.qr_last_error = "历史原因"
            push.auto_reply_last_error = "历史原因"
            push.save(update_fields=["qr_last_error", "auto_reply_last_error", "updated_at"])

        # ① 假适配器「具备接手后发送、没有自动回复」⇒ 该行的 qr 列留下、auto_reply 置空。
        self.register_fake(
            FakeAdapter(supports_accept_send=True, supports_auto_reply=False)
        )
        rows = PlatformPushRowSerializer([dailiantong, other], many=True).data
        by_platform = {row["platform"]: row for row in rows}
        self.assertEqual(set(by_platform[DAILIANTONG]), set(by_platform[UNSUPPORTED]))
        self.assertEqual(by_platform[DAILIANTONG]["qr_last_error"], "历史原因")
        self.assertEqual(by_platform[DAILIANTONG]["auto_reply_last_error"], "历史原因")
        self.assertEqual(by_platform[UNSUPPORTED]["qr_last_error"], "历史原因")
        self.assertEqual(by_platform[UNSUPPORTED]["auto_reply_last_error"], "")

        # ② 没有适配器（未接入平台）⇒ 两组能力列都回空值（⛔ 不留别家的历史失败原因）。
        unregister_adapter(UNSUPPORTED)
        rows = PlatformPushRowSerializer([dailiantong, other], many=True).data
        by_platform = {row["platform"]: row for row in rows}
        self.assertIsNone(by_platform[UNSUPPORTED]["qr_sent_at"])
        self.assertEqual(by_platform[UNSUPPORTED]["qr_last_error"], "")
        self.assertEqual(by_platform[UNSUPPORTED]["auto_reply_last_error"], "")
        # 代练通那一行不受影响。
        self.assertEqual(by_platform[DAILIANTONG]["qr_last_error"], "历史原因")

    def test_the_list_endpoint_still_lists_unimplemented_platform_rows(self):
        """⚠️ 列表是**本地读面**：未接入平台的行也是本租户的真实数据，照常列出来。"""
        client = self._api_client()
        self._push(platform=UNSUPPORTED, serial_no=SERIAL_OTHER)
        response = client.get(f"/api/business/platform-pushes/?platform={UNSUPPORTED}")
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["total"], 1, body)
        self.assertEqual(body["data"]["items"][0]["platform"], UNSUPPORTED)
