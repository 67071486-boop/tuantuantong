"""`dailiantong_sync`：状态同步命令 + 回调接收器。

## 覆盖什么

| 组 | 判据 |
|---|---|
| 轮询 | 只处理 `sent` / `unknown`；`failed` / `pending` 一行都不碰 |
| 单条失败 | 一条回读失败不中断整轮，其它行照常回读 |
| 回调消费 | 回调里的 `Status` **不得**直接落库；必须再调 `LevelOrderDetail` 回读（stub 断言） |
| 接收器 | 正确签名 ⇒ 落盘 + 200；错签名 ⇒ 403 且不落盘；密钥缺失 ⇒ 503 且不落盘 |
| 命令外壳 | `--once` / `--loop` 互斥、未知租户响亮报错、`--interval` 校验 |

## 不访问真实网络

上游一律用 `httpx.MockTransport` 收进 `DailiantongClient`（服务层的唯一构造点是
`dailiantong_push._open_client`，patch 的正是它——⛔ 不是 patch 定义 `DailiantongClient`
的那个模块，见 `AGENTS.md` §5-E）。回调接收器用 WSGI 直接调用，不起真端口。

## 夹具纪律

一切凭据 / 单号 / 回调密钥都是**合成值**（不是任何真实 AppSecret / 订单号）；
`src/tests/test_*` 有真值守卫扫 `src/**`。
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import io
import json
import os
import shutil
import tempfile
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qsl, urlencode

import httpx
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import dailiantong_push as dailiantong_push_module
from apps.business.management.commands import dailiantong_sync as sync_module
from apps.business.dailiantong_client import DailiantongClient, DailiantongConfig
from apps.business.models import (
    DailiantongPush,
    EmpType,
    Game,
    GameServer,
    Order,
    OrderScreenshot,
    OrderStatus,
    Performance,
    PlatformIntegration,
)

#: 合成凭据（⛔ 不是任何真实密钥）。
APP_ID = "synth-dlt-sync-app-0001"
APP_SECRET = "synth-dlt-sync-secret-9f3a"
PAY_PASSWORD = "synth-sync-pay-pw-71c"
PAY_PASSWORD_MD5 = hashlib.md5(PAY_PASSWORD.encode("utf-8")).hexdigest()

#: 合成上游单号（真实是 20 位纯数字；这里用不会撞真实单的合成串）。
SERIAL_SENT = "19000000000000000001"
SERIAL_UNKNOWN = "19000000000000000002"
SERIAL_PENDING = "19000000000000000003"
SERIAL_BAD = "19000000000000000004"
SERIAL_GOOD = "19000000000000000005"
SERIAL_CALLBACK = "19000000000000000006"

#: 一次**暂时性**的上游业务失败（⛔ 刻意**不**用「没有找到该订单」：那句从 P1 起是
#: 「远端订单已被删除 ⇒ 收成 closed」的证据，拿它当普通失败会让失败隔离 / 退避那几条
#: 用例测的是另一件事）。
UPSTREAM_BUSINESS_ERR = "处理数据异常，请稍后重试"
#: P1 的「这张单没了」原文（实测 `-1 没有找到该订单！`）。
ABSENT_BUSINESS_ERR = "没有找到该订单！"

#: 回调接收器的合成密钥（不是真实 AppSecret）。
CALLBACK_SECRET = "synth-callback-secret-7d1e"

#: 载入仓库里的正式接收器（在 `deploy/` 下，不是 Django app）——直接调 WSGI 入口。
_RECEIVER_PATH = Path(__file__).resolve().parents[2] / "deploy" / "callback" / "dailiantong_receiver.py"
_spec = importlib.util.spec_from_file_location("tests._dailiantong_receiver_under_test", _RECEIVER_PATH)
assert _spec is not None and _spec.loader is not None, f"载入不了接收器：{_RECEIVER_PATH}"
receiver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(receiver)


class _FakeUpstream:
    """假上游：只实现 `LevelOrderDetail`，记录每次调用。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.details: dict[str, dict] = {}
        self.failing_serials: set[str] = set()
        self.failure_message = UPSTREAM_BUSINESS_ERR
        #: P1：回读报「这张单不存在」的单号（远端已被删除）。
        self.absent_serials: set[str] = set()
        #: A4 的「内部打手先接 ⇒ 删掉代练通未接手单」：删除成功的单号（之后详情查不到）。
        self.deleted: set[str] = set()
        #: `LevelOrderDelSelf` 的上游回包（默认成功）。
        self.delete_response: dict = {"Result": 1, "Err": "删除成功."}
        #: 进度图行（按单号；默认空列表 ⇒ 进度图同步零新增、零下载）。
        self.progress_rows: dict[str, list[dict]] = {}

    @property
    def actions(self) -> list[str]:
        return [action for action, _form in self.calls]

    def forms_for(self, action: str) -> list[dict]:
        return [form for called, form in self.calls if called == action]

    def detail_calls_for(self, serial: str) -> int:
        return sum(
            1 for action, form in self.calls
            if action == "LevelOrderDetail" and form.get("ODSerialNo") == serial
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode("utf-8")))
        action = form.get("Action", "")
        self.calls.append((action, form))
        if action == "LevelOrderDetail":
            serial = form.get("ODSerialNo", "")
            if serial in self.absent_serials:
                return httpx.Response(200, json={"Result": -1, "Err": ABSENT_BUSINESS_ERR})
            if serial in self.failing_serials:
                return httpx.Response(200, json={"Result": -1, "Err": self.failure_message})
            if serial in self.deleted:
                # 删除后被回读：真实上游回 `-1 没有找到该订单！`。
                return httpx.Response(200, json={"Result": -1, "Err": "没有找到该订单！"})
            detail = self.details.get(serial, {"Status": 12, "CancelStatus": 10})
            return httpx.Response(200, json=detail)
        if action == "LevelOrderProgressList":
            serial = form.get("ODSerialNo", "")
            rows = self.progress_rows.get(serial, [])
            return httpx.Response(
                200,
                json={
                    "LevelOrderProgressList": rows,
                    "RecordCount": len(rows),
                },
            )
        if action == "LevelOrderDelSelf":
            serial = form.get("ODSerialNo", "")
            if self.delete_response.get("Result") == 1:
                self.deleted.add(serial)
            return httpx.Response(200, json=self.delete_response)
        return httpx.Response(200, json={"Result": -101, "Err": "未知动作"})


class _SyncTestBase(TenantTestCase):
    """租户 A + 一个启用且默认的代练通配置；回调文件指到临时目录。"""

    def setUp(self):
        connection.set_tenant(self.tenant)
        get_user_model().objects.create_user(
            username="dlt-sync-operator", password="synth-pw-0001", first_name="同步员"
        )
        self.game = Game.objects.create(name="王者荣耀")
        self.server = GameServer.objects.create(game=self.game, name="安卓QQ区")
        self.integration = PlatformIntegration.objects.create(
            platform=10,
            account="USR-SYNTH-SYNC-01",
            app_id=APP_ID,
            app_secret=APP_SECRET,
            pay_password_md5=PAY_PASSWORD_MD5,
            is_enabled=True,
            is_default=True,
            contact_phone="",
            contact_qq="",
        )
        self.fake = _FakeUpstream()

        self.tmpdir = tempfile.mkdtemp(prefix="dlt-sync-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        env_patcher = mock.patch.dict(
            os.environ, {"DAILIANTONG_CALLBACK_ROOT": self.tmpdir}, clear=False
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        self.events_path = Path(self.tmpdir) / "shared" / "dailiantong-callback.jsonl"

    # ── 夹具 ────────────────────────────────────────────────────────
    def _order(self, index: int = 1) -> Order:
        return Order.objects.create(
            game=self.game,
            server=self.server,
            order_no=f"20261026000000{index:04d}",
            status=OrderStatus.WAITING_GRAB,
            project_text="星耀上王者",
            total_price=Decimal("120.00"),
        )

    def _push(self, *, index: int, state: str, serial: str | None):
        return DailiantongPush.objects.create(
            order=self._order(index),
            platform=10,
            state=state,
            serial_no=serial,
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=f"20261026000000{index:04d}",
            attempts=1,
        )

    # ── 上游打桩（⛔ 不碰真实网络）──────────────────────────────────
    def _client_factory(self, integration):
        return DailiantongClient(
            DailiantongConfig(
                app_id=integration.app_id,
                app_secret=integration.app_secret,
                user_id=integration.account,
                pay_password_md5=integration.pay_password_md5,
            ),
            transport=httpx.MockTransport(self.fake.handler),
            clock=lambda: 1_790_000_000,
        )

    def _run_once(self, *args) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch(
            "apps.business.dailiantong_push._open_client", side_effect=self._client_factory
        ):
            call_command("dailiantong_sync", "--once", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    # ── 回调事件文件 ────────────────────────────────────────────────
    def _write_callback_event(self, body: str, *, newline: bool = True) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": "2026-10-26T15:10:06+00:00",
            "peer": "127.0.0.1",
            "real_ip": "120.26.194.70",
            "body": body,
            "verified": True,
        }
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + ("\n" if newline else ""))


class PollingTests(_SyncTestBase):
    def test_progress_screenshots_are_imported_after_polling(self):
        """⭐ 2026-10-28：本轮回读之后，把打手进度图同步进本地「订单截图」。"""
        serial = "19000000000000000777"
        self._push(index=1, state=DailiantongPush.State.SENT, serial=serial)
        self.fake.progress_rows[serial] = [
            {
                "ID": 367274039,
                "CreateDate": "2026-10-28 11:38:12",
                "Img": "https://img004.dailiantong.com/@/Progress/abc.jpg",
                "Comment": "12*****23",
                "Source": 10,
                "NickName": "接单者",
            }
        ]
        media = tempfile.mkdtemp(prefix="dlt-sync-media-")
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        jpeg = b"\xff\xd8\xff\xe0" + b"synth" * 20
        with override_settings(MEDIA_ROOT=media), mock.patch(
            "apps.business.dailiantong_progress_sync._fetch_image", return_value=jpeg
        ):
            out, _err = self._run_once()

        self.assertEqual(OrderScreenshot.objects.count(), 1)
        self.assertEqual(
            OrderScreenshot.objects.get().img_type, OrderScreenshot.IMG_ORDER
        )
        self.assertIn("进度图 1", out)

    def test_once_refreshes_only_sent_and_unknown_rows(self):
        sent = self._push(index=1, state=DailiantongPush.State.SENT, serial=SERIAL_SENT)
        unknown = self._push(
            index=2, state=DailiantongPush.State.UNKNOWN, serial=SERIAL_UNKNOWN
        )
        failed = self._push(index=3, state=DailiantongPush.State.FAILED, serial=None)
        pending = self._push(
            index=4, state=DailiantongPush.State.PENDING, serial=SERIAL_PENDING
        )

        out, err = self._run_once()

        self.assertEqual(err, "")
        self.assertEqual(
            [a for a in self.fake.actions if a != "LevelOrderProgressList"],
            ["LevelOrderDetail", "LevelOrderDetail"],
            "只该回读 sent / unknown 两行；failed / pending 一行都不该打上游"
            "（进度图同步另算：只对有效推送各拉一次空列表）",
        )
        self.assertEqual(
            {form.get("ODSerialNo") for action, form in self.fake.calls if action == "LevelOrderDetail"},
            {SERIAL_SENT, SERIAL_UNKNOWN},
        )

        for push in (sent, unknown):
            push.refresh_from_db()
            self.assertEqual(push.remote_status, 12)
            self.assertEqual(push.remote_cancel_status, 10)
            self.assertIsNotNone(push.last_synced_at)
        failed.refresh_from_db()
        self.assertIsNone(failed.remote_status, "failed 行的状态不该被同步命令碰")
        self.assertIsNone(failed.last_synced_at)
        pending.refresh_from_db()
        self.assertIsNone(pending.remote_status, "pending 行还没发出，不该回读")
        self.assertIsNone(pending.last_synced_at)

        self.assertIn("轮询 2", out)
        self.assertIn("回调 0", out)
        self.assertIn("失败 0", out)

    def test_unknown_row_without_serial_is_skipped_not_retried_forever(self):
        """`unknown` 行允许没有上游单号（超时要人工核对）⇒ 跳过并汇总，不每轮刷失败。"""
        row = self._push(index=5, state=DailiantongPush.State.UNKNOWN, serial=None)

        out, _err = self._run_once()

        self.assertEqual(self.fake.calls, [])
        self.assertIn("轮询 0", out)
        self.assertIn("失败 0", out)
        self.assertIn("无单号 1", out)
        row.refresh_from_db()
        self.assertIsNone(row.last_synced_at)


    def test_terminal_rows_stop_being_polled(self):
        """远端到终态后 `is_active=False` ⇒ 下一轮不再打上游（否则每条历史单每 30 秒白打一次）。"""
        settled = self._push(
            index=6, state=DailiantongPush.State.SENT, serial="19000000000000000007"
        )
        force_cancelled = self._push(
            index=7, state=DailiantongPush.State.SENT, serial="19000000000000000008"
        )
        self.fake.details["19000000000000000007"] = {"Status": 17, "CancelStatus": 10}
        self.fake.details["19000000000000000008"] = {"Status": 12, "CancelStatus": 16}

        first_out, _ = self._run_once()

        self.assertIn("轮询 2", first_out)
        settled.refresh_from_db()
        force_cancelled.refresh_from_db()
        self.assertFalse(settled.is_active, "Status=17（已结算）应停止轮询")
        self.assertFalse(force_cancelled.is_active, "CancelStatus=16（客服强制撤销）应停止轮询")
        self.assertEqual(settled.remote_status, 17)
        self.assertEqual(force_cancelled.remote_cancel_status, 16)

        calls_after_first = len(self.fake.calls)
        second_out, _ = self._run_once()

        self.assertEqual(len(self.fake.calls), calls_after_first, "终态行不该在下一轮再被轮询")
        self.assertIn("轮询 0", second_out)
        self.assertIn("失败 0", second_out)

    def test_negotiated_and_customer_handled_cancels_stop_being_polled(self):
        """⭐ 真机口径：`CancelStatus=12` / `15` 也是终态。

        现场：远端 `Status=16`（撤销中）+ `CancelStatus=12`（协商已处理 = 打手已同意撤销），
        本地订单已是「已退单」，却仍在每 30 秒轮询、`remote_status` 停在 16
        ⇒ 页面「交易状态」一直显示「撤销中」。

        判据：① 这两档一轮之后 `is_active=False`、本地落已退单；② 下一轮**零上游调用**。
        """
        negotiated = self._push(
            index=8, state=DailiantongPush.State.SENT, serial="19000000000000000009"
        )
        handled = self._push(
            index=9, state=DailiantongPush.State.SENT, serial="19000000000000000010"
        )
        self.fake.details["19000000000000000009"] = {"Status": 16, "CancelStatus": 12}
        self.fake.details["19000000000000000010"] = {"Status": 13, "CancelStatus": 15}

        first_out, _ = self._run_once()

        self.assertIn("轮询 2", first_out)
        for push in (negotiated, handled):
            push.refresh_from_db()
            self.assertFalse(
                push.is_active, "撤销已完成（CancelStatus=12/15）就该停轮询"
            )
            # 这两条推送没有经历过「被接手」（没有占位接手人）⇒ 停在「已退单」。
            self.assertEqual(
                Order.objects.get(pk=push.order_id).status, OrderStatus.REFUNDED
            )

        calls_after_first = len(self.fake.calls)
        second_out, _ = self._run_once()

        self.assertEqual(
            len(self.fake.calls), calls_after_first, "撤销完成的单不该在下一轮再被轮询"
        )
        self.assertIn("轮询 0", second_out)
        self.assertIn("失败 0", second_out)


class AutoArbitrationSectionTests(_SyncTestBase):
    """⭐ 2026-10-28：自动申请客服介入**并进每轮同步**（⛔ 不另起 systemd 单元）。"""

    def test_every_round_calls_the_auto_arbitration_section(self):
        with mock.patch.object(
            sync_module, "auto_arbitrate_pending_pushes", return_value=(2, 1)
        ) as patched:
            out, err = self._run_once()

        self.assertEqual(err, "")
        # ⚠️ 租户数取决于本进程里有没有别的用例 provision 过租户 B
        #    （`GuardSectionExitCodeTests` 已登记过这个测试隔离特性）⇒ 只断言「至少跑了一次」。
        self.assertGreaterEqual(patched.call_count, 1, "每轮必须调用自动仲裁段")
        self.assertIn("自动仲裁 2", out)
        self.assertIn("自动仲裁失败 1", out)

    def test_a_section_failure_is_isolated_from_other_sections(self):
        """仲裁段整段炸了 ⇒ 只计「段级意外」，⛔ 不拖垮群码 / 自动回复（`_guard_section`）。"""
        with mock.patch.object(
            sync_module,
            "auto_arbitrate_pending_pushes",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(CommandError) as ctx:
                self._run_once()
        self.assertEqual(ctx.exception.returncode, 1, "--once 非零 = 我们这边坏了")


class FailureIsolationTests(_SyncTestBase):
    def test_one_failed_read_does_not_stop_the_other_rows(self):
        bad = self._push(index=1, state=DailiantongPush.State.SENT, serial=SERIAL_BAD)
        good = self._push(index=2, state=DailiantongPush.State.SENT, serial=SERIAL_GOOD)
        self.fake.failing_serials.add(SERIAL_BAD)

        out, _err = self._run_once()

        # 两行都被尝试过（顺序按 id：bad 先、good 后）。
        self.assertEqual(self.fake.detail_calls_for(SERIAL_BAD), 1)
        self.assertEqual(self.fake.detail_calls_for(SERIAL_GOOD), 1)
        bad.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(bad.last_error, UPSTREAM_BUSINESS_ERR)
        self.assertEqual(good.remote_status, 12)
        self.assertIn("轮询 1", out)
        self.assertIn("失败 1", out)


class AbsentRemoteTests(_SyncTestBase):
    """⭐ 回读报「已被删除 / 没有找到 / 不存在 / 查不到」= 远端终态。

    旧行为把它当**读取失败**：重试到上限后停轮询，推送行仍停在 `sent`（页面显示
    「已发送 / 未接手」说反话），而且这张单**再也抛不出去**（`sent` 占着部分唯一索引）。
    """

    def test_absent_readback_closes_the_row_and_stops_polling(self):
        row = self._push(
            index=1, state=DailiantongPush.State.SENT, serial=SERIAL_BAD
        )
        self.fake.absent_serials.add(SERIAL_BAD)

        out, err = self._run_once()

        self.assertEqual(err, "")
        row.refresh_from_db()
        self.assertEqual(row.state, DailiantongPush.State.CLOSED)
        self.assertFalse(row.is_active, "远端已不存在 ⇒ 不该再轮询")
        self.assertIn("没有找到", row.remote_note, "remote_note 要写清可读原因")
        self.assertEqual(row.last_error, "", "删单不是故障：last_error 必须清空")
        self.assertIn("轮询 1", out)
        self.assertIn("失败 0", out, "远端终态不算失败（旧实现会把它计成失败）")

    def test_ambiguous_acceptance_or_deletion_does_not_close_the_push(self):
        row = self._push(index=1, state=DailiantongPush.State.SENT, serial=SERIAL_BAD)
        self.fake.failing_serials.add(SERIAL_BAD)
        self.fake.failure_message = "订单已被他人接手或已删除"

        out, _ = self._run_once()

        row.refresh_from_db()
        self.assertEqual(row.state, DailiantongPush.State.SENT)
        self.assertTrue(row.is_active)
        self.assertEqual(row.last_error, "订单已被他人接手或已删除")
        self.assertIn("失败 1", out)

    def test_the_next_round_does_not_hit_upstream_again(self):
        row = self._push(
            index=1, state=DailiantongPush.State.SENT, serial=SERIAL_BAD
        )
        self.fake.absent_serials.add(SERIAL_BAD)
        self._run_once()
        before = len(self.fake.calls)

        second_out, _ = self._run_once()

        self.assertEqual(len(self.fake.calls), before, "closed 行不该再回读")
        self.assertIn("轮询 0", second_out)
        row.refresh_from_db()
        self.assertEqual(row.state, DailiantongPush.State.CLOSED)

    def test_a_closed_row_no_longer_blocks_a_new_push(self):
        """closed 之后该单可以**重新抛**（不再占有效推送的部分唯一索引）。"""
        row = self._push(
            index=1, state=DailiantongPush.State.SENT, serial=SERIAL_BAD
        )
        self.fake.absent_serials.add(SERIAL_BAD)
        self._run_once()
        row.refresh_from_db()
        self.assertNotIn(row.state, DailiantongPush.ACTIVE_STATES)
        self.assertFalse(
            DailiantongPush.objects.filter(
                order_id=row.order_id, state__in=DailiantongPush.ACTIVE_STATES
            ).exists()
        )


class CallbackQueueTests(_SyncTestBase):
    def _callback_body(self, *, serial: str, status: str = "99", cancel_status: str = "98") -> str:
        """构造一条**声称**状态是 99 的回调（与详情里的 12 故意不同）。

        `Sign` 由接收器负责验，消费侧不看它；这里随便给一个值，确保命令**不**读 Status。
        """
        return urlencode(
            [
                ("SerialNo", serial),
                ("Status", status),
                ("StatusName", "回调里声称的状态"),
                ("CancelStatus", cancel_status),
                ("ExOrderNo", "202610260000000001"),
                ("Sign", "ignored-by-consumer"),
            ]
        )

    def test_callback_is_only_an_alarm_and_the_status_comes_from_a_reread(self):
        push = self._push(
            index=1, state=DailiantongPush.State.SENT, serial=SERIAL_CALLBACK
        )
        self._write_callback_event(self._callback_body(serial=SERIAL_CALLBACK))

        out, _err = self._run_once()

        # 判据一：确实回读了详情（stub 断言 `order_detail` 被调用）。
        self.assertEqual(self.fake.detail_calls_for(SERIAL_CALLBACK), 1)
        # 判据二：落库的是详情里的 12/10，⛔ 不是回调里的 99/98。
        push.refresh_from_db()
        self.assertEqual(push.remote_status, 12)
        self.assertEqual(push.remote_cancel_status, 10)
        self.assertNotEqual(push.remote_status, 99)
        # 回调已经回读过 ⇒ 同一轮轮询不再重复打上游。
        self.assertEqual(
            [a for a in self.fake.actions if a != "LevelOrderProgressList"],
            ["LevelOrderDetail"],
        )
        self.assertIn("轮询 0", out)
        self.assertIn("回调 1", out)

    def test_consumed_events_are_not_replayed_on_the_next_round(self):
        self._push(index=1, state=DailiantongPush.State.SENT, serial=SERIAL_CALLBACK)
        self._write_callback_event(self._callback_body(serial=SERIAL_CALLBACK))

        first_out, _ = self._run_once()
        second_out, _ = self._run_once()

        self.assertIn("回调 1", first_out)
        self.assertIn("回调 0", second_out)
        # ⚠️ 第二轮仍会**轮询**这一行（它是 sent，轮询本来就是每轮都读）——所以
        # `LevelOrderDetail` 总共 2 次是正常的；判「回调没重放」看的是 `回调` 计数。
        self.assertIn("轮询 1", second_out)
        self.assertEqual(
            self.fake.detail_calls_for(SERIAL_CALLBACK), 2,
            "第二轮只该是轮询再读一次，⛔ 不是把同一条回调又消费一遍",
        )

    def test_callback_forces_one_read_even_after_the_row_stopped_polling(self):
        """`is_active=False` 只关掉**轮询**；回调事件仍要强制回读一次（终态以回读为准）。"""
        push = self._push(
            index=9, state=DailiantongPush.State.SENT, serial="19000000000000000009"
        )
        DailiantongPush.objects.filter(pk=push.pk).update(is_active=False)
        self._write_callback_event(
            self._callback_body(serial="19000000000000000009")
        )

        out, _ = self._run_once()

        self.assertIn("回调 1", out)
        self.assertIn("轮询 0", out)
        self.assertEqual(self.fake.detail_calls_for("19000000000000000009"), 1)
        push.refresh_from_db()
        self.assertEqual(push.remote_status, 12)

    def test_a_half_written_last_line_is_left_for_the_next_round(self):
        """接收器正在追加时可能读到没有换行的半行 ⇒ 本轮不消费、不推进偏移。"""
        push = self._push(
            index=1, state=DailiantongPush.State.SENT, serial=SERIAL_CALLBACK
        )
        self._write_callback_event(
            self._callback_body(serial=SERIAL_CALLBACK), newline=False
        )

        out, _ = self._run_once()

        # 半行没被消费；但这一行是 sent ⇒ **轮询**照常回读一次（两件事互不影响）。
        self.assertIn("回调 0", out)
        self.assertIn("轮询 1", out)
        from apps.business.management.commands import dailiantong_sync as module

        offset_file = module.callback_offset_path(connection.schema_name)
        self.assertFalse(offset_file.exists(), "半行不消费时⛔ 不许写偏移")

        # 接收器补上换行 ⇒ 下一轮才消费（回调回读过一次；轮询因去重不再重复）。
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        out, _ = self._run_once()
        self.assertIn("回调 1", out)
        self.assertIn("轮询 0", out)
        self.assertEqual(self.fake.detail_calls_for(SERIAL_CALLBACK), 2)
        push.refresh_from_db()
        self.assertEqual(push.remote_status, 12)


class PendingRecoveryTests(_SyncTestBase):
    """⭐ 审计修复（2026-10-27，P1-4）：pending 超时回收成 unknown（不再永久卡死）。"""

    def _pending(self, *, age_minutes: int, serial=None):
        push = self._push(index=11, state=DailiantongPush.State.PENDING, serial=serial)
        DailiantongPush.objects.filter(pk=push.pk).update(
            updated_at=timezone.now() - timedelta(minutes=age_minutes)
        )
        return push

    def test_stale_pending_becomes_unknown_with_a_readable_reason(self):
        push = self._pending(age_minutes=6, serial=None)

        out, _err = self._run_once()

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.UNKNOWN)
        self.assertIn("pending 状态超过", push.last_error)
        self.assertIn("人工", push.last_error)
        self.assertIn("pending 回收 1", out)
        # 没有上游单号 ⇒ 本轮机不再打上游（下一轮也只会跳过）。
        self.assertEqual(self.fake.calls, [])

    def test_fresh_pending_is_left_alone(self):
        push = self._pending(age_minutes=1, serial=None)

        out, _err = self._run_once()

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.PENDING)
        self.assertNotIn("pending 回收", out)


class SyncBackoffTests(_SyncTestBase):
    """⭐ 审计修复（2026-10-27，P3-12）：回读失败退避 1/5/15/60 分钟，上限停止。"""

    def test_failed_row_is_backed_off_so_the_next_round_does_not_hit_upstream(self):
        push = self._push(index=12, state=DailiantongPush.State.SENT, serial=SERIAL_BAD)
        self.fake.failing_serials.add(SERIAL_BAD)

        first_out, _ = self._run_once()
        self.assertEqual(self.fake.detail_calls_for(SERIAL_BAD), 1)
        self.assertIn("失败 1", first_out)
        push.refresh_from_db()
        self.assertEqual(push.sync_attempts, 1)
        self.assertIsNotNone(push.next_sync_at)

        second_out, _ = self._run_once()

        self.assertEqual(
            self.fake.detail_calls_for(SERIAL_BAD), 1, "退避窗口内⛔ 不许再打上游"
        )
        self.assertIn("轮询 0", second_out)
        self.assertIn("退避中 1", second_out)

    def test_after_the_failure_limit_the_row_stops_polling_with_a_reason(self):
        push = self._push(index=13, state=DailiantongPush.State.SENT, serial=SERIAL_BAD)
        self.fake.failing_serials.add(SERIAL_BAD)
        DailiantongPush.objects.filter(pk=push.pk).update(
            sync_attempts=4, next_sync_at=None
        )

        _out, _err = self._run_once()

        push.refresh_from_db()
        self.assertEqual(push.sync_attempts, 5)
        self.assertFalse(push.is_active)
        self.assertIn("已停止自动轮询", push.last_error)
        self.assertIn(UPSTREAM_BUSINESS_ERR, push.last_error)

        calls_before = len(self.fake.calls)
        _out2, _err2 = self._run_once()
        self.assertEqual(len(self.fake.calls), calls_before)

    def test_success_resets_the_backoff_counters(self):
        push = self._push(index=14, state=DailiantongPush.State.SENT, serial=SERIAL_GOOD)
        DailiantongPush.objects.filter(pk=push.pk).update(
            sync_attempts=3, next_sync_at=None
        )

        self._run_once()

        push.refresh_from_db()
        self.assertEqual(push.sync_attempts, 0)
        self.assertIsNone(push.next_sync_at)


class ProcessLockTests(_SyncTestBase):
    """⭐ 审计修复（2026-10-27，P1-3）：第二个同步进程直接退出。"""

    def test_second_process_is_rejected_with_a_clear_message(self):
        from apps.business.management.commands import dailiantong_sync as module

        path = module.sync_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        holder = path.open("a+", encoding="utf-8")
        self.addCleanup(holder.close)
        import fcntl

        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(fcntl.flock, holder.fileno(), fcntl.LOCK_UN)

        with self.assertRaises(CommandError) as ctx:
            self._run_once()

        self.assertIn("另一个 dailiantong_sync 进程正在运行", str(ctx.exception))


class GuardSectionExitCodeTests(_SyncTestBase):
    """⭐ 审计修复（2026-10-27，P3-14）：段级异常计入 unexpected，--once 非零退出。"""

    def test_section_exception_makes_once_exit_nonzero_but_other_section_still_runs(self):
        from apps.business.management.commands import dailiantong_sync as module

        dispatch = mock.MagicMock(return_value=(1, 0))
        out, err = io.StringIO(), io.StringIO()
        # ⚠️ **夹具适配**：判据「段级异常互不阻塞」按「每个租户各跑一次」写
        #    （`call_count == 1`）⇒ 默认本命令只看到**一个**租户。实测：同一进程里先跑过
        #    `test_dailiantong_push.PushIsolationTests`（它 provision 了租户 B）后，public
        #    schema 里的 `Tenant` 行会留下来（django-tenants 的既有测试隔离形态）⇒ 会跑两个
        #    租户、`call_count == 2`（基线提交上复跑过，红与本线改动无关）。
        #    修法是把租户集合钉成「只有本用例这一个」（⛔ 不放宽 `call_count == 1` 这条断言）。
        with mock.patch.object(
            module.Command, "_send_group_qr", side_effect=RuntimeError("qr boom")
        ), mock.patch.object(module.Command, "_dispatch_auto_replies", dispatch), \
             mock.patch.object(
                 module.Command, "_iter_tenants", return_value=[self.tenant]
             ):
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    "dailiantong_sync", "--once", stdout=out, stderr=err
                )

        # ⭐ 口径（并行抖动实测：单跑必绿、并行下偶发红）：
        #    断言写死 `== 1`（只有群码段失败）会在机器负载高时被别的段（网络/超时类）带红
        #    ——本命令的 returncode 是**各段按位或**，负载高时别的段偶尔也失败、值变成 3 / 5 …
        #    ⇒ 那条断言在**与本用例无关**的情况下变红，把人引向错误方向。
        #    这里只钉本用例真正关心的事实：①退出码**非 0**（有段失败要响亮）；
        #    ②群码段那一位**确实**被置上（不然「群码炸了」这条压根没被记录）。
        #    ⛔ 不要退回 `== 1`：那等于假设「别的段永不失败」，在并行分片下不成立。
        self.assertNotEqual(ctx.exception.returncode, 0)
        self.assertTrue(
            ctx.exception.returncode & 1,
            f"群码段的失败位没被置上：returncode={ctx.exception.returncode}",
        )
        # 群码段炸了，自动回复段仍然跑完（互不阻塞）。
        self.assertEqual(dispatch.call_count, 1)
        self.assertIn("自动回复 1", out.getvalue())
        self.assertNotIn("qr boom", err.getvalue())


class CommandContractTests(_SyncTestBase):
    def test_once_and_loop_together_is_rejected(self):
        from apps.business.management.commands import dailiantong_sync as module

        def _no_looping(_schema, _interval):
            raise AssertionError("守卫失效：命令进入了常驻循环（--once 被忽略）")

        with mock.patch.object(module.Command, "_loop", side_effect=_no_looping):
            with self.assertRaises(CommandError):
                call_command("dailiantong_sync", "--once", "--loop")

    def test_unknown_tenant_schema_raises_command_error(self):
        with self.assertRaises(CommandError):
            call_command("dailiantong_sync", "--once", "--tenant", "no-such-schema")

    def test_interval_must_be_a_positive_integer_for_loop(self):
        for value in ("0", "-1"):
            with self.subTest(value=value):
                with self.assertRaises(CommandError):
                    call_command("dailiantong_sync", "--loop", "--interval", value)

    def test_interval_is_ignored_by_once(self):
        """help 写明「仅 --loop 使用」⇒ `--once --interval 0` 不许把排障命令挡下来。"""
        self._push(index=8, state=DailiantongPush.State.SENT, serial="19000000000000000010")
        out, _err = self._run_once("--interval", "0")
        self.assertIn("轮询 1", out)

    def test_default_loop_interval_is_one_hour(self):
        """⭐ h13（2026-10-28 业务方）：回调为主 ⇒ 兜底轮询默认 1 小时（原 30 秒）。"""
        self.assertEqual(sync_module.LOOP_INTERVAL_SECONDS, 3600)


class CallbackFirstLoopTests(_SyncTestBase):
    """⭐ h13：回调是主路径 —— 回调触发的轮次只回读回调命中的行，不扫其它行。"""

    def test_a_callback_round_refreshes_only_the_callback_row(self):
        self._push(index=1, state=DailiantongPush.State.SENT, serial=SERIAL_CALLBACK)
        self._push(index=2, state=DailiantongPush.State.SENT, serial=SERIAL_SENT)
        self._write_callback_event(f"SerialNo={SERIAL_CALLBACK}&Status=12&CancelStatus=10")

        command = sync_module.Command()
        command.stdout = io.StringIO()
        with mock.patch(
            "apps.business.dailiantong_push._open_client", side_effect=self._client_factory
        ), mock.patch.object(
            sync_module.Command, "_iter_tenants", return_value=[self.tenant]
        ):
            unexpected = command._run_once(None, poll_upstream=False)

        self.assertFalse(unexpected)
        self.assertEqual(
            self.fake.detail_calls_for(SERIAL_CALLBACK), 1,
            "回调命中的行必须立刻回读",
        )
        self.assertEqual(
            self.fake.detail_calls_for(SERIAL_SENT), 0,
            "回调触发的轮次⛔ 不扫其它 sent / unknown 行（1 小时兜底才扫）",
        )
        summary = command.stdout.getvalue()
        self.assertIn("回调触发", summary)
        self.assertIn("本轮不扫其它行", summary)

    def test_loop_wakes_on_callback_file_growth_without_waiting_the_hour(self):
        """`_loop` 在事件文件增长时立刻跑一轮**不轮询**的；完整轮仍按 interval。"""

        class _StopAfterThreeTicks:
            """第 4 次 `is_set()` 返回 True（跑完 3 个 tick 就退出，用例不睡）。"""

            def __init__(self):
                self.checks = 0

            def is_set(self):
                self.checks += 1
                return self.checks > 3

            def wait(self, _seconds):
                return None

        command = sync_module.Command()
        command._run_once = mock.Mock(return_value=False)
        # 初始 5 字节；第 2 个 tick 变成 10 字节（= 有回调落盘）⇒ 必须多跑一轮。
        with mock.patch.object(sync_module, "_STOP", _StopAfterThreeTicks()), \
             mock.patch.object(
                 sync_module, "_callback_events_size",
                 side_effect=[5, 5, 10, 10],
             ), mock.patch.object(
                 sync_module, "_callbacks_pending", return_value=False
             ):
            command._loop(None, 3600)

        self.assertEqual(command._run_once.call_count, 2)
        self.assertEqual(
            command._run_once.call_args_list[0].kwargs, {"poll_upstream": True}
        )
        self.assertEqual(
            command._run_once.call_args_list[1].kwargs, {"poll_upstream": False}
        )

    def test_callbacks_pending_is_true_only_for_complete_unread_lines(self):
        """⭐ 一轮 500 条上限的积压：长度没变也要能继续唤醒（只认完整行）。"""
        self._write_callback_event(f"SerialNo={SERIAL_SENT}&Status=12")
        offset_path = sync_module.callback_offset_path(self.tenant.schema_name)

        # 还没有偏移文件（进程第一次跑）⇒ False：由完整轮负责。
        self.assertFalse(sync_module._callbacks_pending())

        sync_module._write_offset(offset_path, 0)
        self.assertTrue(sync_module._callbacks_pending(), "有完整未读行 ⇒ 要再跑一轮")

        size = self.events_path.stat().st_size
        sync_module._write_offset(offset_path, size)
        self.assertFalse(sync_module._callbacks_pending(), "读完了就没有积压")

        # 只剩半行（接收器正在写）⇒ False：等文件增长，不要空转。
        self.events_path.write_text('{"body": "SerialNo=19000000000000000001"}', encoding="utf-8")
        sync_module._write_offset(offset_path, 0)
        self.assertFalse(sync_module._callbacks_pending())


class ReceiverTests(SimpleTestCase):
    """回调接收器：验签 → 落盘 → 200；失败档一律不落盘。

    ⚠️ 用 `SimpleTestCase`（**不用数据库**）正是这条线的边界：接收器连 Django 都没 import。
    如果哪天有人在接收器里加了库调用，这组用例本身仍会跑过——真正的边界靠接收器模块的
    import 面守（它只用标准库）。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dlt-receiver-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.env_file = Path(self.tmpdir) / "dailiantong-callback.env"
        self.events_file = Path(self.tmpdir) / "dailiantong-callback.jsonl"
        self.env_file.write_text(f"{receiver.SECRET_VAR_NAME}={CALLBACK_SECRET}\n", encoding="utf-8")
        patcher = mock.patch.dict(
            os.environ,
            {
                "DAILIANTONG_CALLBACK_ENV_FILE": str(self.env_file),
                "DAILIANTONG_CALLBACK_EVENTS_PATH": str(self.events_file),
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── 小工具 ──────────────────────────────────────────────────────
    @staticmethod
    def _signed_body(pairs: list[tuple[str, str]], secret: str = CALLBACK_SECRET) -> str:
        """按 §7.1 口径算签名：**只拼参数值**（不含 Sign）+ AppSecret。"""
        presign = "".join(value for _key, value in pairs)
        sign = hashlib.md5((presign + secret).encode("utf-8")).hexdigest()
        return urlencode([*pairs, ("Sign", sign)])

    def _call(self, body: str, *, method: str = "POST", path: str | None = None) -> tuple[str, bytes]:
        body_bytes = body.encode("utf-8")
        captured: dict = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": receiver.NOTIFY_PATH if path is None else path,
            "CONTENT_TYPE": "application/x-www-form-urlencoded",
            "CONTENT_LENGTH": str(len(body_bytes)),
            "wsgi.input": io.BytesIO(body_bytes),
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_X_REAL_IP": "120.26.194.70",
        }
        payload = b"".join(receiver.application(environ, start_response))
        return captured["status"], payload

    def _recorded(self) -> list[dict]:
        with self.events_file.open("r", encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]

    # ── 判据 ────────────────────────────────────────────────────────
    def test_valid_signature_is_stored_and_returns_200_ok(self):
        pairs = [
            ("SerialNo", SERIAL_CALLBACK),
            ("Status", "12"),
            ("StatusName", "正在代练"),
            ("ExOrderNo", "202610260000000001"),
        ]
        body = self._signed_body(pairs)

        status, payload = self._call(body)

        self.assertTrue(status.startswith("200"), status)
        self.assertEqual(payload, b"ok")
        records = self._recorded()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(
            set(record), {"ts", "peer", "real_ip", "body", "verified"},
            "落盘记录的字段是接收器与消费侧的共同契约，⛔ 不要随手加字段",
        )
        self.assertEqual(record["body"], body)
        self.assertIs(record["verified"], True)
        self.assertEqual(record["peer"], "127.0.0.1")
        self.assertEqual(record["real_ip"], "120.26.194.70")
        # ⛔ 密钥绝不落盘。
        self.assertNotIn(CALLBACK_SECRET, self.events_file.read_text(encoding="utf-8"))

    def test_distributed_receiver_waits_for_the_event_and_directory_to_sync(self):
        body = self._signed_body([("SerialNo", SERIAL_CALLBACK), ("Status", "12")])
        with mock.patch.dict(os.environ, {"DAILIANTONG_CALLBACK_FSYNC": "1"}):
            with mock.patch.object(receiver.os, "fsync", wraps=os.fsync) as sync:
                self.assertEqual(self._call(body), ("200 OK", b"ok"))
                self.assertEqual(sync.call_count, 2)
            with mock.patch.object(receiver.os, "fsync", side_effect=OSError("synthetic sync failure")):
                self.assertEqual(self._call(body), ("500 Internal Server Error", b"store failed"))

    def test_wrong_signature_returns_403_and_writes_nothing(self):
        pairs = [("SerialNo", SERIAL_CALLBACK), ("Status", "12")]
        body = self._signed_body(pairs, secret="synth-wrong-secret")

        status, _payload = self._call(body)

        self.assertTrue(status.startswith("403"), status)
        self.assertFalse(self.events_file.exists(), "验签不过 ⇒ ⛔ 一个字节都不落盘")

    def test_missing_signature_returns_403_and_writes_nothing(self):
        body = urlencode([("SerialNo", SERIAL_CALLBACK), ("Status", "12")])

        status, _payload = self._call(body)

        self.assertTrue(status.startswith("403"), status)
        self.assertFalse(self.events_file.exists())

    def test_signature_is_order_sensitive(self):
        """按参数名排序再算 ⇒ 对不上（§7.1【实测】）。"""
        pairs = [("SerialNo", SERIAL_CALLBACK), ("Status", "12")]
        sign = hashlib.md5(
            ("".join(value for _key, value in pairs) + CALLBACK_SECRET).encode("utf-8")
        ).hexdigest()
        # 报文里的顺序与算签名时的顺序相反（值相同、顺序不同）。
        body = urlencode(
            [("Status", "12"), ("SerialNo", SERIAL_CALLBACK), ("Sign", sign)]
        )

        status, _payload = self._call(body)

        self.assertTrue(status.startswith("403"), status)
        self.assertFalse(self.events_file.exists())

    def test_missing_secret_file_returns_503_fail_closed(self):
        self.env_file.unlink()
        body = self._signed_body([("SerialNo", SERIAL_CALLBACK), ("Status", "12")])

        status, _payload = self._call(body)

        self.assertTrue(status.startswith("503"), status)
        self.assertFalse(
            self.events_file.exists(),
            "读不到密钥时必须 fail-closed（503 且不落盘），⛔ 不许跳过验签",
        )

    def test_only_the_exact_path_and_post_are_accepted(self):
        body = self._signed_body([("SerialNo", SERIAL_CALLBACK), ("Status", "12")])

        status, _ = self._call(body, path="/dailiantong/v1/notify/")
        self.assertTrue(status.startswith("404"), status)

        status, _ = self._call(body, method="GET")
        self.assertTrue(status.startswith("405"), status)

        self.assertFalse(self.events_file.exists())


class InternalClaimReconcileTests(_SyncTestBase):
    """⭐ **内部打手先接 ⇒ 及时把代练通订单撤掉**。

    判据口径：
    - 只处理「本地有**真实**接手人（不是占位账号）+ 远端**还没人接**（11/10/未回读）」的行；
    - 远端已接走是「两边同时接」⇒ **保代练通**（本地打手由状态回写撤销），⛔ 这里不撤；
    - 未接手 ⇒ 删除（即时退款，`state=closed`）；已接手 ⇒ 协商撤销（用 `claim_withdraw_at` 挡重复）；
    - 撤回失败**有界重试**（3 次后停，原因进 `remote_note`），⛔ 不每 30 秒无限打上游。
    """

    def _push_for(self, order, serial: str, *, remote: int = 11):
        """在**已有订单**上造一条已发出的推送行（⛔ 不要再用 `_push`——它会另建一张单）。"""
        push = DailiantongPush.objects.create(
            order=order,
            platform=10,
            state=DailiantongPush.State.SENT,
            serial_no=serial,
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=order.order_no,
            remote_status=remote,
            attempts=1,
        )
        self.fake.details[serial] = {"Status": remote, "CancelStatus": 10}
        return push

    def _order_with_internal_taker(self, index: int, serial: str, *, remote: int = 11):
        order = self._order(index)
        taker = get_user_model().objects.create(username=f"internal-taker-{index}")
        Performance.objects.create(
            order=order,
            emp_type=EmpType.MAIN,
            user=taker,
            temp_amount=Decimal("5.00"),
        )
        # 本地打手先接 ⇒ 订单在「代练中」（与 `claim_order` 的真实终态一致）。
        # ⚠️ 也补上发单价：远端接手后要把接手人换成占位账号，绩效重算需要一个基价。
        Order.objects.filter(pk=order.pk).update(
            status=OrderStatus.IN_PROGRESS, hall_price=Decimal("5.00")
        )
        order.refresh_from_db()
        push = self._push_for(order, serial, remote=remote)
        return order, push

    def test_an_unaccepted_platform_order_is_deleted_when_an_internal_taker_took_it(self):
        order, push = self._order_with_internal_taker(11, "19000000000000000101")

        out, _err = self._run_once()

        self.assertEqual(self.fake.actions.count("LevelOrderDelSelf"), 1)
        self.assertEqual(
            self.fake.forms_for("LevelOrderDelSelf")[0]["ODSerialNo"],
            "19000000000000000101",
        )
        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.CLOSED)
        self.assertIsNotNone(push.claim_withdraw_at)
        self.assertIn("内部打手已先接单", push.remote_note)
        self.assertEqual(push.claim_withdraw_attempts, 0)
        # 本地订单仍是「代练中」（内部打手在做），⛔ 不被远端 11 拉回大厅。
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertTrue(
            OperationLog.objects.filter(
                action="order.dailiantong_withdraw",
                order_no=order.order_no,
                # 机器来源的唯一库层判据：`actor_id IS NULL`（`record()` 的 fail-closed
                # 要求这种行必须显式声明 `system=True`，本行是它落到库里的形态）。
                actor_id__isnull=True,
            ).exists(),
            "机器撤回必须留审计（机器来源）",
        )
        self.assertIn("内部先接已撤代练通 1", out)

    def test_a_platform_accepted_order_is_left_to_the_platform_and_the_internal_taker_is_rolled_back(self):
        """两边同时接 ⇒ 保代练通：本地打手被撤销、状态按远端回写，⛔ **不删**代练通单。"""
        order, push = self._order_with_internal_taker(
            12, "19000000000000000102", remote=12
        )

        self._run_once()

        self.assertNotIn("LevelOrderDelSelf", self.fake.actions)
        push.refresh_from_db()
        self.assertIsNone(push.claim_withdraw_at)
        self.assertIn("已撤销", push.remote_note)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(
            perf.user.username, dailiantong_push_module.VIRTUAL_TAKER_USERNAME
        )

    def test_a_failed_delete_is_retried_with_a_bounded_budget(self):
        order, push = self._order_with_internal_taker(13, "19000000000000000103")
        self.fake.delete_response = {"Result": -1, "Err": "订单异常，无法删除"}

        self._run_once()

        push.refresh_from_db()
        self.assertEqual(push.claim_withdraw_attempts, 1)
        self.assertIn("撤回代练通失败（第 1 次）", push.remote_note)
        self.assertIsNone(push.claim_withdraw_at)

        # 已到上限 ⇒ 下一轮不再打上游（⛔ 不无限重试）。
        DailiantongPush.objects.filter(pk=push.pk).update(
            claim_withdraw_attempts=3
        )
        before = self.fake.actions.count("LevelOrderDelSelf")
        self._run_once()
        self.assertEqual(self.fake.actions.count("LevelOrderDelSelf"), before)
        push.refresh_from_db()
        self.assertEqual(push.claim_withdraw_attempts, 3)

    def test_an_already_withdrawn_row_is_not_processed_again(self):
        order, push = self._order_with_internal_taker(14, "19000000000000000104")
        DailiantongPush.objects.filter(pk=push.pk).update(
            claim_withdraw_at=timezone.now()
        )

        self._run_once()

        self.assertNotIn("LevelOrderDelSelf", self.fake.actions)

    def test_an_order_without_a_real_taker_is_not_touched(self):
        """没有内部接手人 ⇒ 这条链属「只抛到代练通」，⛔ 不撤。"""
        order = self._order(15)
        self._push_for(order, "19000000000000000105", remote=11)

        self._run_once()

        self.assertNotIn("LevelOrderDelSelf", self.fake.actions)

    def test_an_off_shelf_platform_order_is_deleted_like_the_immediate_path(self):
        """⭐ 兜底与即时路径对 None / 10 / 18 统一成「未接手 ⇒ 删除」。

        ⚠️ 两条路必须同口径：即时路径已按业务方口径真机放宽到 18 删除，若兜底仍「18 跳过」，
        同一个远端状态两种结论本身就是 bug 面（即时撤回失败后那条行要靠兜底接手）。
        18 归进「未接手族」⇒ 不换成占位账号；因为本地已有真实接手人，平台这张单要撤掉 ⇒
        `reconcile` 走删除（`allow_unaccepted_delete=True`），成功后记 `claim_withdraw_at`。
        """
        order, push = self._order_with_internal_taker(
            16, "19000000000000000106", remote=18
        )

        self._run_once()

        self.assertIn("LevelOrderDelSelf", self.fake.actions)
        self.assertNotIn("LevelOrderCancel", self.fake.actions)
        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.CLOSED)
        self.assertIsNotNone(push.claim_withdraw_at)
        self.assertEqual(push.claim_withdraw_attempts, 0)
        self.assertIn("内部打手已先接单", push.remote_note)
        # 18 不算已接手 ⇒ 内部接手人原样保留（不是「代练通打手」占位账号）。
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user.username, "internal-taker-16")


class CallbackWritesLocalStatusTests(_SyncTestBase):
    """⭐ 「**含回调触发的回读**」也要回写本地订单状态。

    回调只当闹钟（报文里的 `Status` 一个字都不许直接落库），但它触发的那次
    `refresh_push_status` **必须**把远端状态映射成本地状态 —— 否则「回调到了、
    页面还停在待抢单」，正是业务方说的「绝不能不一致」。
    """

    def test_a_callback_triggered_readback_writes_the_local_status(self):
        order = self._order(21)
        Order.objects.filter(pk=order.pk).update(hall_price=Decimal("5.00"))
        push = DailiantongPush.objects.create(
            order=order,
            platform=10,
            state=DailiantongPush.State.SENT,
            serial_no="19000000000000000201",
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=order.order_no,
            attempts=1,
        )
        # 详情说「等待验收」（13）；回调报文里塞一个**假的** 99 证明它不被采信。
        self.fake.details["19000000000000000201"] = {
            "Status": 13,
            "CancelStatus": 10,
            "AcceptUID": "USR-CB",
            "AcceptUserName": "回调打手",
        }
        self._write_callback_event(
            urlencode(
                [
                    ("SerialNo", "19000000000000000201"),
                    ("Status", "99"),
                    ("CancelStatus", "98"),
                    ("Sign", "ignored-by-consumer"),
                ]
            )
        )

        self._run_once()

        order.refresh_from_db()
        push.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW, "回调触发的回读必须回写本地状态")
        self.assertEqual(push.remote_status, 13, "落库的是详情里的 13，⛔ 不是回调里的 99")
        self.assertEqual(push.remote_taker_uid, "USR-CB")


# ── 回调事件文件轮转 / 保留期────────────────────────────────


class CallbackRotationTests(_SyncTestBase):
    """现役文件超阈值 + **所有租户都读完** ⇒ 轮转成归档段、新建空文件、偏移归零。

    ⚠️ 轮转的前提是「每一行都被每个租户消费过」——否则会静默丢事件（本类逐条钉住）。
    """

    def _offset_path(self):
        return sync_module.callback_offset_path(self.tenant.schema_name)

    def _write_events(self, lines: int = 3) -> int:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(
            json.dumps({"body": f"SerialNo=S{index}&Status=12"}, ensure_ascii=False) + "\n"
            for index in range(lines)
        )
        self.events_path.write_text(body, encoding="utf-8")
        return self.events_path.stat().st_size

    def _set_rotate_bytes(self, value: int):
        patcher = mock.patch.dict(
            os.environ, {"DAILIANTONG_CALLBACK_ROTATE_BYTES": str(value)}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_fully_consumed_file_is_rotated_and_offsets_reset(self):
        size = self._write_events()
        self._offset_path().write_text(str(size), encoding="utf-8")
        self._set_rotate_bytes(10)

        archive = sync_module.rotate_callback_events()

        self.assertIsNotNone(archive)
        self.assertTrue(archive.exists(), "归档段必须留着（事后取证）")
        self.assertEqual(self.events_path.read_text(encoding="utf-8"), "")
        self.assertEqual(
            self._offset_path().read_text(encoding="utf-8"), "0",
            "轮转后偏移必须归零，否则旧偏移会跳过新文件开头",
        )
        self.assertEqual(archive.stat().st_size, size)

    def test_a_file_with_an_unread_tail_is_never_rotated(self):
        size = self._write_events()
        self._offset_path().write_text(str(size - 1), encoding="utf-8")
        self._set_rotate_bytes(10)

        self.assertIsNone(sync_module.rotate_callback_events())
        self.assertTrue(self.events_path.exists())
        self.assertEqual(self.events_path.stat().st_size, size)

    def test_no_offset_files_means_no_rotation(self):
        self._write_events()
        self._set_rotate_bytes(10)
        self.assertIsNone(
            sync_module.rotate_callback_events(),
            "还没有任何租户读过 ⇒ ⛔ 轮转（否则整段被静默跳过）",
        )

    def test_a_small_file_is_not_rotated(self):
        self._write_events()
        self._offset_path().write_text("0", encoding="utf-8")
        self._set_rotate_bytes(1024 * 1024)
        self.assertIsNone(sync_module.rotate_callback_events())

    # ── ⭐ M6（审计）：失效租户的偏移文件要「忽略 + 清」 ──────────

    def _ghost_offset(self, schema="ghost_tenant_zz"):
        """一个**租户表里不存在**的 schema 的偏移文件（下线 / 改名留下的）。"""
        path = sync_module.callback_offset_path(schema)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("0", encoding="utf-8")
        return path

    def test_a_stale_tenant_offset_is_ignored_by_the_backlog_probe(self):
        """失效租户偏移 = 0 不能让 `min(offsets)` 永远停在 0（每 2 秒空转一轮维护段）。"""
        size = self._write_events()
        self._offset_path().write_text(str(size), encoding="utf-8")
        ghost = self._ghost_offset()

        self.assertNotIn(
            ghost, sync_module._offset_files(), "失效租户的偏移不该进入「现役」集合"
        )
        self.assertFalse(
            sync_module._callbacks_pending(),
            "现役租户都读完了 ⇒ 不该因为有失效租户的 0 偏移就判「还有积压」",
        )

    def test_stale_tenant_offsets_are_pruned_and_do_not_block_rotation(self):
        """轮转条件 `min(现役 offsets) >= size` 不能被失效租户的旧偏移卡成永假。"""
        size = self._write_events()
        self._offset_path().write_text(str(size), encoding="utf-8")
        ghost = self._ghost_offset()
        self._set_rotate_bytes(10)

        removed = sync_module._prune_stale_offset_files()

        self.assertEqual(removed, [ghost])
        self.assertFalse(ghost.exists(), "失效租户的偏移文件应被清掉（不只是忽略）")
        self.assertTrue(self._offset_path().exists(), "现役租户的偏移⛔ 不许被误删")
        self.assertIsNotNone(
            sync_module.rotate_callback_events(),
            "清掉失效偏移后，轮转条件应当成立",
        )

    def test_the_full_round_prunes_stale_offsets(self):
        self._write_events()
        ghost = self._ghost_offset()

        self._run_once()

        self.assertFalse(ghost.exists(), "完整轮的维护段应顺手清掉失效租户的偏移")

    def test_the_full_round_rotation_runs_without_touching_the_push_flow(self):
        size = self._write_events()
        self._offset_path().write_text(str(size), encoding="utf-8")
        self._set_rotate_bytes(10)

        out, err = self._run_once()

        self.assertEqual(err, "")
        self.assertIn("轮询 0", out)
        self.assertTrue(
            any(
                item.name.startswith(self.events_path.name + ".")
                and not item.name.endswith(".lock")
                for item in self.events_path.parent.iterdir()
            ),
            "全租户跑法应在开头做一次轮转",
        )
        self.assertEqual(self.events_path.read_text(encoding="utf-8"), "")

    def test_archives_older_than_the_retention_are_deleted(self):
        size = self._write_events()
        self._offset_path().write_text(str(size), encoding="utf-8")
        self._set_rotate_bytes(10)
        archive = sync_module.rotate_callback_events()
        self.assertIsNotNone(archive)

        patcher = mock.patch.dict(
            os.environ, {"DAILIANTONG_CALLBACK_RETENTION_SECONDS": "60"}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        old = time.time() - 120
        os.utime(archive, (old, old))

        removed = sync_module.prune_callback_archives()

        self.assertEqual(removed, [archive])
        self.assertFalse(archive.exists())

    def test_a_fresh_archive_is_kept(self):
        size = self._write_events()
        self._offset_path().write_text(str(size), encoding="utf-8")
        self._set_rotate_bytes(10)
        archive = sync_module.rotate_callback_events()
        patcher = mock.patch.dict(
            os.environ, {"DAILIANTONG_CALLBACK_RETENTION_SECONDS": "3600"}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        removed = sync_module.prune_callback_archives()

        self.assertEqual(removed, [])
        self.assertTrue(archive.exists())

    def test_the_receiver_locks_the_shared_lock_file_not_the_data_file(self):
        """接收器追加写拿的是 `<事件文件>.lock`（消费侧轮转也拿同一把）。

        结构性判据（够用且不会误报）：`_append_record` 里必须出现 `lock_path_for(path)`，
        且锁文件名后缀与命令侧的 `CALLBACK_LOCK_SUFFIX` 一致。⚠️ 若有人改回「锁数据文件」，
        轮转时两边会锁到两个不同 inode（= 丢行），这条会先红。
        """
        source = inspect.getsource(receiver._append_record)
        self.assertIn("lock_path_for(path)", source)
        self.assertNotIn("flock(fd", source)
        self.assertEqual(receiver.LOCK_SUFFIX, sync_module.CALLBACK_LOCK_SUFFIX)
