"""⭐ H3-1（2026-10-28）：**撞单 0 撤**。

业务口径（业务方原话）：「若发生撞单（多个平台同时接手）时，系统会保留**最早接单**的那个，
其它所有平台全部**申请 0 撤**」。

## 覆盖什么

| 组 | 判据 |
|---|---|
| 两条同时接手 | 只撤**较晚**那条，最早那条一个字节都不动（0 撤的参数逐项字面断言） |
| 三条及以上 | 只留最早一条，其余各走一遍（能撤的撤、未接入平台只写原因） |
| 开关关闭 | 这一段**零上游调用**（连一次额外的详情回读都没有） |
| 幂等 | 同一条推送只发起一次（第二轮不再打上游） |
| 失败退避 | 上游拒 ⇒ 记次数 + 6 小时退避 + 可读原因；上限到了停手，⛔ 不刷屏 |
| 「最早接单」判据 | `remote_accepted_at` 优先；缺失退化到推送创建时间 |
| 人工撤销在途 | `cancel_requested_at` 非空的推送**不参与**撞单判定（⛔ 不覆盖人的决定） |
| 写前回读 | 回读显示撤销流程已在走 ⇒ 不重复申请、只记收口 |
| 每轮触发 | `dailiantong_sync --once` 每轮都调这一段（⛔ 不另起 systemd 单元） |

## ⚠️ 撞单为什么是「两个平台」

`DailiantongPush.Meta.constraints` 的 `uniq_dailiantong_push_active` 规定
「同一 (订单, 平台) 至多一条有效推送」⇒ 同一订单下能同时存在的多条已接手推送，
**只可能来自不同平台**。本文件因此用 `platform=10`（代练通）+ `platform=30/80`
（未接入的其它平台）来构造撞单。⛔ 不要为了「同平台两条也能撞」去放宽那条约束
（它挡的是重复抛单 = 真钱重复建单）。

## 不访问真实网络

沿用 `tests/test_dailiantong_sync.py` 的假上游（`httpx.MockTransport` +
`dailiantong_push._open_client` 这个唯一构造点），本文件只**扩**一个 `LevelOrderCancel`。
一切凭据 / 单号都是**合成值**。
"""

from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qsl

import httpx
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.audit.models import OperationLog
from apps.business import dailiantong_push as push_module
from apps.business.dailiantong_actions import CONFLICT_ZERO_REFUND_COMMENT
from apps.business.management.commands import dailiantong_sync as sync_module
from apps.business.models import (
    DailiantongPush,
    Order,
    OrderStatus,
    PlatformPushSettings,
)
from tests.test_dailiantong_sync import (  # noqa: E402 —— 重夹具，见 `test_platform_adapters.py` 的先例
    _FakeUpstream,
    _SyncTestBase,
)

#: 合成上游单号（⛔ 不是真实单号）。
SERIAL_EARLY = "19000000000000000201"
SERIAL_LATE = "19000000000000000202"
SERIAL_THIRD = "19000000000000000203"
#: 同一测试类里还有 `setUp` 建的推送 ⇒ 这些用例自己再建行时换一组单号（`serial_no` 全局唯一）。
SERIAL_EARLY_B = "19000000000000000204"
SERIAL_LATE_B = "19000000000000000205"

#: 代练通 = 10（本仓唯一接入的平台）；另外两个是「未接入」的平台（只用来构造撞单）。
DAILIANTONG = 10
OTHER_A = 30
OTHER_B = 80


class _ConflictFake(_FakeUpstream):
    """在 `_SyncTestBase` 的假上游上补两件：`LevelOrderCancel` + 撤销后的远端状态。"""

    def __init__(self):
        super().__init__()
        #: 上游对 `LevelOrderCancel` 的回包（默认成功）。
        self.cancel_response: dict = {"Result": 1, "Err": "撤销成功."}
        #: 撤销成功后**回读**到的远端状态。真机（`docs/DAILIANTONG-API.md` §6.9）是
        #: `Status=16 / CancelStatus=11`；这里按「协商撤销中、订单还在做」给 ——
        #: 这样「下一轮不该重复撤」那条用例才有判别力（候选集仍然命中它）。
        self.detail_after_cancel: dict = {"Status": 12, "CancelStatus": 11}

    def cancelled_serials(self) -> list[str]:
        return [
            form.get("ODSerialNo")
            for action, form in self.calls
            if action == "LevelOrderCancel"
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode("utf-8")))
        action = form.get("Action", "")
        if action == "LevelOrderCancel":
            self.calls.append((action, form))
            if self.cancel_response.get("Result") == 1:
                # 写后回读看到的就是 `detail_after_cancel`（按单号落进假详情的表里）。
                serial = form.get("ODSerialNo", "")
                detail = dict(self.details.get(serial) or {})
                detail.update(self.detail_after_cancel)
                self.details[serial] = detail
            return httpx.Response(200, json=self.cancel_response)
        return super().handler(request)


class _ConflictBase(_SyncTestBase):
    """同一订单 + 多条（跨平台）已接手的推送 + 一个可用的代练通配置。"""

    def setUp(self):
        super().setUp()
        self.fake = _ConflictFake()
        #: 建一行设置（默认值 = 证据件 SK145 的 Y ⇒ 开）。
        self.settings = PlatformPushSettings.get_solo()

    # ── 夹具 ────────────────────────────────────────────────────────
    def _shared_order(self, *, index: int = 1) -> Order:
        return Order.objects.create(
            game=self.game,
            server=self.server,
            order_no=f"20261028000000{index:04d}",
            status=OrderStatus.WAITING_GRAB,
            project_text="星耀上王者",
            total_price=Decimal("120.00"),
        )

    def _push_on(
        self,
        order: Order,
        *,
        serial: str,
        platform: int = DAILIANTONG,
        remote_status: int = 12,
        accepted_at=None,
        created_at=None,
        **extra,
    ) -> DailiantongPush:
        push = DailiantongPush.objects.create(
            order=order,
            platform=platform,
            state=DailiantongPush.State.SENT,
            serial_no=serial,
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=order.order_no,
            attempts=1,
            remote_status=remote_status,
            remote_accepted_at=accepted_at,
            **extra,
        )
        if created_at is not None:
            # `created_at` 是 `auto_now_add` ⇒ 只能建完再显式改（本用例要钉「早/晚」）。
            DailiantongPush.objects.filter(pk=push.pk).update(created_at=created_at)
            push.refresh_from_db()
        return push

    def _accepted(self, *, seconds: int):
        return timezone.now() - timedelta(seconds=seconds)

    def _run_section(self):
        with mock.patch(
            "apps.business.dailiantong_push._open_client",
            side_effect=self._client_factory,
        ):
            return push_module.zero_refund_conflicting_pushes()


class TwoAcceptedPushesTests(_ConflictBase):
    """两条同时接手 ⇒ 只撤**较晚**那条（代练通），最早那条不动。"""

    def setUp(self):
        super().setUp()
        self.order = self._shared_order()
        # 更早接单的是**别的平台**（我们撤不了，也不该撤）；代练通是较晚的那条 ⇒ 0 撤它。
        self.early = self._push_on(
            self.order,
            serial=SERIAL_EARLY,
            platform=OTHER_A,
            accepted_at=self._accepted(seconds=600),
        )
        self.late = self._push_on(
            self.order, serial=SERIAL_LATE, accepted_at=self._accepted(seconds=60)
        )

    def test_only_the_later_push_is_zero_refunded(self):
        self.assertEqual(self._run_section(), (1, 0))

        forms = self.fake.forms_for("LevelOrderCancel")
        self.assertEqual(len(forms), 1, "只该撤较晚的那条")
        form = forms[0]
        self.assertEqual(form["ODSerialNo"], SERIAL_LATE)
        # ── 0 撤的参数逐项钉死（⛔ 不是「从详情取现值」）──
        self.assertEqual(form["Flag"], "0")
        self.assertEqual(form["PayLevelBal"], "0")
        self.assertEqual(form["RepEnsureBal"], "0")
        self.assertEqual(form["Comment"], CONFLICT_ZERO_REFUND_COMMENT)
        self.assertTrue(form.get("PayPass"), "申请撤销必须带支付密码（客户端现算）")

        # 写前 + 写后回读：较晚那条共 2 次详情；最早那条一次都没打。
        self.assertEqual(self.fake.detail_calls_for(SERIAL_LATE), 2)
        self.assertEqual(self.fake.detail_calls_for(SERIAL_EARLY), 0)

        self.early.refresh_from_db()
        self.late.refresh_from_db()
        self.assertIsNone(self.early.conflict_zero_refund_at, "保留最早接单那条")
        self.assertEqual(self.early.conflict_zero_refund_note, "", "被保留的行一个字都不写")
        self.assertIsNotNone(self.late.conflict_zero_refund_at)
        self.assertIn("保留最早接单", self.late.conflict_zero_refund_note)
        self.assertIsNone(
            self.late.cancel_requested_at,
            "撞单 0 撤⛔ 不写 cancel_requested_at（那会让这张单变成可重发）",
        )

        # 机器来源的审计（actor_id=None + 操作人写「代练通同步」）
        row = OperationLog.objects.get(action="order.dailiantong_withdraw")
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.actor, push_module.SYNC_ACTOR)
        self.assertIn(CONFLICT_ZERO_REFUND_COMMENT, row.reason)
        self.assertEqual(row.order_no, self.order.order_no)

    def test_the_earliest_push_wins_even_when_the_later_row_is_created_first(self):
        """判据是「接单时间」，⛔ 不是「谁是先建行 / 谁先被轮询」。"""
        # 本类的 `setUp` 已经建了一对夹具（那是另一张单）；本用例只要自己的那一对。
        DailiantongPush.objects.all().delete()
        order = self._shared_order(index=2)
        # 先建「较晚接单」的**代练通**行（id 更小），后建「最早接单」的其它平台行。
        # ⇒ 按 id 排会保错人；只有按接单时间排才留得住「最早接单」那条。
        self._push_on(order, serial=SERIAL_LATE_B, accepted_at=self._accepted(seconds=60))
        self._push_on(
            order,
            serial=SERIAL_EARLY_B,
            platform=OTHER_A,
            accepted_at=self._accepted(seconds=600),
        )

        self._run_section()

        self.assertEqual(self.fake.cancelled_serials(), [SERIAL_LATE_B])

    def test_a_kept_dailiantong_push_is_never_cancelled(self):
        """反过来：代练通是最早接单的那条 ⇒ 它一个字节都不动（其它平台我们撤不了）。"""
        DailiantongPush.objects.all().delete()
        order = self._shared_order(index=3)
        kept = self._push_on(
            order, serial=SERIAL_EARLY_B, accepted_at=self._accepted(seconds=600)
        )
        other = self._push_on(
            order,
            serial=SERIAL_LATE_B,
            platform=OTHER_A,
            accepted_at=self._accepted(seconds=60),
        )

        self.assertEqual(self._run_section(), (0, 0))

        self.assertEqual(self.fake.forms_for("LevelOrderCancel"), [])
        kept.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNone(kept.conflict_zero_refund_at)
        self.assertIsNone(
            other.conflict_zero_refund_at,
            "未接入平台不置幂等键（适配器上线后还要能撤）",
        )
        self.assertIn("尚未接入 0 撤能力", other.conflict_zero_refund_note)
        self.assertIn("保留最早接单", other.conflict_zero_refund_note)


class OffShelfIsNotAcceptedTests(_ConflictBase):
    """⭐ H7（2026-10-28）：`Status=18` **已下架**不算「已接手」⇒ 不进撞单候选。

    撞单的定义是「同一订单下 ≥ 2 条**已接手**的推送」；已下架 = 没有打手，
    拿它凑数会凭空发起 0 撤（真钱写动作）。判据钉在「零上游调用 + 零幂等键」上。
    """

    def test_an_off_shelf_push_does_not_form_a_conflict(self):
        order = self._shared_order(index=4)
        self._push_on(
            order, serial=SERIAL_EARLY_B, accepted_at=self._accepted(seconds=600)
        )
        # 另一平台只是被下架（18），不是第二条「已接手」。
        self._push_on(
            order,
            serial=SERIAL_LATE_B,
            platform=OTHER_A,
            remote_status=18,
            accepted_at=self._accepted(seconds=60),
        )

        self.assertEqual(self._run_section(), (0, 0))

        self.assertEqual(
            self.fake.forms_for("LevelOrderCancel"), [], "18 不算已接手 ⇒ 不撞单、零 0 撤"
        )
        for push in DailiantongPush.objects.order_by("id"):
            self.assertIsNone(push.conflict_zero_refund_at)
            self.assertEqual(push.conflict_zero_refund_note, "")


class ThreeAcceptedPushesTests(_ConflictBase):
    def test_three_accepted_pushes_keep_only_the_earliest(self):
        order = self._shared_order()
        self._push_on(
            order,
            serial=SERIAL_EARLY,
            platform=OTHER_A,
            accepted_at=self._accepted(seconds=900),
        )
        # 代练通在中间：它不是最早（不保留），也不是最晚（还有一条更晚的）⇒ 撤它。
        self._push_on(order, serial=SERIAL_LATE, accepted_at=self._accepted(seconds=600))
        self._push_on(
            order,
            serial=SERIAL_THIRD,
            platform=OTHER_B,
            accepted_at=self._accepted(seconds=60),
        )

        self.assertEqual(self._run_section(), (1, 0))

        self.assertEqual(
            self.fake.cancelled_serials(),
            [SERIAL_LATE],
            "三条里代练通居中 ⇒ 撤它；最晚那条是未接入平台，撤不了",
        )
        kept = DailiantongPush.objects.get(serial_no=SERIAL_EARLY)
        self.assertIsNone(kept.conflict_zero_refund_at)
        third = DailiantongPush.objects.get(serial_no=SERIAL_THIRD)
        self.assertIn("尚未接入 0 撤能力", third.conflict_zero_refund_note)

    def test_three_platforms_where_dailiantong_is_the_latest(self):
        order = self._shared_order(index=2)
        self._push_on(
            order,
            serial=SERIAL_EARLY,
            platform=OTHER_A,
            accepted_at=self._accepted(seconds=900),
        )
        self._push_on(
            order,
            serial=SERIAL_THIRD,
            platform=OTHER_B,
            accepted_at=self._accepted(seconds=600),
        )
        self._push_on(order, serial=SERIAL_LATE, accepted_at=self._accepted(seconds=60))

        self.assertEqual(self._run_section(), (1, 0))
        self.assertEqual(self.fake.cancelled_serials(), [SERIAL_LATE])


class SwitchOffTests(_ConflictBase):
    def test_switch_off_means_no_upstream_call_at_all(self):
        order = self._shared_order()
        self._push_on(
            order,
            serial=SERIAL_EARLY,
            platform=OTHER_A,
            accepted_at=self._accepted(seconds=600),
        )
        self._push_on(order, serial=SERIAL_LATE, accepted_at=self._accepted(seconds=60))
        PlatformPushSettings.objects.filter(pk=1).update(
            auto_zero_refund_on_conflict=False
        )

        self.assertEqual(self._run_section(), (0, 0))

        self.assertEqual(self.fake.calls, [], "开关关 ⇒ 零上游调用（一次详情都不该有）")
        self.assertFalse(
            DailiantongPush.objects.exclude(conflict_zero_refund_note="").exists(),
            "开关关 ⇒ 一个行都不该被写",
        )

    def test_switch_off_via_the_sync_command_too(self):
        """整条同步命令跑一轮：开关关 ⇒ 只有轮询的详情回读，没有任何写动作。"""
        order = self._shared_order()
        self._push_on(
            order,
            serial=SERIAL_EARLY,
            platform=OTHER_A,
            accepted_at=self._accepted(seconds=600),
        )
        self._push_on(order, serial=SERIAL_LATE, accepted_at=self._accepted(seconds=60))
        PlatformPushSettings.objects.filter(pk=1).update(
            auto_zero_refund_on_conflict=False
        )

        out, err = self._run_once()

        self.assertEqual(err, "")
        self.assertNotIn("LevelOrderCancel", self.fake.actions)
        self.assertIn("轮询 1", out, "只有代练通那条会被轮询（其它平台未接入 ⇒ 跳过）")
        self.assertNotIn("撞单0撤", out)
        # 代练通那条只被轮询回读一次（没有额外的「写前 / 写后」回读）
        self.assertEqual(self.fake.detail_calls_for(SERIAL_LATE), 1)


class IdempotencyAndBackoffTests(_ConflictBase):
    def setUp(self):
        super().setUp()
        order = self._shared_order()
        self.early = self._push_on(
            order,
            serial=SERIAL_EARLY,
            platform=OTHER_A,
            accepted_at=self._accepted(seconds=600),
        )
        self.late = self._push_on(
            order, serial=SERIAL_LATE, accepted_at=self._accepted(seconds=60)
        )

    def test_the_same_push_is_never_zero_refunded_twice(self):
        self.assertEqual(self._run_section(), (1, 0))
        self.assertEqual(self.fake.cancelled_serials(), [SERIAL_LATE])

        # 第一轮之后远端仍是「协商撤销中、订单还在做」（CancelStatus=11 / Status=12）
        # ⇒ 这一行**仍然**是撞单候选，只有幂等键能挡住第二次申请。
        self.late.refresh_from_db()
        self.assertEqual(self.late.remote_status, 12)
        self.assertEqual(self.late.remote_cancel_status, 11)

        self.assertEqual(self._run_section(), (0, 0))
        self.assertEqual(
            self.fake.cancelled_serials(),
            [SERIAL_LATE],
            "同一条推送只许发起一次 0 撤",
        )

    def test_upstream_refusal_is_retried_with_backoff_and_a_readable_reason(self):
        self.fake.cancel_response = {"Result": -1, "Err": "订单异常，无法修改"}
        before = timezone.now()

        self.assertEqual(self._run_section(), (0, 1))

        self.late.refresh_from_db()
        self.assertEqual(self.late.conflict_zero_refund_attempts, 1)
        self.assertIsNone(self.late.conflict_zero_refund_at)
        self.assertIsNotNone(self.late.conflict_zero_refund_next_at)
        self.assertGreaterEqual(
            self.late.conflict_zero_refund_next_at, before + timedelta(hours=6)
        )
        self.assertIn(
            "订单异常，无法修改",
            self.late.conflict_zero_refund_note,
            "可读原因里要有上游原文",
        )
        self.assertIn("订单异常，无法修改", self.late.last_error)
        # 失败也要留一条机器审计（⛔ 静默失败）
        self.assertTrue(
            OperationLog.objects.filter(
                action="order.dailiantong_withdraw", reason__contains="撞单 0 撤失败"
            ).exists()
        )

        # 退避窗口内：不再打上游（同一个 fake 的调用数不增）
        calls_after_first = len(self.fake.calls)
        self.assertEqual(self._run_section(), (0, 0))
        self.assertEqual(len(self.fake.calls), calls_after_first, "退避窗口内⛔ 不许再打上游")

    def test_after_the_attempt_limit_it_stops_for_good(self):
        DailiantongPush.objects.filter(pk=self.late.pk).update(
            conflict_zero_refund_attempts=(
                push_module.MAX_CONFLICT_ZERO_REFUND_ATTEMPTS - 1
            ),
            conflict_zero_refund_next_at=timezone.now() - timedelta(minutes=1),
        )
        self.fake.cancel_response = {"Result": -1, "Err": "订单异常，无法修改"}

        self.assertEqual(self._run_section(), (0, 1))

        self.late.refresh_from_db()
        self.assertEqual(
            self.late.conflict_zero_refund_attempts,
            push_module.MAX_CONFLICT_ZERO_REFUND_ATTEMPTS,
        )
        self.assertIsNone(self.late.conflict_zero_refund_next_at, "到上限后不再排下一轮")
        self.assertIn("已停止自动重试", self.late.conflict_zero_refund_note)

        calls_after = len(self.fake.calls)
        self.assertEqual(self._run_section(), (0, 0))
        self.assertEqual(len(self.fake.calls), calls_after, "到上限后⛔ 一次都不许再打")


class AcceptedTimeCriterionTests(_ConflictBase):
    def test_missing_accepted_time_falls_back_to_the_push_creation_time(self):
        """上游没回 `StartDate` ⇒ 退化到推送创建时间（判据见 `conflict_accepted_sort_key`）。"""
        now = timezone.now()
        order = self._shared_order()
        early_created = self._push_on(
            order,
            serial=SERIAL_EARLY,
            accepted_at=None,
            created_at=now - timedelta(hours=2),
        )
        # 较晚接单的是**未接入平台**那条：如果回退判据写错（把 None 当成「现在」），
        # 保留的就会变成它、被 0 撤的会变成代练通那条 ⇒ 本用例会看到一次 LevelOrderCancel。
        self._push_on(
            order,
            serial=SERIAL_LATE,
            platform=OTHER_A,
            accepted_at=now - timedelta(minutes=30),
            created_at=now - timedelta(hours=1),
        )

        self.assertIsNone(early_created.remote_accepted_at)
        self.assertEqual(
            push_module.conflict_accepted_sort_key(early_created)[0],
            early_created.created_at,
            "接单时间缺失 ⇒ 退化到推送创建时间",
        )

        self.assertEqual(self._run_section(), (0, 0))
        self.assertEqual(
            self.fake.forms_for("LevelOrderCancel"),
            [],
            "缺接单时间那条按创建时间更早 ⇒ 保留它（代练通那条不被撤）",
        )
        early_created.refresh_from_db()
        self.assertIsNone(early_created.conflict_zero_refund_at)

    def test_rows_with_a_manual_cancel_are_not_part_of_the_conflict(self):
        """`cancel_requested_at` 非空 = 人的决定（撤销后重发）⇒ ⛔ 不被系统 0 撤覆盖。"""
        order = self._shared_order()
        self._push_on(
            order,
            serial=SERIAL_EARLY,
            accepted_at=self._accepted(seconds=600),
            cancel_requested_at=self._accepted(seconds=120),
        )
        self._push_on(
            order,
            serial=SERIAL_LATE,
            platform=OTHER_A,
            accepted_at=self._accepted(seconds=60),
        )

        self.assertEqual(self._run_section(), (0, 0))
        self.assertEqual(self.fake.cancelled_serials(), [])
        self.assertFalse(
            DailiantongPush.objects.exclude(conflict_zero_refund_note="").exists()
        )

    def test_a_readback_showing_the_cancel_already_running_is_closed_without_a_write(self):
        order = self._shared_order()
        self._push_on(
            order,
            serial=SERIAL_EARLY,
            platform=OTHER_A,
            accepted_at=self._accepted(seconds=600),
        )
        self._push_on(order, serial=SERIAL_LATE, accepted_at=self._accepted(seconds=60))
        # 写前回读就说「已经在协商撤销中」⇒ 不该再发一次 `LevelOrderCancel`。
        self.fake.details[SERIAL_LATE] = {"Status": 12, "CancelStatus": 11}

        self.assertEqual(self._run_section(), (0, 0))

        self.assertEqual(self.fake.forms_for("LevelOrderCancel"), [])
        late = DailiantongPush.objects.get(serial_no=SERIAL_LATE)
        self.assertIsNotNone(
            late.conflict_zero_refund_at, "已经不需要撤 ⇒ 记收口（⛔ 不每轮重试）"
        )
        self.assertIn("撤销流程已经在走", late.conflict_zero_refund_note)


# ── ⭐ G3（审计）：撞单 0 撤的两个失败分支 ─────────────────────


class _CancelTransportErrorFake(_ConflictFake):
    """`LevelOrderCancel` 传输失败（超时 / 连接断）⇒ 「结果未知」，⛔ 不自动重试。"""

    def handler(self, request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qsl

        form = dict(parse_qsl(request.content.decode("utf-8")))
        if form.get("Action") == "LevelOrderCancel":
            self.calls.append(("LevelOrderCancel", form))
            raise httpx.ConnectError("synthetic connect error", request=request)
        return super().handler(request)


class ZeroRefundFailureBranchTests(_ConflictBase):
    """两个「需要人来看」的失败形态：结果未知 / 已提交但回读没核验到。

    重试安全性完全依赖写前回读闸（⛔ 不做写动作的自动重试）；这里钉的是：
    失败 ⇒ 有可读原因 + 有退避窗口 + 下一轮不再立刻打上游。
    """

    def _conflict_pair(self):
        # ⚠️ 同一 (订单, 平台) 只能有一条「在途且未申请撤销」的行（部分唯一索引）⇒
        # 保留的那条放别的平台（撞单本来就是跨平台的），被 0 撤的这条放代练通（唯一能撤）。
        order = self._shared_order()
        self._push_on(
            order, serial=SERIAL_EARLY, platform=OTHER_A,
            accepted_at=self._accepted(seconds=600),
        )
        late = self._push_on(
            order, serial=SERIAL_LATE,
            accepted_at=self._accepted(seconds=60),
        )
        self.fake.details[SERIAL_EARLY] = {"Status": 12, "CancelStatus": 10}
        self.fake.details[SERIAL_LATE] = {"Status": 12, "CancelStatus": 10}
        return late

    def test_a_transport_failure_is_unknown_and_backed_off(self):
        late = self._conflict_pair()
        self.fake = _CancelTransportErrorFake()

        handled, failed = self._run_section()

        self.assertEqual((handled, failed), (0, 1))
        late.refresh_from_db()
        self.assertEqual(late.conflict_zero_refund_attempts, 1)
        self.assertIsNotNone(late.conflict_zero_refund_next_at, "失败必须有退避窗口")
        self.assertIsNone(late.conflict_zero_refund_at, "失败⛔ 不许记成已收口")
        self.assertIn("结果未知", late.conflict_zero_refund_note)

        # 退避窗口内重跑 ⇒ 零上游调用（⛔ 每轮刷上游）。
        self.fake.calls.clear()
        self.assertEqual(self._run_section(), (0, 0))
        self.assertEqual(self.fake.calls, [])

    def test_a_submitted_but_unverified_cancel_is_reported_and_backed_off(self):
        late = self._conflict_pair()
        # 上游回了「成功」，但写后回读仍是 CancelStatus=10（撤销没登记上）。
        self.fake.detail_after_cancel = {"Status": 12, "CancelStatus": 10}

        handled, failed = self._run_section()

        self.assertEqual((handled, failed), (0, 1))
        late.refresh_from_db()
        self.assertEqual(late.conflict_zero_refund_attempts, 1)
        self.assertIsNotNone(late.conflict_zero_refund_next_at)
        self.assertIsNone(late.conflict_zero_refund_at)
        self.assertIn("没能核验到", late.conflict_zero_refund_note)


class StaleCancelRegressionTests(_ConflictBase):
    """⭐ H2（2026-10-28 审计）：撤销被远端取消 + 已有 NULL 在途行。

    回归序列：A 申请撤销（`cancel_requested_at` 非空 ⇒ 退出部分唯一索引）→ D8 重发建 B
    （在索引里）→ 远端把 A 的撤销取消（CancelStatus 回到 10）→ 回读 A 时若把标记清回 NULL，
    A 重新落进索引 ⇒ UPDATE 撞 `uniq_dailiantong_push_active`（A 永久停摆、页面永远
    「撤销中」、同单两张活单、0 撤兜底因过滤掉 A 而失效）。
    修法：有 NULL 在途行 ⇒ A 收 `closed` + 保留标记 + 写可读原因，并让 0 撤仍能看见 A。
    """

    def test_it_closes_instead_of_colliding_and_the_zero_refund_fallback_sees_both(self):
        order = self._shared_order()
        early = self._push_on(
            order, serial=SERIAL_EARLY,
            accepted_at=self._accepted(seconds=600),
            remote_cancel_status=10,
        )
        early.cancel_requested_at = timezone.now()
        early.save(update_fields=["cancel_requested_at", "updated_at"])
        late = self._push_on(
            order, serial=SERIAL_LATE,
            accepted_at=self._accepted(seconds=60),
            remote_cancel_status=10,
        )
        # 回读 A：Status=12 / CancelStatus=10（远端把撤销取消了，单还活着）。
        self.fake.details[SERIAL_EARLY] = {"Status": 12, "CancelStatus": 10}
        self.fake.details[SERIAL_LATE] = {"Status": 12, "CancelStatus": 10}

        with mock.patch(
            "apps.business.dailiantong_push._open_client",
            side_effect=self._client_factory,
        ):
            # 不抛 IntegrityError 是本条的第一判据（修复前这里会撞部分唯一索引）。
            push_module.refresh_push_status(early)

        early.refresh_from_db()
        late.refresh_from_db()
        self.assertEqual(early.state, DailiantongPush.State.CLOSED, "有在途兄弟行 ⇒ 本行收口")
        self.assertFalse(early.is_active)
        self.assertIsNotNone(
            early.cancel_requested_at,
            "⛔ 不硬清标（清回 NULL 会与在途行撞部分唯一索引）",
        )
        self.assertIn("在途推送", early.remote_note)
        self.assertEqual(late.state, DailiantongPush.State.SENT)
        self.assertIsNone(late.cancel_requested_at)

        # ⭐ 两张活单都能被 0 撤兜底看见：保留最早接单的 A，0 撤较晚的 B。
        handled, failed = self._run_section()
        self.assertEqual((handled, failed), (1, 0))
        self.assertEqual(self.fake.cancelled_serials(), [SERIAL_LATE])
        late.refresh_from_db()
        self.assertIsNotNone(late.conflict_zero_refund_at)


class SyncSectionWiringTests(_ConflictBase):
    """⭐ 「并进 `dailiantong_sync` 每轮」：⛔ 不新建 systemd 单元。"""

    def test_every_round_calls_the_conflict_section(self):
        with mock.patch.object(
            sync_module, "zero_refund_conflicting_pushes", return_value=(2, 1)
        ) as patched:
            out, err = self._run_once()

        self.assertEqual(err, "")
        self.assertGreaterEqual(patched.call_count, 1, "每轮必须调用撞单 0 撤段")
        self.assertIn("撞单0撤 2", out)
        self.assertIn("撞单0撤失败 1", out)

    def test_a_section_failure_is_isolated_from_other_sections(self):
        """撞单段整段炸了 ⇒ 只计「段级意外」（`--once` 非零退出），⛔ 不拖垮其它段。"""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            sync_module,
            "zero_refund_conflicting_pushes",
            side_effect=RuntimeError("boom"),
        ), mock.patch(
            "apps.business.dailiantong_push._open_client",
            side_effect=self._client_factory,
        ):
            with self.assertRaises(CommandError):
                call_command("dailiantong_sync", "--once", stdout=out, stderr=err)

        self.assertIn("轮询 0", out.getvalue(), "撞单段炸了，汇总行照旧写出来")

    def test_a_full_round_zero_refunds_through_the_command(self):
        order = self._shared_order()
        self._push_on(
            order,
            serial=SERIAL_EARLY,
            platform=OTHER_A,
            accepted_at=self._accepted(seconds=600),
        )
        self._push_on(order, serial=SERIAL_LATE, accepted_at=self._accepted(seconds=60))

        out, err = self._run_once()

        self.assertEqual(err, "")
        self.assertEqual(self.fake.cancelled_serials(), [SERIAL_LATE])
        self.assertIn("撞单0撤 1", out)
        self.assertIn("未接入平台跳过 1", out)
