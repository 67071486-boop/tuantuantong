"""上下游 P3 Task 4：**下行接收侧**的状态接受集闸 + `hall_price` 落库（spec §10.2 / §16.2 / §16.3）。

本文件钉两件事（业务方裁定，预检 R2 / R3 / R10）：

① **状态接受集闸**：下行只接受 `DOWNSTREAM_ACCEPTED_STATUSES` 那 4 种（`IN_PROGRESS` /
   `ISSUE` / `PAUSED` / `REFUNDED`）。对端推来一个**合法但不在集合里**的状态（例如
   `PENDING_REVIEW`）⇒ 下家的状态**不动**，但这**不是跳过**（裁定 R3）：事件形状合法、
   对端也确实发来了 ⇒ 算 `applied`、⛔ 不进 `skipped_details`、⛔ 不新造 `code`
   （那会让发送侧把它记成 `failed` 并退避重投一条注定不改状态的事件）。
   ⚠️ 未知状态（连 `OrderStatus.values` 都不在）仍走 `peer_inbound` 既有的硬跳过
   `unknown_status`——本文件不重复钉那一条（`tests/test_peer_inbound_update.py` 已覆盖）。

② **发单价落库**（裁定 R10：上家的发单价 = 下家的接单价）：`hall_price` **首单建单**
   也要落（原计划漏了这条），**后续更新**（`order.update` 与 P1/P2 的 `order.assign` /
   `order.grab` / `order.withdraw`）也要落；对端没带价（`None`）时 ⛔ 不许把本地已有的价
   写没（`is not None`，E-0021）。

## 夹具

`TestPeerBase` 照 `tests/test_peer_inbound_update.py` 的形状（本文件自带一份，⛔ 不改
`tests/test_peer_p3_triggers.py` 里那个同名夹具——那个是发/收**触发挂钩**用的，语义不同）：
一条启用的 `PeerUpstreamBinding` + 一张**已链接**的单（`PeerOrderLink.peer_order_no`
等于事件的 `target_ref`）⇒ 事件默认走**更新**分支。首单那条用例改用一个新的对端单号
（本地没有 `PeerOrderLink`）⇒ 走**建单**分支。

⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
⚠️ 夹具值**全部是合成值**（`SYN-P3-*` / `UP-*` / `*.test.invalid` / `测试*`）。

⚠️ 与任务书的偏差（执行期更正）：任务书的 `_push(...)` 调 `receive_events`（它返**字典**），
而两条用例按 `outcome.applied` **属性**取值——字典没有这个属性，照抄会 `AttributeError`。
这里保留 `_push` 走 `receive_events`（任务书指定的入口），断言改用**下标**
（`outcome["applied"]` / `outcome["skipped"]` / `outcome["skipped_details"]`），语义一字不改。
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.api.peer_views import receive_events
from apps.business.models import (
    Game,
    Order,
    OrderStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
)
from apps.business.peer_sync import hash_token, new_token

#: 已链接的那张本地单的对端单号（同时是本地单号——夹具刻意让两者相等，事件按它去重）。
ORDER_NO = "SYN-P3-OLD"
#: 首单用例用的**新**对端单号（本地还没有 `PeerOrderLink` ⇒ 建单分支）。
NEW_REF = "SYN-P3-NEW"
#: 两端对齐用的游戏名（裁决 R21：按名字对齐，⛔ 不传 id）。
GAME_NAME = "测试游戏"
#: ⭐ 案 A（D-0364）：池内豁免写进审计行 reason 的人话（唯一出处
#: `apps/business/peer_inbound.py::PEER_INBOUND_STATUS_HELD_NOTE`；这里**钉字面值**，
#: ⛔ 不 import 常量——E-0024：自指断言在被断言的常量改坏时不会红）。
STATUS_HELD_NOTE = "状态保持：本单已回池，状态不跟代练中"


class TestPeerBase(TenantTestCase):
    """共用夹具：一款游戏、一条启用的上游绑定、一张已链接的本地单。"""

    def setUp(self):
        self.game = Game.objects.create(name=GAME_NAME)
        self.kefu = get_user_model().objects.create_user(
            username="syn-p3-inbound-kefu", password="pw-syn-p3"
        )
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家甲",
            peer_base_url="https://peer-up.test.invalid",
            peer_account="syn-up-account",
            # 令牌两列按模型口径给全（`token_in_hash` 只存哈希、`token_out` 存明文）。
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-local.test.invalid",
            bound_by=self.kefu,
        )
        self.order = Order.objects.create(
            order_no=ORDER_NO,
            game=self.game,
            status=OrderStatus.WAITING_GRAB,
            total_price=Decimal("10.00"),
            source_order_no=ORDER_NO,
        )
        self.link = PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.upstream_binding,
            peer_order_no=ORDER_NO,
            # `last_applied_at` 留空 = 比任何带时刻的事件都旧 ⇒ 事件会被应用
            # （`test_peer_inbound_update` 的 C4 用例钉过这条口径）。
        )


class DownstreamInboundGateTests(TestPeerBase):
    """状态接受集闸（R1/R3）与发单价落库（R10）。"""

    def _push(self, status, hall_price="88.00", kind="order.update"):
        return receive_events(self.upstream_binding, [{
            "kind": kind, "target_ref": self.order.order_no,
            "occurred_at": (self.order.updated_at + timedelta(seconds=5)).isoformat(),
            "payload": {"status": status, "hall_price": hall_price, "game_name": GAME_NAME},
        }])

    def test_an_accepted_status_is_applied(self):
        self._push("ISSUE")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "ISSUE")

    def test_a_status_outside_the_accepted_set_is_not_applied(self):
        """上家把单改成「待验收」不推给下家；即便推来了也不许改下家的状态。"""
        before = self.order.status
        outcome = self._push("PENDING_REVIEW")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, before)
        # ⚠️ 不应用 ≠ 硬跳过（**裁定 R3**）：这条事件形状合法、对端也确实是发来了，
        #    只是「这个状态不该改下家」⇒ 算 `applied`（**不进** `skipped_details`、⛔ 不新造 code
        #    ——那会让发送侧把它记成 `failed` 并退避重投一条注定不改状态的事件）。
        self.assertEqual(outcome["applied"] + outcome["skipped"], 1)
        self.assertEqual(outcome["applied"], 1)
        self.assertEqual(outcome["skipped_details"], [])

    def test_an_order_update_lands_the_price_as_the_customer_price(self):
        """⭐ D-0307（2026-10-26）：`order.update` 携价 ⇒ 写**客户价**（`total_price`）。

        上家发单价落到下家账上 = 「接单客户价」（进账侧）；`hall_price`（下家给打手的价）
        **一格不动**（由下家自己决定改不改）。
        """
        self._push("IN_PROGRESS", hall_price="123.45")
        self.order.refresh_from_db()
        self.assertEqual(str(self.order.total_price), "123.45")
        self.assertIsNone(self.order.hall_price, "order.update 支不许动打手价")

    def test_the_ignored_status_does_not_block_the_price(self):
        """状态不在集合里 ⇒ 状态不动，但客户价照落（两件事各自独立）。"""
        self._push("PENDING_REVIEW", hall_price="77.00")
        self.order.refresh_from_db()
        self.assertEqual(str(self.order.total_price), "77.00")
        self.assertIsNone(self.order.hall_price)

    def test_a_first_push_lands_the_upstream_hall_price_as_the_taker_price(self):
        """⭐ R10：首单（本地还没有这张单）也要把发单价落成下家的接单价。"""
        receive_events(self.upstream_binding, [{
            "kind": "order.assign", "target_ref": NEW_REF,
            "occurred_at": (self.order.updated_at + timedelta(seconds=9)).isoformat(),
            "payload": {"status": "IN_PROGRESS", "hall_price": "52.00",
                        "game_name": GAME_NAME},
        }])
        created = Order.objects.get(source_order_no=NEW_REF)
        self.assertEqual(str(created.hall_price), "52.00")

    def test_a_p1_p2_event_also_carries_the_price(self):
        """R10：`order.assign` 这类 P1/P2 事件也要把发单价带上（否则内存旧值被原样写回）。"""
        self._push("IN_PROGRESS", hall_price="64.00", kind="order.assign")
        self.order.refresh_from_db()
        self.assertEqual(str(self.order.hall_price), "64.00")

    def test_a_missing_price_does_not_wipe_the_local_one(self):
        """对端没带价（`None`）⇒ ⛔ 不许把本地已有的价写没（`is not None`，E-0021）。"""
        self.order.hall_price = "39.00"
        self.order.save(update_fields=["hall_price"])
        self._push("IN_PROGRESS", hall_price=None)
        self.order.refresh_from_db()
        self.assertEqual(str(self.order.hall_price), "39.00")

    def test_an_order_update_lands_content_but_ignores_the_payload_total_price(self):
        """⭐ **D-0377 的改准（2026-11-06，取代裁定 R2 的「remark 不落地」半句）**：
        `order.update` 支落内容面（remark 等）+ `status` + **客户价（= 载荷里的
        `hall_price`）**；载荷里的 `total_price` 键仍被忽略，打手价（`hall_price`）不动。

        ⚠️ **2026-10-26 改准（D-0307）**：旧口径写「这一支只写 `status` + `hall_price`，
        `total_price` 一个字不改」；终裁是「上家改价改**客户价**（`total_price`），
        打手价（`hall_price`）由下家自己决定」⇒ 现在变的是 `total_price`（值取自
        `hall_price`），**载荷里的 `total_price` 键仍被忽略**。
        """
        self.order.remark = "本地旧备注"
        self.order.save(update_fields=["remark"])
        self.order.refresh_from_db()

        receive_events(self.upstream_binding, [{
            "kind": "order.update",
            "target_ref": self.order.order_no,
            "occurred_at": (self.order.updated_at + timedelta(seconds=5)).isoformat(),
            "payload": {
                "status": "ISSUE",
                "hall_price": "55.00",
                # ⛔ `total_price` 键**不许**落地（下家客户价只跟发单价，D-0307）。
                "total_price": "999.00",
                # ⭐ D-0377：remark 落地（已转正正式单跟着上家改内容）。
                "remark": "对端新备注",
                "game_name": GAME_NAME,
            },
        }])

        self.order.refresh_from_db()
        self.assertEqual(self.order.remark, "对端新备注")
        # 打手价一格不动（D-0307：由下家自己决定改不改）。
        self.assertIsNone(self.order.hall_price)
        # 客户价 = 载荷里的 `hall_price`（55.00），⛔ 不是载荷里那个被忽略的 `total_price`（999.00）。
        self.assertEqual(self.order.total_price, Decimal("55.00"))
        # 照旧落地的状态（前置哨兵：事件真的应用上了，⛔ 不是"整条链路没跑"）。
        self.assertEqual(self.order.status, "ISSUE")


class PoolStatusGuardTests(TestPeerBase):
    """⭐ 案 A（D-0364，2026-11-02 生产实锤）：入站同步**不许覆盖下家的池内态**。

    现场：下家把单放回自己池子（清接手人 → 未分配、重抛大厅 → 待抢单）是**本地状态机
    动作、按设计不回传上家**；而上家任何一次普通字段更新生成的 `order.update` 载荷
    **恒带上家当前状态**（= 代练中），接收侧「状态照抄」支无条件覆盖 ⇒ 单被静默打回
    「代练中」并掉出大厅（僵尸态 2h48m 至退款）。

    判据（唯一出处 `peer_inbound.PEER_POOL_LOCAL_STATUSES` × 入站 `IN_PROGRESS`）：
    - 本地 ∈ {未分配, 待抢单} 且入站 `IN_PROGRESS` ⇒ **状态不写**，但事件照旧 `applied`
      （R3 同款：判成跳过会让发送侧退避重投）、水位照推进、其余字段照写；
    - 入站 `ISSUE` / `PAUSED` / `REFUNDED` ⇒ **照旧穿透**（上家的异常 / 暂停 / 退单是
      真实业务事件，退单必须能落到已回池的单上）。

    ⚠️ **变异自证（必须能红）**：把 `peer_inbound` 里 `pool_status_exempt` 的判据去掉
    （还原成裸接受集闸）⇒ 本组第 1 条当场红（状态被改成 IN_PROGRESS）。
    """

    def _push_update(self, status, hall_price="88.00", *, offset_seconds=5):
        """给已链接的本地单推一条 `order.update`（默认比夹具新 5 秒 ⇒ 必过 stale 闸）。"""
        return receive_events(self.upstream_binding, [{
            "kind": "order.update",
            "target_ref": self.order.order_no,
            "occurred_at": (
                self.order.updated_at + timedelta(seconds=offset_seconds)
            ).isoformat(),
            "payload": {"status": status, "hall_price": hall_price,
                        "game_name": GAME_NAME},
        }])

    def _inbound_update_row(self):
        return OperationLog.objects.get(
            order_no=self.order.order_no, action="peer.inbound_update"
        )

    def test_an_inbound_in_progress_does_not_override_a_pool_status(self):
        """本地待抢单 + 入站 order.update(IN_PROGRESS) ⇒ 状态保住、价照落、留「状态保持」。"""
        outcome = self._push_update("IN_PROGRESS", hall_price="123.45")

        # 事件照旧 applied（R3 同款：⛔ 不进 skipped——那会让发送侧退避重投）。
        self.assertEqual(outcome["applied"], 1, outcome)
        self.assertEqual(outcome["skipped"], 0, outcome)
        self.assertEqual(outcome["skipped_details"], [])
        self.order.refresh_from_db()
        self.link.refresh_from_db()
        # 状态被保住（变异判据：去掉豁免 ⇒ 这里变 IN_PROGRESS，本条红）。
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        # 其余字段照写：客户价照落；打手价 / 备注本就不在 order.update 支的写面。
        self.assertEqual(self.order.total_price, Decimal("123.45"))
        self.assertIsNone(self.order.hall_price)
        # 水位照推进（公共尾不受豁免影响）。
        self.assertEqual(
            self.link.last_applied_at,
            self.order.updated_at + timedelta(seconds=5),
        )
        # 审计行要能看出「状态未跟」：reason = 状态保持、正文含「状态保持」、
        # 无状态段（from_value 空 ⇒ 渲染器整段剔除，不留「（状态： → X）」空壳）。
        row = self._inbound_update_row()
        self.assertEqual(row.reason, STATUS_HELD_NOTE)
        self.assertIn("状态保持", row.summary)
        self.assertNotIn("（状态：", row.summary)
        self.assertEqual(row.from_value, "")
        self.assertEqual(row.to_value, "待抢单")

    def test_an_unassigned_local_status_is_held_too(self):
        """本地未分配（由入站 `order.assign` 落的）+ 入站 IN_PROGRESS ⇒ 仍保池内。"""
        # 第一步：入站接手类事件把单打回未分配（D-0343 回池口径；这一步的形状本身
        # ⛔ 不在豁免范围——上家收回 / 重派走的就是它）。
        first = receive_events(self.upstream_binding, [{
            "kind": "order.assign",
            "target_ref": self.order.order_no,
            "occurred_at": (self.order.updated_at + timedelta(seconds=5)).isoformat(),
            "payload": {"status": "IN_PROGRESS", "hall_price": "60.00",
                        "game_name": GAME_NAME},
        }])
        self.assertEqual(first["applied"], 1, first)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)

        # 第二步：比第一步**更晚**的普通更新带着上家那边的「代练中」过来
        # （案 A 现场：stale 闸拦不住——触发事件确实比本地动作新）。
        second = self._push_update("IN_PROGRESS", offset_seconds=9)
        self.assertEqual(second["applied"], 1, second)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        row = self._inbound_update_row()
        self.assertEqual(row.reason, STATUS_HELD_NOTE)

    def test_a_refunded_push_still_penetrates_a_pool_order(self):
        """本地待抢单 + 入站 REFUNDED ⇒ 照旧变退单（穿透不回归：退单必须落到回池单上）。"""
        outcome = self._push_update("REFUNDED")

        self.assertEqual(outcome["applied"], 1, outcome)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)
        # 非豁免档：reason 不带「状态保持」、状态段照常出现（B1 的 from→to）。
        row = self._inbound_update_row()
        self.assertEqual(row.reason, "")
        self.assertNotIn(STATUS_HELD_NOTE, row.summary)
        self.assertIn("（状态：待抢单 → 已退单）", row.summary)
        self.assertEqual(row.from_value, "待抢单")

    def test_an_in_progress_push_onto_an_in_progress_order_still_applies(self):
        """本地代练中 + 入站 IN_PROGRESS ⇒ 照旧应用（豁免只护池内两态，常态零漂移）。"""
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])

        outcome = self._push_update("IN_PROGRESS", hall_price="77.00")

        self.assertEqual(outcome["applied"], 1, outcome)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self.order.total_price, Decimal("77.00"))
        # from == to ⇒ 状态段整段剔除（R3 口径：无状态变化不留「代练中 → 代练中」噪声）。
        row = self._inbound_update_row()
        self.assertNotIn("（状态：", row.summary)
        self.assertEqual(row.reason, "")
