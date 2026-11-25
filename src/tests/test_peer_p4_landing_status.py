"""上下游 P4 Task 1：**接手类事件落地即「未分配」**（业务方 2026-09-27 裁定）。

本期状态口径（计划 Global Constraints 5）：

- `order.assign` / `order.grab`（接手类）⇒ 对方那张单落到 `UNASSIGNED`，**建单与更新两支都如此**；
- `order.withdraw` ⇒ `REFUNDED`（P2 语义一字不变）；
- `order.update` ⇒ 仍按 P3 的接受集闸（`{IN_PROGRESS, ISSUE, PAUSED, REFUNDED}`，合法但不在集内 = applied-as-noop）。
  ⚠️ **建单支（本地还没有那张单）不带接受集闸、照抄事件里的 `raw_status`**（P3 语义，Global Constraints 5/6）
  ——下面那条 first-ever `order.update` 用例钉住这一点。

⛔ **建单与更新两支不许打架**：本文件那条「同一张单第二次接手仍然落到未分配」专门钉住更新支，
`order.update`（本地已有单 / 本地还没有单两种）与 `order.withdraw` 是对照组——没有这些，
「把整支都改成 UNASSIGNED」也会全绿。

⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
⚠️ 夹具一律**合成值**（`https://peer-a.test.invalid` / `https://peer-b.test.invalid` /
`测试上家甲` / `SYN-P4-*`）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.business.api.peer_views import receive_events
from apps.business.models import Game, Order, PeerUpstreamBinding
from apps.business.peer_sync import hash_token, new_token

#: 两端对齐用的游戏名（裁决 R21：按名字对齐，⛔ 不传 id）。
GAME = "测试游戏"
#: 事件时刻的基准（合成值；`plus_seconds` 相对它推移）。
BASE_TIME = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)


class TestPeerBase(TenantTestCase):
    """共用夹具：一款游戏、一条启用的上游绑定（下家侧）。"""

    def setUp(self):
        self.game = Game.objects.create(name=GAME)
        self.kefu = get_user_model().objects.create_user(
            username="syn-p4-landing-kefu", password="pw-syn-p4"
        )
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            # 令牌两列按模型口径给全（`token_in_hash` 只存哈希、`token_out` 存明文）。
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.kefu,
        )

    def _event(self, kind, order_no, *, status="IN_PROGRESS", game_name=GAME,
               plus_seconds=0):
        """造一条事件：`kind` / `target_ref` / `occurred_at` 在**顶层**，订单字段进 `payload`。"""
        return {
            "kind": kind,
            "target_ref": order_no,
            "occurred_at": (BASE_TIME + timedelta(seconds=plus_seconds)).isoformat(),
            "payload": {
                "status": status,
                "total_price": "88.00",
                "remark": "上家备注",
                "content": "客户原始需求",
                "game_name": game_name,
            },
        }

    def _withdraw_event(self, order_no, *, plus_seconds=0, actor="测试上家甲",
                        reason="taker_removed"):
        """造一条撤单事件：`withdraw_reason` / `actor_name` 都在**事件级**（spec §16.2）。"""
        event = self._event(
            "order.withdraw", order_no, status="WAITING_GRAB",
            plus_seconds=plus_seconds,
        )
        event["withdraw_reason"] = reason
        event["actor_name"] = actor
        return event


class LandingStatusTests(TestPeerBase):
    """接手类事件落「未分配」；`order.update` / `order.withdraw` 旧语义不动。"""

    def test_a_new_order_from_assign_lands_unassigned(self):
        receive_events(self.upstream_binding, [self._event(
            "order.assign", "SYN-P4-1", status="IN_PROGRESS", game_name=GAME)])
        self.assertEqual(Order.objects.get(source_order_no="SYN-P4-1").status, "UNASSIGNED")

    def test_a_grab_also_lands_unassigned(self):
        receive_events(self.upstream_binding, [self._event(
            "order.grab", "SYN-P4-2", status="IN_PROGRESS", game_name=GAME)])
        self.assertEqual(Order.objects.get(source_order_no="SYN-P4-2").status, "UNASSIGNED")

    def test_a_second_take_resets_an_existing_order_to_unassigned(self):
        """换人再派一次 ⇒ 对方仍是「未分配」（⛔ 不许被顶成代练中）。"""
        receive_events(self.upstream_binding, [self._event(
            "order.assign", "SYN-P4-3", status="IN_PROGRESS", game_name=GAME)])
        second = receive_events(self.upstream_binding, [self._event(
            "order.assign", "SYN-P4-3", status="IN_PROGRESS", game_name=GAME,
            plus_seconds=30)])
        # ⚠️ 第二条事件必须**真的走到更新支**（`applied=1`）：否则它若退化成 stale 跳过，
        #    下面那条状态断言只会看到**建单支**留下的 `UNASSIGNED` 而恒绿——更新支没被测到。
        self.assertEqual(second["applied"], 1, second)
        self.assertEqual(second["skipped"], 0, second)
        self.assertEqual(second["skipped_details"], [], second)
        self.assertEqual(Order.objects.get(source_order_no="SYN-P4-3").status, "UNASSIGNED")

    def test_an_order_update_still_syncs_the_status(self):
        """`order.update` 不受本次改动影响（P3 接受集照旧）。"""
        receive_events(self.upstream_binding, [self._event(
            "order.assign", "SYN-P4-4", status="IN_PROGRESS", game_name=GAME)])
        receive_events(self.upstream_binding, [self._event(
            "order.update", "SYN-P4-4", status="ISSUE", game_name=GAME,
            plus_seconds=30)])
        self.assertEqual(Order.objects.get(source_order_no="SYN-P4-4").status, "ISSUE")

    def test_a_first_ever_order_update_lands_the_events_status(self):
        """⭐ P3 语义不变（Global Constraints 5/6）：本地还没有这张单时先收到 `order.update`
        ⇒ 落事件里的状态（⛔ 不是「未分配」）——建单支也必须按 kind 分口径。"""
        receive_events(self.upstream_binding, [self._event(
            "order.update", "SYN-P4-UPD-1", status="IN_PROGRESS", game_name=GAME)])
        self.assertEqual(Order.objects.get(source_order_no="SYN-P4-UPD-1").status, "IN_PROGRESS")

    def test_a_withdraw_still_refunds(self):
        receive_events(self.upstream_binding, [self._event(
            "order.assign", "SYN-P4-5", status="IN_PROGRESS", game_name=GAME)])
        receive_events(self.upstream_binding, [
            self._withdraw_event("SYN-P4-5", plus_seconds=30, actor="测试上家甲"),
        ])
        self.assertEqual(Order.objects.get(source_order_no="SYN-P4-5").status, "REFUNDED")
