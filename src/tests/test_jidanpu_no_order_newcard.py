"""⭐ D-0362（2026-11-02）：鸡蛋铺「明确查无」终态化 + 直接新建卡——防循环保护网。

被钉住的契约（计划 `docs/plans/2026-11-02-jidanpu-verbatim-noorder-newcard.md` §一）：

- **首遇逐字查无 ⇒ 终态行 + 既有全新建卡路**（撤回 / 非撤回两形态的正向用例已在
  `tests.test_jidanpu_member_consume.ThrowPrepareRecreateTests` ③⑥ 反转落位，本模块不重复；
  本模块钉的是**新引入的保护面**）；
- **防循环主钉**：同一（订单，绑定）自动新建**终身上限 1 次**——已自动新建过、回读再遇
  逐字查无 ⇒ 「转人工」终态行（`member.no_order_handoff`）+ 终态跳过，⛔ 绝不二次新建、
  ⛔ 对账不再重排（`reconcile` 的 `handoff_refs` 闸）；
- **含糊失败维持现状**（口径③回归钉）：超时 / 5xx / 解析不出 / 验签失败 = `ApiError`
  ⇒ 照旧 `_retry` + `THROW_PREPARE_UNREADABLE_REASON` 前缀（熔断燃料族，20 条即挂起）；
- **挂起期共存**（D-0361 同一把闸）：挂起期遇逐字查无 ⇒ 挂起照旧拦截、⛔ 不放行新建、
  终态化照记（事件行按 `NO_ORDER_SUSPENDED_REASON` 收敛 `skipped`，不喂燃料、不写终态行、
  ⛔ 不消耗上限）；
- **幂等 / 回环防重原样穿过**：新建走既有 `ensure_remote`（地址级去重 / 来源号认领 /
  链路 binding+order 唯一），本线 ⛔ 不绕过任何既有闸；
- **自愈**：「转人工」的判定永远打在**新鲜的逐字查无**上——人工在对方店里把卡补回来时，
  prepare 读到卡、流进既有判定腿，⛔ 不落转人工行。

## ⛔ 变异自证（先红后绿，`AGENTS.md` §5-G 追加①②③；2026-11-02 本机实测留档）

**变异一（防循环主钉的源码变异）**：把 `_consume_launch` recreate 支里的
`if _auto_recreate_already_used(event, binding):` 判据改成 `if False:`（= 上限判据被删、
永远当作没用过）⇒ `AntiLoopTests.test_second_no_order_hands_off_and_never_recreates`
**必须红**（第二次查无也放行新建：拆锚重录走了既有录单路）。变异态实测（2026-11-02
本机）：`FAILED (failures=1)`，红在 `AssertionError: 'failed' != 'skipped'`——放行的那次
重录 `add_order` 落了第二张卡、回读拿号失败收 `failed`（红本身就是「变异已应用」的自证：
第二次放行的行为只可能经被删的那道闸改变）；**逐字编辑还原**（⛔ 未用
`git checkout --`），还原后 `git diff` 无本文件残留、主钉+§11 类 30 条全绿。

**变异二（对账不捞闸的源码变异）**：把 `reconcile` 里 `if order.order_no in handoff_refs:`
改成 `if False:`（= 不捞闸被删）⇒ `ReconcileParkingTests.test_a_handoff_parked_order_is_not_requeued`
**必须红**（已转人工的单又被排进 `to_launch`）。变异态实测（本机）：
`FAILED (failures=1)`，红在 `to_launch` 元组差（恰一条本不该排的 ReconcileItem）；
逐字还原后绿。

**在套件内的结构钉**（与源码变异互为两路证据）：
`test_the_cap_judge_is_consulted_at_the_recreate_branch` 把判据桩成「永远没用过」⇒
第二次查无必须放行新建——调用点被删 / 被挪时本用例红（桩不生效、结局仍是 skipped）。

§11 当年的变异钉（删 `if rearmed and withdrawn:` ⇒ ⑥红）已随 D-0362 反转**失效**，
见 `tests.test_jidanpu_member_consume.ThrowPrepareRecreateTests` docstring 的反转登记。

⚠️ 断言一律打**数据库行**与**桩收到的调用序列**（⛔ 不断言 logger）；夹具一律合成值
（E-0077）；⛔ 零真实网络。桩复用 `tests.test_jidanpu_member_consume` 那一份
（E-0029：桩打在被查找的模块 `apps.business.jidanpu` 上，两个模块共用同一个替身）。
"""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone

from jidanpu.errors import ApiError

from apps.business import jidanpu_common, jidanpu_member
from apps.business.models import (
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
)

from tests.test_jidanpu_member_consume import (
    CLIENT,
    JID,
    JID_2,
    ORDER_NO,
    _Base,
    _stub,
    _Upstream,
)

#: 第三张卡的合成远端单号（本模块自用；consume 模块只有 JID / JID_2）。
JID_3 = "JDP-00000000000000002003"

#: ⭐ E-0291：kind 从代码枚举、字面值钉死（⛔ 改名会静默换掉语义与时间线人话表）。
KIND_NO_ORDER_RECREATED_LITERAL = "member.no_order_recreated"
KIND_NO_ORDER_HANDOFF_LITERAL = "member.no_order_handoff"
#: 终态行 / 挂起期人话的**字面值**（⛔ 不从常量派生，E-0024）。
HANDOFF_REASON_LITERAL = "已自动新建仍查无，转人工处置"
RECREATED_REASON_LITERAL = "对端明确查无此单，按拍板转新建（自动新建，每单限一次）"
SUSPENDED_REASON_LITERAL = (
    "对端明确查无此单，但这家店的发单仍在挂起期（回读故障未复位）："
    "按挂起口径拦截、不自动新建；挂起解除后下一轮按查无新口径处置"
)
#: 含糊失败拒抛循环那句的**前缀**字面值（回归钉：含糊失败必须落在这个族里 = 计燃料）。
UNREADABLE_PREFIX_LITERAL = "抛单前回读对方那张单的状态失败"
#: 转人工档的人话**不允许**撞上回读失败族的前缀（skip 行不进燃料，撞族会让排障误判）。
FAMILY_PREFIXES = tuple(jidanpu_member.LAUNCH_READBACK_FAILURE_REASONS)


def _note_rows(kind, order_no):
    return JidanpuSyncEvent.objects.filter(
        kind=kind, target_type="order", target_ref=order_no
    )


class NoOrderKindContractTests(_Base):
    """kind / 人话的字面量契约（E-0291 枚举 + E-0024 钉值）。"""

    def test_kind_literals_are_the_wire_values(self):
        self.assertEqual(
            jidanpu_member.KIND_NO_ORDER_RECREATED, KIND_NO_ORDER_RECREATED_LITERAL
        )
        self.assertEqual(
            jidanpu_member.KIND_NO_ORDER_HANDOFF, KIND_NO_ORDER_HANDOFF_LITERAL
        )

    def test_terminal_rows_are_not_outbox_kinds(self):
        """两条终态行只是台账痕迹：⛔ 不进 `OUTBOX_KINDS`（`run_outbox` 永不取它）。"""
        self.assertNotIn(
            jidanpu_member.KIND_NO_ORDER_RECREATED, jidanpu_member.OUTBOX_KINDS
        )
        self.assertNotIn(
            jidanpu_member.KIND_NO_ORDER_HANDOFF, jidanpu_member.OUTBOX_KINDS
        )

    def test_reason_literals(self):
        self.assertEqual(jidanpu_member.NO_ORDER_HANDOFF_REASON, HANDOFF_REASON_LITERAL)
        self.assertEqual(
            jidanpu_member.NO_ORDER_RECREATED_REASON, RECREATED_REASON_LITERAL
        )
        self.assertEqual(
            jidanpu_member.NO_ORDER_SUSPENDED_REASON, SUSPENDED_REASON_LITERAL
        )

    def test_suspended_reason_does_not_collide_with_the_breaker_fuel_family(self):
        """挂起期那句人话 ⛔ 不许以回读失败族任何一句开头（撞族会让燃料统计/排障误判）。"""
        self.assertFalse(SUSPENDED_REASON_LITERAL.startswith(FAMILY_PREFIXES))
        self.assertFalse(HANDOFF_REASON_LITERAL.startswith(FAMILY_PREFIXES))


class AntiLoopTests(_Base):
    """⭐⭐ 防循环主钉：每（订单，绑定）自动新建**终身上限 1 次**，再遇查无 ⇒ 转人工。"""

    def _withdrawn_link(self):
        """链接行 + 「退成过」标记（首遇自动新建的现场形态，照 consume ③）。"""
        link = self._link(in_hall=False)
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            withdrawn_at=timezone.now()
        )
        return link

    def _first_auto_recreate(self):
        """首遇：撤回过的单 + 自动事件 + 查无 ⇒ 新建（返回消费后的台账行）。

        形态照 `ThrowPrepareRecreateTests` 反转后的③（撤回单自动首遇）；这里只搭链条，
        断言本体在下面的用例里。
        """
        self._withdrawn_link()
        up = _Upstream(
            remote_id=JID_2,
            lookup={ORDER_NO: {"orderId": JID_2, "orderIdView": ORDER_NO}},
        )
        event = self._event(jidanpu_member.KIND_LAUNCH)
        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)
        self.assertEqual(outcome, "done", "链条前提：首遇自动新建成功")
        return event

    def test_second_no_order_hands_off_and_never_recreates(self):
        """⭐⭐ 主钉（变异一的目标）：已自动新建过、再遇逐字查无 ⇒ 转人工，⛔ 绝不二次新建。

        循环数学（拍板口径）：每（订单，绑定）自动新建上界 1 张 + 转人工终态行
        每窗 1 条 ⇒ 「回读查无 → 新建 → 又查无 → 又新建」死循环在此断根。
        """
        self._first_auto_recreate()
        # 第二轮：对账再排一条 launch（自动、无 rearmed），桩回「逐字查无」（缺省 None）。
        up = _Upstream()
        event2 = self._event(jidanpu_member.KIND_LAUNCH)
        with _stub(up):
            outcome = jidanpu_member.consume_event(event2, client=CLIENT)
        self.assertEqual(outcome, "skipped")
        # ⛔ 零次录单 / 抛单：前置回读（prepare 真读了 JID_2）之外一次上游写都没有。
        self.assertEqual(up.names(), ["fetch_order_by_key1"])
        event2.refresh_from_db()
        self.assertEqual(event2.status, "skipped")
        self.assertEqual(event2.reason, HANDOFF_REASON_LITERAL)
        # 转人工终态行恰一条、带绑定与被查的旧远端单号。
        notes = list(_note_rows(KIND_NO_ORDER_HANDOFF_LITERAL, ORDER_NO))
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].status, "skipped")
        self.assertEqual(notes[0].payload["member_binding_id"], self.binding.pk)
        self.assertEqual(notes[0].payload["jidanpu_order_id"], JID_2)
        # ⛔ 自动新建账本仍只有首遇那一条（上限 = 1）。
        self.assertEqual(
            _note_rows(KIND_NO_ORDER_RECREATED_LITERAL, ORDER_NO).count(), 1
        )
        # ⛔ 不双卡：链路仍只有一行（唯一约束下的那一张卡）。
        self.assertEqual(
            JidanpuMemberOrderLink.objects.filter(
                binding=self.binding, order=self.order
            ).count(),
            1,
        )

    def test_a_third_encounter_within_the_window_writes_no_second_note(self):
        """第三轮仍在窗内 ⇒ 照旧转人工，转人工行**不灌水**（每窗恰一条）。"""
        self._first_auto_recreate()
        for _ in range(2):
            up = _Upstream()
            event = self._event(jidanpu_member.KIND_LAUNCH)
            with _stub(up):
                self.assertEqual(
                    jidanpu_member.consume_event(event, client=CLIENT), "skipped"
                )
        self.assertEqual(_note_rows(KIND_NO_ORDER_HANDOFF_LITERAL, ORDER_NO).count(), 1)
        self.assertEqual(
            _note_rows(KIND_NO_ORDER_RECREATED_LITERAL, ORDER_NO).count(), 1,
            "上限账本不随重复触发增长",
        )
        self.assertEqual(
            JidanpuMemberOrderLink.objects.filter(
                binding=self.binding, order=self.order
            ).count(),
            1,
        )

    def test_the_cap_judge_is_consulted_at_the_recreate_branch(self):
        """⭐ 结构钉：recreate 支**真的咨询**「上限已用」判据（防调用点被挪走/删除）。

        把判据桩成「永远没用过」⇒ 第二次查无必须放行新建——若实现删掉了调用点 /
        把它挪到跳过之后，本用例红（那种实现下桩不生效、结局仍是 skipped）。
        与 docstring 的**源码变异一**互为两路证据（源码变异证「判据在决定结局」，
        本用例证「判据在 recreate 支的决策点上」）。
        """
        self._first_auto_recreate()
        up = _Upstream(
            remote_id=JID_3,
            lookup={ORDER_NO: {"orderId": JID_3, "orderIdView": ORDER_NO}},
        )
        event2 = self._event(jidanpu_member.KIND_LAUNCH)
        with (
            mock.patch.object(
                jidanpu_member,
                "_auto_recreate_already_used",
                side_effect=lambda event, binding: False,
            ),
            _stub(up),
        ):
            self.assertEqual(
                jidanpu_member.consume_event(event2, client=CLIENT), "done"
            )
        self.assertIn("add_order", up.names())

    def test_the_cap_judge_is_a_lifetime_per_order_and_binding_query(self):
        """「已自动新建过」判据本体：按（订单，绑定）终身判，⛔ 不串单、⛔ 不串绑定。"""
        other_user = User.objects.create_user(username="member-c", password="pw-a")
        other_binding = JidanpuMemberBinding.objects.create(
            user=other_user,
            jidanpu_base_url="https://jidanpu-c.test.invalid",
            account="acc-c",
            password="pw-a",
            enabled=True,
        )
        self.assertFalse(
            jidanpu_member._auto_recreate_already_used(
                self._event(jidanpu_member.KIND_LAUNCH), self.binding
            )
        )
        event = self._event(jidanpu_member.KIND_LAUNCH)
        jidanpu_member._note_no_order_recreated(event, self.binding, JID)
        self.assertTrue(jidanpu_member._auto_recreate_already_used(event, self.binding))
        # 同单**另一绑定**没建过 ⇒ False（⛔ 不串绑定）；另一单同绑定 ⇒ False（⛔ 不串单）。
        self.assertFalse(
            jidanpu_member._auto_recreate_already_used(event, other_binding)
        )
        other_order = Order.objects.create(
            game=self.game,
            order_no=ORDER_NO[:-1] + "9",
            status=OrderStatus.WAITING_GRAB,
            hall_price="12.34",
            total_price="50.00",
            project_text="合成项目乙",
        )
        other_order_event = self._event(jidanpu_member.KIND_LAUNCH, order=other_order)
        self.assertFalse(
            jidanpu_member._auto_recreate_already_used(other_order_event, self.binding)
        )

    def test_manual_rethrow_after_handoff_still_recreates(self):
        """转人工之后**人工重抛**仍能新建（§11 手动路径不限次；上限只管自动）。"""
        self._first_auto_recreate()
        up = _Upstream(
            remote_id=JID_3,
            lookup={ORDER_NO: {"orderId": JID_3, "orderIdView": ORDER_NO}},
        )
        event2 = self._event(jidanpu_member.KIND_LAUNCH)
        # 人工重抛的现场：复用旧行时 payload 带 `rearmed_at`（此刻 > 撤回时刻 ⇒ 人工）。
        payload = dict(event2.payload or {})
        payload["rearmed_at"] = timezone.now().isoformat()
        JidanpuSyncEvent.objects.filter(pk=event2.pk).update(payload=payload)
        event2.refresh_from_db()
        with _stub(up):
            self.assertEqual(
                jidanpu_member.consume_event(event2, client=CLIENT), "done"
            )
        self.assertIn("add_order", up.names())
        self.assertEqual(dict(up.calls)["throw_order"]["remote_order_id"], JID_3)
        # ⛔ 人工动作不占自动上限：账本行仍只有首遇那一条。
        self.assertEqual(
            _note_rows(KIND_NO_ORDER_RECREATED_LITERAL, ORDER_NO).count(), 1
        )

    def test_a_card_restored_by_the_store_self_heals_instead_of_handoff(self):
        """自愈钉：人工在对方店里把卡补回来 ⇒ prepare 读到卡、走既有判定腿，⛔ 不转人工。

        「转人工」的判定打在**新鲜的逐字查无**上——卡回来了就不再是查无，
        那条线照 `already` 幂等腿收口（这是窗口化对账重排的设计目的：每窗重校一次）。
        """
        self._first_auto_recreate()
        up = _Upstream(
            lookup={JID_2: {"orderId": JID_2, "orderSta": "未分配", "isThrowOrder": "Y"}},
        )
        event2 = self._event(jidanpu_member.KIND_LAUNCH)
        with _stub(up):
            self.assertEqual(
                jidanpu_member.consume_event(event2, client=CLIENT), "done"
            )
        self.assertEqual(up.names(), ["fetch_order_by_key1"], "卡在 ⇒ 幂等腿，零写")
        self.assertFalse(
            _note_rows(KIND_NO_ORDER_HANDOFF_LITERAL, ORDER_NO).exists(),
            "卡回来了 ⛔ 不许落转人工行",
        )


class AmbiguousFailureRegressionTests(_Base):
    """口径③回归钉：含糊失败（超时 / 5xx / 解析不出 / 验签失败 = `ApiError`）维持现状。"""

    def test_an_api_error_still_retries_with_the_unreadable_reason(self):
        """`ApiError` ⇒ `failed` + 不可读原因前缀 + 计一次尝试（零新建、零终态行）。"""
        self._link(in_hall=False)
        up = _Upstream(fetch_error=ApiError("网关超时（合成）", code=0))
        event = self._event(jidanpu_member.KIND_LAUNCH)
        with _stub(up):
            self.assertEqual(
                jidanpu_member.consume_event(event, client=CLIENT), "failed"
            )
        self.assertEqual(up.names(), ["fetch_order_by_key1"])
        self.assertNotIn("add_order", up.names())
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertTrue(event.reason.startswith(UNREADABLE_PREFIX_LITERAL))
        self.assertFalse(
            _note_rows(KIND_NO_ORDER_RECREATED_LITERAL, ORDER_NO).exists(),
            "含糊失败 ⛔ 不写自动新建终态行（判不出 = 按坏了处理）",
        )
        self.assertFalse(_note_rows(KIND_NO_ORDER_HANDOFF_LITERAL, ORDER_NO).exists())

    def test_the_unreadable_reason_still_feeds_the_breaker(self):
        """含糊失败仍是熔断燃料：20 条同因 `failed` 行 ⇒ 挂起判定为真（口径④零改动）。

        ⚠️ 种行 reason = **全句常量 + 上游原话后缀**（真实行的形状；族判据
        `startswith` 认的是全句，只写前缀半句会以「夹具错」红、不产出结论）。
        """
        for i in range(jidanpu_member.LAUNCH_READBACK_BREAKER_THRESHOLD):
            jidanpu_common.write_event(
                direction="out", kind=jidanpu_member.KIND_LAUNCH, status="failed",
                target_type="order", target_ref=f"2026110200000000{i:04d}",
                reason=jidanpu_member.THROW_PREPARE_UNREADABLE_REASON + f"（合成原话 {i}）",
                payload={"member_binding_id": self.binding.pk},
            )
        blocked, detail = jidanpu_member._launch_readback_breaker_state(
            self.binding, now=timezone.now()
        )
        self.assertTrue(blocked)
        self.assertIn("回读失败", detail)


class SuspendedCoExistenceTests(_Base):
    """⭐ D-0361 共存：挂起期遇逐字查无 ⇒ 挂起照旧拦截、⛔ 不放行新建、终态化照记。"""

    def _suspend(self):
        """把绑定推进挂起态（20 条回读失败族 `failed` launch 行）+ 断言前提成立。

        ⚠️ 种行 reason = **全句常量 + 后缀**（族判据 `startswith` 认全句，见
        `AmbiguousFailureRegressionTests.test_the_unreadable_reason_still_feeds_the_breaker`）。
        """
        for i in range(jidanpu_member.LAUNCH_READBACK_BREAKER_THRESHOLD):
            jidanpu_common.write_event(
                direction="out", kind=jidanpu_member.KIND_LAUNCH, status="failed",
                target_type="order", target_ref=f"2026110200000001{i:04d}",
                reason=jidanpu_member.THROW_PREPARE_UNREADABLE_REASON + f"（合成原话 {i}）",
                payload={"member_binding_id": self.binding.pk},
            )
        blocked, _detail = jidanpu_member._launch_readback_breaker_state(
            self.binding, now=timezone.now()
        )
        self.assertTrue(blocked, "夹具前提：绑定确在挂起态")

    def test_a_suspended_store_blocks_the_new_card_and_ledgers_the_fact(self):
        """挂起期（probe 放行 / 直接消费的形状）遇查无 ⇒ 终态跳过、零新建、零账本行。"""
        self._suspend()
        self._link(in_hall=False)
        up = _Upstream()  # 桩缺省 = 逐字查无
        event = self._event(jidanpu_member.KIND_LAUNCH)
        with _stub(up):
            self.assertEqual(
                jidanpu_member.consume_event(event, client=CLIENT), "skipped"
            )
        self.assertEqual(up.names(), ["fetch_order_by_key1"], "⛔ 零新建：只有前置回读")
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.reason, SUSPENDED_REASON_LITERAL)
        self.assertFalse(
            _note_rows(KIND_NO_ORDER_RECREATED_LITERAL, ORDER_NO).exists(),
            "挂起期拦截不消耗自动新建上限",
        )
        self.assertFalse(_note_rows(KIND_NO_ORDER_HANDOFF_LITERAL, ORDER_NO).exists())

    def test_a_manual_rethrow_is_also_blocked_while_suspended(self):
        """⛔ 挂起期不放行新建——**人工旁路也拦**（交付口径明文；解除后按新口径处置）。"""
        self._suspend()
        self._link(in_hall=False)
        up = _Upstream()
        event = self._event(jidanpu_member.KIND_LAUNCH)
        payload = dict(event.payload or {})
        payload["rearmed_at"] = timezone.now().isoformat()
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(payload=payload)
        event.refresh_from_db()
        with _stub(up):
            self.assertEqual(
                jidanpu_member.consume_event(event, client=CLIENT), "skipped"
            )
        self.assertNotIn("add_order", up.names())
        event.refresh_from_db()
        self.assertEqual(event.reason, SUSPENDED_REASON_LITERAL)

    def test_suspension_clears_and_the_next_round_follows_the_new_rule(self):
        """挂起解除（done 行进「最新 20 条」）⇒ 同一形状的查无按新口径处置（自动首遇新建）。"""
        self._suspend()
        jidanpu_common.write_event(
            direction="out", kind=jidanpu_member.KIND_LAUNCH, status="done",
            target_type="order", target_ref="2026110200000002ffff",
            reason="", payload={"member_binding_id": self.binding.pk},
        )
        blocked, _detail = jidanpu_member._launch_readback_breaker_state(
            self.binding, now=timezone.now()
        )
        self.assertFalse(blocked, "夹具前提：done 行让挂起复位")
        self._link(in_hall=False)
        up = _Upstream(
            remote_id=JID_2,
            lookup={ORDER_NO: {"orderId": JID_2, "orderIdView": ORDER_NO}},
        )
        event = self._event(jidanpu_member.KIND_LAUNCH)
        with _stub(up):
            self.assertEqual(
                jidanpu_member.consume_event(event, client=CLIENT), "done"
            )
        self.assertIn("add_order", up.names())
        self.assertEqual(
            _note_rows(KIND_NO_ORDER_RECREATED_LITERAL, ORDER_NO).count(), 1
        )


class ReconcileParkingTests(_Base):
    """⭐ 口径②「不再重排」的对账半边：已转人工的（单，绑定）⇒ 对账不再排 launch。"""

    def setUp(self):
        super().setUp()
        # ⭐ 对账差集按「绑定用户勾过的游戏」算候选（`games_of`）⇒ 不勾游戏这一张单
        #    的 shape 是 absent、会进退单支而不是 launch 支。这里勾上 = shape hall。
        from apps.accounts.models import StaffProfile

        profile = StaffProfile.objects.create(user=self.user)
        profile.games.set([self.game])

    def _park(self, *, note_age=None):
        """夹具：在厅期望 + 链路不在厅 + 一条转人工终态行（`note_age` 可把行调老）。

        返回 `(order, reconcile 用的 now)`；`now` 在**落行之后**取（否则行比窗新、
        「过期」那一档会以错误的原因变红）。
        """
        self._link(in_hall=False)
        event = self._event(jidanpu_member.KIND_LAUNCH)
        jidanpu_member._note_no_order_handoff_once(
            event, self.binding, JID, now=timezone.now()
        )
        if note_age is not None:
            # auto_now_add 不可直改 ⇒ update 绕过（测试夹具专用手段）。
            JidanpuSyncEvent.objects.filter(
                kind=KIND_NO_ORDER_HANDOFF_LITERAL, target_ref=ORDER_NO
            ).update(created_at=timezone.now() - note_age)
        return self.order, timezone.now()

    def test_a_handoff_parked_order_is_not_requeued(self):
        """⭐ 主钉（变异二的目标）：窗内转人工行存在 ⇒ `to_launch` 不含这一单。

        终态 `skipped` 挡得住重试、⛔ 挡不住重排——不闸的话每 180 秒重排一条注定
        跳过的 launch（台账灌水）。⭐ 只闸推单：本形状下 `to_withdraw` 本就不该有行
        （shape=hall 不进退单支），一并钉住「清理档没被误伤」。
        """
        order, now = self._park()
        result = jidanpu_member.reconcile(self.binding, now=now)
        self.assertEqual(result.to_launch, ())
        self.assertEqual(result.to_withdraw, ())

    def test_a_binding_without_a_handoff_note_still_gets_queued(self):
        """反对照：同一形状、**无**转人工行 ⇒ 照旧排（证明闸就是那条行，不是别的东西）。"""
        order, now = self._park()
        _note_rows(KIND_NO_ORDER_HANDOFF_LITERAL, ORDER_NO).delete()
        result = jidanpu_member.reconcile(self.binding, now=now)
        self.assertEqual([item.order.pk for item in result.to_launch], [order.pk])

    def test_an_expired_note_unparks_until_the_next_handoff_row(self):
        """窗口过期 ⇒ 对账恢复排（下一轮消费再遇查无会重写新窗行 = 有界的重申循环）。"""
        order, now = self._park(
            note_age=jidanpu_member.NO_ORDER_HANDOFF_NOTE_WINDOW + timedelta(minutes=1)
        )
        result = jidanpu_member.reconcile(self.binding, now=now)
        self.assertEqual([item.order.pk for item in result.to_launch], [order.pk])

    def test_the_gate_reads_the_binding_and_the_kind(self):
        """判据钉在（**同绑定** + **转人工 kind**）上：别绑定的行 / 别 kind 的行都不闸。"""
        order = self.order
        self._link(in_hall=False)
        other_user = User.objects.create_user(username="member-d", password="pw-a")
        other_binding = JidanpuMemberBinding.objects.create(
            user=other_user,
            jidanpu_base_url="https://jidanpu-d.test.invalid",
            account="acc-d",
            password="pw-a",
            enabled=True,
        )
        # 同单、**另一绑定**的转人工行 ⇒ 不闸本绑定。
        event = self._event(jidanpu_member.KIND_LAUNCH, binding=other_binding)
        jidanpu_member._note_no_order_handoff_once(
            event, other_binding, JID, now=timezone.now()
        )
        # 同单、本绑定、**别 kind**（挂起挡单行，D-0361）⇒ 也不闸。
        jidanpu_common.write_event(
            direction="out", kind=jidanpu_member.KIND_LAUNCH_BLOCKED,
            status="skipped", target_type="order", target_ref=ORDER_NO,
            reason="（合成）挂起挡单", payload={"member_binding_id": self.binding.pk},
        )
        result = jidanpu_member.reconcile(self.binding, now=timezone.now())
        self.assertEqual([item.order.pk for item in result.to_launch], [order.pk])


class HandoffSkipFingerprintExemptionTests(_Base):
    """⭐ D-0370 护栏豁免钉：转人工 skip 的终态行**不带状态指纹**（D-0362 每窗重申循环保留）。

    转人工支设计的循环 = 窗过期 ⇒ 对账重排一条 launch ⇒ 消费再遇逐字查无 ⇒ 转人工 skip
    ⇒（下一窗）再重排。终态停重排闸若让这条终态行带上指纹，「指纹不变 ⇒ 永闸」会把循环
    永久掐死（恢复只剩人工重抛）⇒ 落终态时抹掉指纹（`_strip_requeue_guard_fp`）：护栏对
    无指纹行 fail-open，循环的有界性由 D-0362 自己的窗机制保证。
    """

    def _withdrawn_link(self):
        """链接行 + 「退成过」标记（照 `AntiLoopTests` 同形状，本类自备）。"""
        link = self._link(in_hall=False)
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            withdrawn_at=timezone.now()
        )
        return link

    def _first_auto_recreate(self):
        """首遇自动新建成功（返回消费后的台账行；照 `AntiLoopTests` 同形状）。"""
        self._withdrawn_link()
        up = _Upstream(
            remote_id=JID_2,
            lookup={ORDER_NO: {"orderId": JID_2, "orderIdView": ORDER_NO}},
        )
        event = self._event(jidanpu_member.KIND_LAUNCH)
        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)
        self.assertEqual(outcome, "done", "链条前提：首遇自动新建成功")
        return event

    def test_the_handoff_skipped_row_carries_no_fingerprint(self):
        """转人工 skip 后，事件行 payload 里没有 `requeue_guard_fp`（护栏 fail-open 的前提）。"""
        self._first_auto_recreate()
        event2 = self._event(jidanpu_member.KIND_LAUNCH)  # 自动路径排队 ⇒ 构造物：带指纹
        self.assertIn(jidanpu_member.REQUEUE_GUARD_FP_KEY, event2.payload)
        with _stub(_Upstream()):
            self.assertEqual(
                jidanpu_member.consume_event(event2, client=CLIENT), "skipped"
            )
        event2.refresh_from_db()
        self.assertEqual(event2.reason, HANDOFF_REASON_LITERAL, "构造物核对：确实走的转人工支")
        self.assertNotIn(
            jidanpu_member.REQUEUE_GUARD_FP_KEY, event2.payload,
            "转人工终态行⛔ 不带指纹（护栏对它豁免）",
        )

    def test_requeue_after_handoff_skip_is_not_blocked(self):
        """窗循环不被闸：转人工 skip 行在 ⇒ 自动路径照旧排得出新行（fail-open）。"""
        self._first_auto_recreate()
        event2 = self._event(jidanpu_member.KIND_LAUNCH)
        with _stub(_Upstream()):
            self.assertEqual(
                jidanpu_member.consume_event(event2, client=CLIENT), "skipped"
            )
        event2.refresh_from_db()
        again = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        self.assertNotEqual(
            again.pk, event2.pk,
            "转人工行无指纹 ⇒ 闸放行（D-0362 每窗重申循环保留）",
        )
        self.assertEqual(again.status, "pending")


class IdempotentSecondTriggerTests(_Base):
    """新建幂等（既有防重闸原样穿过）：同单二次触发不双卡。"""

    def _recreate_once(self):
        """首遇自动新建成功（撤回单 + 查无 ⇒ 新卡 JID_2、链路在厅）。"""
        link = self._link(in_hall=False)
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            withdrawn_at=timezone.now()
        )
        up1 = _Upstream(
            remote_id=JID_2,
            lookup={ORDER_NO: {"orderId": JID_2, "orderIdView": ORDER_NO}},
        )
        event1 = self._event(jidanpu_member.KIND_LAUNCH)
        with _stub(up1):
            self.assertEqual(
                jidanpu_member.consume_event(event1, client=CLIENT), "done"
            )

    def test_a_second_trigger_with_the_card_present_does_not_double_card(self):
        """首遇新建成功后再触发 ⇒ prepare 读到**新**卡在厅 ⇒ `already` 幂等腿，
        零录单、链路仍恰一行（binding+order 唯一），账本行不增。"""
        self._recreate_once()
        # 第二次触发：链路在（新卡）、在厅 ⇒ prepare 读到卡 ⇒ 幂等收口，⛔ 不再录单。
        up2 = _Upstream(
            lookup={JID_2: {"orderId": JID_2, "orderSta": "未分配", "isThrowOrder": "Y"}},
        )
        event2 = self._event(jidanpu_member.KIND_LAUNCH)
        with _stub(up2):
            self.assertEqual(
                jidanpu_member.consume_event(event2, client=CLIENT), "done"
            )
        self.assertNotIn("add_order", up2.names())
        self.assertEqual(
            JidanpuMemberOrderLink.objects.filter(
                binding=self.binding, order=self.order
            ).count(),
            1,
        )
        self.assertEqual(
            _note_rows(KIND_NO_ORDER_RECREATED_LITERAL, ORDER_NO).count(), 1
        )

    def test_a_card_landed_by_an_interrupted_recreate_is_claimed_not_duplicated(self):
        """⚠️ 既有认领闸穿过 recreate 现场：录单落地而我方中断（回读没拿到号、链接缺席）
        ⇒ 下一轮**按来源号认领**那张卡（⛔ 不录第二张）——来源号口径的防重闸原样生效。"""
        link = self._link(in_hall=False)
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            withdrawn_at=timezone.now()
        )
        # 首遇：查无放行 → add_order 成功（卡落地）→ 回读拿不到号（None）⇒ failed、链接缺席。
        up1 = _Upstream()  # 缺省：prepare 旧号查无；回读（按来源号）也查无 = 中断现场
        event1 = self._event(jidanpu_member.KIND_LAUNCH)
        with _stub(up1):
            self.assertEqual(
                jidanpu_member.consume_event(event1, client=CLIENT), "failed"
            )
        self.assertIn("add_order", up1.names(), "夹具前提：卡已录进对方（桩侧 _recorded）")
        # 第二轮（新事件、新 payload）：来源号预读**认领**到那张卡 ⇒ ⛔ 零次 add_order。
        up2 = _Upstream(
            remote_exists=True,
            lookup={
                ORDER_NO: {"orderId": JID_2, "orderIdView": ORDER_NO,
                           "isThrowOrder": "N"},
                JID_2: {"orderId": JID_2, "isThrowOrder": "Y"},
            },
        )
        event2 = self._event(jidanpu_member.KIND_LAUNCH)
        with _stub(up2):
            self.assertEqual(
                jidanpu_member.consume_event(event2, client=CLIENT), "done"
            )
        self.assertNotIn("add_order", up2.names(), "认领了已落地的卡，⛔ 不录第二张")
        self.assertEqual(
            JidanpuMemberOrderLink.objects.filter(
                binding=self.binding, order=self.order
            ).count(),
            1,
        )
