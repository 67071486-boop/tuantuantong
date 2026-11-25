"""E-0282 / B2·B3（2026-10-24）：**到图触发补推**的三个 READY 写入点 + 存量补推命令。

计划：`docs/plans/2026-10-24-member-assign-on-mirror-claim-and-pic-repush.md` §3-B2 / §3-B3。

三个触发点（⭐ L1：全仓 READY 写入点**恰好这三处**，`screenshot_views.py 的读过滤` 是读过滤）：

| 触发点 | 位置纪律 |
|---|---|
| `claim_screenshot`（内网 / 打手 App / 开放平台三端共用） | `row.save()` 之后、`img_type` 闸**之外**（M3：闸内只服务订单截图档的广播，member 口径「全传」含群码） |
| `store_remote_pics`（上家拉回，E-0282 事故路径） | **非事务**（M4：拉图链路刻意不在事务里，窗口由 B3 兜底）——判据见 `tests/test_jidanpu_member_pics.py` 的回归用例 |
| `peer_inbound._apply_screenshot`（peer 回传） | savepoint 之后、幂等支之外 |

命令（B3）：`repush_member_pics` 默认 dry-run、`--execute` 才排事件（照 `rescue_gone_mirrors` 的
口径）；圈面按台账判据收窄（L3：无「已传到下家」成功句）。

⚠️ 判据一律打**数据库行**（`JidanpuSyncEvent` 的 kind / payload）；夹具一律合成值（E-0077）。
"""
from __future__ import annotations

from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.business.api.screenshot_views import claim_screenshot, issue_screenshot_ticket
from apps.business import jidanpu_member
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderScreenshot,
    OrderStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_inbound import apply_events
from apps.business.peer_sync import hash_token, new_token
from apps.core.storage import get_storage

JPEG_BODY = b"\xff\xd8\xff" + b"\0" * 16

KIND_ASSIGN = "member.assign"

GAME_NAME = "补推触发点夹具游戏"
ORDER_NO = "20261024000000000201"
JID = "JDP-00000000000000000201"
PEER_NO = "20261024000000000901"
MEMBER_BASE_URL = "https://jidanpu-repush.test.invalid"
BASE_TIME = datetime(2026, 10, 24, 2, 0, tzinfo=dt_timezone.utc)

#: 一条合法的共享截图对象名（合成值；形状闸要求 `shared-orders/<origin>/<随机>.<ext>`）。
SHARED_KEY = "shared-orders/SYN-REPUSH-ORIG-1/abcdef123456.jpg"


def _pending_then_claim(order, user, *, img_type=OrderScreenshot.IMG_ORDER):
    """三段式的前两段：签发一张 pending 票 ⇒ 把字节放进对象存储 ⇒ 认领成 ready。"""
    ticket = issue_screenshot_ticket(
        order_no=order.order_no, ext="jpg", img_type=img_type, uploaded_by=user,
    )
    assert ticket.ok, ticket.msg
    get_storage().put_bytes(ticket.row.object_key, JPEG_BODY, content_type="image/jpeg")
    outcome = claim_screenshot(
        order_no=order.order_no, uploaded_by=user, screenshot_id=ticket.row.pk,
        remark="合成备注",
    )
    assert outcome.ok, outcome.msg
    return outcome.row


class _RepushFixture(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name=GAME_NAME)
        self.user = User.objects.create_user(username="repush-kefu-a", password="pw-canary")
        self.order = Order.objects.create(
            game=self.game, order_no=ORDER_NO, status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("12.34"), total_price=Decimal("50.00"),
        )
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=self.user,
            temp_amount=Decimal("12.34"),
        )
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user, jidanpu_base_url=MEMBER_BASE_URL,
            account="acc-repush-a", password="pw-canary",
            source_name="团团通系统", enabled=True,
        )
        self.link = JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order, jidanpu_order_id=JID, in_hall=False,
        )

    def _assign_rows(self, order=None):
        return JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_ASSIGN,
            target_ref=(order or self.order).order_no,
        )


class ClaimScreenshotTriggerTests(_RepushFixture):
    """触发点①：`claim_screenshot`（三端共用的认领函数）——save 后、img_type 闸外。"""

    def test_claiming_an_order_shot_queues_the_repush(self):
        """认领一张**订单截图** ⇒ 给已建链的①式绑定排一条 assign（payload repush）。"""
        row = _pending_then_claim(self.order, self.user, img_type=OrderScreenshot.IMG_ORDER)

        self.assertEqual(row.status, OrderScreenshot.STATUS_READY)
        queued = self._assign_rows()
        self.assertEqual(queued.count(), 1)
        event = queued.get()
        self.assertEqual(event.status, "pending")
        self.assertIs(event.payload["repush"], True)
        self.assertEqual(event.payload["member_binding_id"], self.binding.pk)

    def test_claiming_a_group_qr_also_queues_the_repush(self):
        """⭐ M3：**群聊二维码**也触发——触发点在 `img_type` 闸之外（闸内照抄会静默漏掉群码；
        member 口径「全传」含群码）。判别力：把触发挪进 `if img_type == IMG_ORDER` ⇒ 本条红。"""
        _pending_then_claim(self.order, self.user, img_type=OrderScreenshot.IMG_GROUP_QR)

        self.assertEqual(self._assign_rows().count(), 1)

    def test_a_claim_on_an_order_without_any_link_queues_nothing(self):
        """没有 member 链路（还没录到过对方）⇒ 认领照常、一条都不排。"""
        JidanpuMemberOrderLink.objects.filter(pk=self.link.pk).delete()

        _pending_then_claim(self.order, self.user)

        self.assertEqual(self._assign_rows().count(), 0)


class PeerInboundScreenshotTriggerTests(_RepushFixture):
    """触发点③：peer 回传截图入库（savepoint 之后）⇒ 排补推。"""

    def setUp(self):
        super().setUp()
        self.kefu = User.objects.create_user(username="repush-peer-kefu", password="pw-canary")
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲", peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(new_token()), token_out=new_token(),
            local_base_url="https://peer-b.test.invalid", bound_by=self.kefu,
        )
        PeerOrderLink.objects.create(
            order=self.order, upstream_binding=self.upstream_binding,
            peer_order_no=PEER_NO, last_applied_at=BASE_TIME,
        )

    def _screenshot_event(self, key=SHARED_KEY):
        return {
            "kind": "order.screenshot.add", "target_ref": PEER_NO,
            "occurred_at": BASE_TIME.isoformat(),
            "payload": {"object_key": key, "img_type": 0, "remark": "截图备注"},
        }

    def test_a_peer_screenshot_add_queues_the_repush(self):
        """peer 回传一张新图 ⇒ 落库 + 恰好一条 assign（payload repush）。"""
        result = apply_events([self._screenshot_event()], binding=self.upstream_binding)

        self.assertEqual(result.applied, 1, result)
        self.assertTrue(
            OrderScreenshot.objects.filter(order=self.order, object_key=SHARED_KEY,
                                           status=OrderScreenshot.STATUS_READY).exists(),
        )
        queued = self._assign_rows()
        self.assertEqual(queued.count(), 1)
        self.assertIs(queued.get().payload["repush"], True)

    def test_a_duplicate_peer_event_does_not_queue_a_second_row(self):
        """幂等那一轮（同 key 已有行 ⇒ already-applied）⇒ **不触发**（边沿：只认新落的那笔）。"""
        first = apply_events([self._screenshot_event()], binding=self.upstream_binding)
        self.assertEqual(first.applied, 1)

        second = apply_events([self._screenshot_event()], binding=self.upstream_binding)

        self.assertEqual(second.applied, 1, second)  # 幂等也算 applied
        self.assertEqual(self._assign_rows().count(), 1, "重复事件不得再排一条")


class RepushMemberPicsCommandTests(_RepushFixture):
    """⭐ B3：`repush_member_pics`——默认 dry-run；`--execute` 排事件；圈面按台账收窄（L3）。"""

    def setUp(self):
        super().setUp()
        self.schema = connection.schema_name
        # 圈内那条：链路 + ready 图 + **无**「已传到下家」成功句。
        self._ready_shot(self.order)
        # 圈外三条：已传成功 / 无图 / 无链路。
        self.done_order = self._order("20261024000000000202")
        self._ready_shot(self.done_order)
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.done_order,
            jidanpu_order_id="JDP-00000000000000000202", in_hall=False,
        )
        JidanpuSyncEvent.objects.create(
            direction="in", kind=KIND_ASSIGN, status="done",
            target_type="order", target_ref=self.done_order.order_no,
            reason="订单截图已传到下家（共 1 张：订单截图 1 张）",
            payload={"member_binding_id": self.binding.pk, "pushed_pic_keys": {}},
        )
        self.noshot_order = self._order("20261024000000000203")
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.noshot_order,
            jidanpu_order_id="JDP-00000000000000000203", in_hall=False,
        )
        self.nolink_order = self._order("20261024000000000204")
        self._ready_shot(self.nolink_order)

    def _order(self, order_no):
        return Order.objects.create(
            game=self.game, order_no=order_no, status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("12.34"), total_price=Decimal("50.00"),
        )

    def _ready_shot(self, order):
        #: object_key 全局唯一 ⇒ 每张单一把合成键（同形状、不同 origin 段）。
        key = f"shared-orders/SYN-REPUSH-{order.order_no[-4:]}/abcdef123456.jpg"
        get_storage().put_bytes(key, JPEG_BODY, content_type="image/jpeg")
        return OrderScreenshot.objects.create(
            order=order, img_type=OrderScreenshot.IMG_ORDER, object_key=key,
            status=OrderScreenshot.STATUS_READY, size_bytes=len(JPEG_BODY),
            uploaded_by=self.user,
        )

    def _run(self, *args):
        out = StringIO()
        call_command("repush_member_pics", "--schema", self.schema, *args, stdout=out)
        return out.getvalue()

    def test_default_is_dry_run_and_writes_nothing(self):
        out = self._run()

        self.assertIn(self.order.order_no, out, "dry-run 也该逐行打印计划")
        self.assertNotIn(self.done_order.order_no, out, "已传成功的链路不进圈（L3）")
        self.assertNotIn(self.noshot_order.order_no, out, "无图不进圈")
        self.assertNotIn(self.nolink_order.order_no, out, "无链路不进圈")
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind=KIND_ASSIGN, direction="out").count(), 0,
            "dry-run 落了事件",
        )

    def test_execute_queues_the_missing_chain_with_the_repush_payload(self):
        out = self._run("--execute")

        self.assertIn("1 条链路", out)
        rows = self._assign_rows()
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().target_ref, self.order.order_no)
        self.assertIs(rows.get().payload["repush"], True)
        # 圈外三条一条都不排。
        for order in (self.done_order, self.noshot_order, self.nolink_order):
            self.assertEqual(self._assign_rows(order).count(), 0)

    def test_an_immediate_rerun_dedupes_onto_the_in_flight_row(self):
        """⭐ M5（在飞那一半）：事件还在飞 ⇒ 重跑 = 零新行（`queue_member_event` 在飞归并）。"""
        self._run("--execute")
        self._run("--execute")

        self.assertEqual(self._assign_rows().count(), 1)

    def test_an_openapi_link_is_not_in_the_plan(self):
        """②式绑定（mode 唯一判据）⇒ dry-run 清单里也不出现（不排永远办不到的事）。"""
        openapi_user = User.objects.create_user(username="repush-openapi")
        openapi_binding = JidanpuMemberBinding.objects.create(
            user=openapi_user, jidanpu_base_url="https://jidanpu-openapi-repush.test.invalid",
            account="acc-openapi", password="pw", mode="openapi",
            client_id="cid-synth", enabled=True,
        )
        openapi_order = self._order("20261024000000000205")
        self._ready_shot(openapi_order)
        JidanpuMemberOrderLink.objects.create(
            binding=openapi_binding, order=openapi_order,
            jidanpu_order_id="JDP-00000000000000000205", in_hall=False,
        )

        out = self._run()

        self.assertNotIn(openapi_order.order_no, out)

    def test_hall_and_withdrawn_links_are_excluded(self):
        """⭐ 修复轮 H1 / M2（2026-10-24）：还挂在下家大厅（`in_hall=True`）与已退成
        （`withdrawn_at` 非空）的链路**不进圈**——与 B2 到图触发同两个过滤。
        判别力：把命令里那两个过滤拿掉 ⇒ dry-run 清单断言红。"""
        hall_order = self._order("20261024000000000206")
        self._ready_shot(hall_order)
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=hall_order,
            jidanpu_order_id="JDP-00000000000000000206", in_hall=True,
        )
        withdrawn_order = self._order("20261024000000000207")
        self._ready_shot(withdrawn_order)
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=withdrawn_order,
            jidanpu_order_id="JDP-00000000000000000207", in_hall=False,
            withdrawn_at=BASE_TIME,
        )

        dry = self._run()
        self.assertNotIn(hall_order.order_no, dry, "在厅链路不进圈（H1）")
        self.assertNotIn(withdrawn_order.order_no, dry, "已退成链路不进圈（M2）")
        # 实跑同样零排队。
        self._run("--execute")
        for order in (hall_order, withdrawn_order):
            self.assertEqual(self._assign_rows(order).count(), 0)
