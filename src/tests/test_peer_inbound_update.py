"""上下游 P2 接收侧的**时刻裁决 / 同单更新 / 撤单落库**（spec §16.2 / §16.3 / §16.5）。

判据分五层：

① **时刻裁决**（Ruling 22 的严格口径）：已有 `PeerOrderLink` 时，事件级 `occurred_at`
   **严格晚于** `link.last_applied_at` ⇒ 更新；**相等或更旧** ⇒ `code=stale` 的良性跳过；
   缺失 / 不是可解析的 ISO 时刻 ⇒ `code=bad_shape` 的硬跳过；
② **同单更新的字段集**：更新的是 §10.1 那一组订单列 + 按名字解析的游戏；`order_no` /
   `order_source` / `source` / `sales_user` / `sales_agent` / `created_at` 一行都不许碰；
③ **撤单落库**（spec §16.2）：直接写 `Order.status = REFUNDED` 列 + 一条
   `order.peer_withdraw` 订单日志；⛔ **不走** `apply_transition`（那个入口要节点集、还会跑
   全部副作用与对外回声）。⭐ **改准（P0-7 / `D-0288`）**：原文这里写
   「（那会跑本地退款钩子去清绩效）」并把「绩效行逐字节不变」当判据——**那个口径已作废**：
   钱包成为发薪唯一出口之后，完成单在完成那一刻就已自动入账（`settled_by="system"`），
   撤单只写状态列会让**钱留在打手钱包里**。今天撤单支在 `save()` 之后**显式调**
   `refunds.on_order_refunded(order, <赋值前状态>)` ⇒ 两列清 0 + 自动发薪回冲 + 客服行清 0；
   本文件那条反向控制已同批改准（钩子失败要整笔回滚那条在
   `tests/test_settle_p1_peer_withdraw.py`）；
④ **本地没有这张单时**（spec §16.3 末表）：`order.assign` / `order.grab` 照常落单
   （"不漏单"）；`order.withdraw` 跳过并给 `code=unknown_order`；
⑤ **部分失败契约**（spec §16.5）：每条跳过都带 `skipped_details` 的
   `{"target_ref", "code", "reason"}`，且**恰好**四个 code 取值。

⚠️ 必须用 `TenantTestCase`：`apps.business` 与 `apps.audit` 只在 `TENANT_APPS` 里。
⚠️ 夹具一律**合成值**（`测试客服甲` / `peer-account-a` / `https://peer-a.test.invalid`）。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import WxPusherBinding, WxPusherNotice
from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderSource,
    OrderStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
    Performance,
    PerformanceAdjustment,
)
from apps.business.peer_inbound import (
    SKIP_BAD_SHAPE,
    SKIP_STALE,
    SKIP_UNKNOWN_ORDER,
    SKIP_UNKNOWN_STATUS,
    apply_events,
)
from apps.business.peer_sync import hash_token, new_token

#: 本文件统一的"对端单号"常量（去重键是 `(绑定, 对端单号)`）。
PEER_NO = "A-1"
#: 本地订单号（形状照 `src/apps/business/order_no.py` 的产物，值本身无意义）。
LOCAL_NO = "20260926000000000031"


class PeerInboundUpdateTests(TenantTestCase):
    """`apply_events` 的时刻裁决 / 更新 / 撤单落库（spec §16.2 / §16.3）。"""

    #: 三个时刻：`T0 < T1 < T2`，`T1` 是本地已应用到的时刻（`link.last_applied_at`）。
    T0 = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)
    T1 = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
    T2 = datetime(2026, 9, 26, 11, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.game = Game.objects.create(name="上下游更新夹具游戏")
        self.other_game = Game.objects.create(name="上下游更新的另一款游戏")
        self.taker = get_user_model().objects.create_user(
            username="peer-inbound-taker", password="pw-taker"
        )
        self.kefu = get_user_model().objects.create_user(
            username="peer-inbound-kefu", password="pw-kefu"
        )
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.kefu,
        )
        self.order = Order.objects.create(
            order_no=LOCAL_NO,
            game=self.game,
            status=OrderStatus.WAITING_GRAB,
            total_price=Decimal("10.00"),
            remark="原备注",
            project_text="原项目名",
            customer_phone="原电话",
            customer_qq="原QQ",
            content="原需求",
            order_source=None,
            source="原来源快照",
            sales_user=self.kefu,
            sales_agent="原销售快照",
            created_by="原创建人快照",
            source_order_no=PEER_NO,
        )
        self.link = PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.binding,
            peer_order_no=PEER_NO,
            last_applied_at=self.T1,
        )

    def test_incoming_order_status_updates_and_withdrawal_notify_the_current_worker_once(self):
        worker_role, _ = Group.objects.get_or_create(name="代练员")
        self.taker.groups.add(worker_role)
        WxPusherBinding.objects.create(user=self.taker, app_id="936", uid="UID_peer_synthetic")
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        Performance.objects.create(order=self.order, user=self.taker, emp_type=EmpType.MAIN)
        with override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
            WXPUSHER_APP_ID="936", WXPUSHER_APP_TOKEN="AT_synthetic",
        ):
            applied = apply_events([
                self._event(kind="order.update", status=OrderStatus.PAUSED, occurred_at=self.T2),
                self._event(kind="order.update", status=OrderStatus.PAUSED, occurred_at=self.T2 + timedelta(seconds=1)),
                self._withdraw(occurred_at=self.T2 + timedelta(seconds=2)),
            ], binding=self.binding)
        self.assertEqual(applied.applied, 3, applied)
        self.assertEqual(list(WxPusherNotice.objects.order_by("pk").values_list("kind", "user_id", "order_no")), [
            ("paused", self.taker.pk, self.order.order_no),
            ("refunded", self.taker.pk, self.order.order_no),
        ])

    # ── 夹具构造器 ────────────────────────────────────────────────────────
    def _event(self, *, status=OrderStatus.IN_PROGRESS, occurred_at=None,
               order_no=PEER_NO, kind="order.assign", **payload_over):
        """造一条**新单 / 更新**事件：事件级元数据在**顶层**（与 `kind` 等并列）。

        ⚠️ `occurred_at=None` 表示**不带**这个键（P1 时代的事件形状）——⛔ 不是带一个 `None` 值。
        """
        payload = {
            "status": status,
            "total_price": "88.00",
            "remark": "新备注",
            "project_text": "新项目名",
            "customer_phone": "新电话",
            "customer_qq": "新QQ",
            "content": "新需求",
            "game_name": self.game.name,
        }
        payload.update(payload_over)
        event = {"kind": kind, "target_ref": order_no, "payload": payload}
        if occurred_at is not None:
            event["occurred_at"] = occurred_at.isoformat()
        return event

    def _withdraw(self, *, reason="taker_removed", occurred_at=None,
                  actor_name="测试客服甲", order_no=PEER_NO):
        """造一条撤单事件：`withdraw_reason` / `actor_name` 都在**事件级**。

        ⚠️ `actor_name` 由**上家**带过来（下家的绑定表里只有上家子账号的登录名，
        算不出显示名，spec §16.2）。
        """
        event = self._event(status=OrderStatus.WAITING_GRAB, order_no=order_no,
                            kind="order.withdraw",
                            occurred_at=occurred_at)
        event["withdraw_reason"] = reason
        if actor_name is not None:
            event["actor_name"] = actor_name
        return event

    # ── ① 时刻裁决 ────────────────────────────────────────────────────────
    def test_inbound_new_issue_enqueues_notification_once(self):
        from django.db import connection
        from apps.wecom.models import WecomAppConfig, WecomNotification
        WecomAppConfig.objects.create(tenant_schema=connection.schema_name, enabled=True, corp_id='synthetic', agent_id=1, secret='secret', order_status_enabled=True)
        event = self._event(kind='order.update', status=OrderStatus.ISSUE, occurred_at=self.T2)
        self.assertEqual(apply_events([event], binding=self.binding).applied, 1)
        from apps.wecom.notifications import _recover_order_journal
        _recover_order_journal()
        self.assertEqual(WecomNotification.objects.filter(order_no=LOCAL_NO, kind='order_status').count(), 1)
        apply_events([event], binding=self.binding)
        _recover_order_journal()
        self.assertEqual(WecomNotification.objects.filter(order_no=LOCAL_NO, kind='order_status').count(), 1)

    def test_inbound_old_status_before_enable_is_not_notified(self):
        from django.db import connection
        from apps.wecom.models import WecomAppConfig, WecomNotification
        WecomAppConfig.objects.create(tenant_schema=connection.schema_name, enabled=True, corp_id='synthetic', agent_id=1, secret='secret', order_status_enabled=True, order_status_enabled_at=self.T2 + timedelta(seconds=1))
        event = self._event(kind='order.update', status=OrderStatus.ISSUE, occurred_at=self.T2)
        self.assertEqual(apply_events([event], binding=self.binding).applied, 1)
        self.assertFalse(WecomNotification.objects.exists())

    def test_legacy_new_issue_without_event_time_is_notified(self):
        from django.db import connection
        from django.utils import timezone as django_timezone
        from apps.wecom.models import WecomAppConfig, WecomNotification
        WecomAppConfig.objects.create(tenant_schema=connection.schema_name, enabled=True, corp_id='synthetic', agent_id=1, secret='secret', order_status_enabled=True, order_status_enabled_at=django_timezone.now())
        event = self._event(kind='order.update', status=OrderStatus.ISSUE, order_no='NEW-PEER')
        self.assertEqual(apply_events([event], binding=self.binding).applied, 1)
        from apps.wecom.notifications import _recover_order_journal
        _recover_order_journal()
        self.assertEqual(WecomNotification.objects.filter(kind='order_status').count(), 1)

    def test_a_newer_event_updates_the_existing_order(self):
        """同一张单的第二次事件（抢单）按 `occurred_at` **严格更新**，不再一律跳过。"""
        result = apply_events(
            [self._event(occurred_at=self.T2)], binding=self.binding
        )
        self.assertEqual(result.applied, 1, result)
        self.assertEqual(result.skipped, 0, result)
        self.assertEqual(result.skipped_details, [])
        self.order.refresh_from_db()
        self.link.refresh_from_db()
        # ⚠️ P4 起接手类落地即未分配（业务方）：本条事件是 `order.assign`。
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        # `last_applied_at` 随应用一起推进（否则下一次同刻事件会被当成"新"）。
        self.assertEqual(self.link.last_applied_at, self.T2)

    def test_the_update_covers_the_whitelist_and_leaves_the_rest_alone(self):
        """更新的是 §10.1 那一组列 + 按名字解析的游戏；归属 / 标识列一行不碰。"""
        event = self._event(
            occurred_at=self.T2,
            total_price="123.45",
            remark="改过的备注",
            project_text="改过的项目名",
            customer_phone="改过的电话",
            customer_qq="改过的QQ",
            content="改过的需求",
            game_name=self.other_game.name,
        )
        # 载荷里塞进**不该被消费**的键：它们即使存在也不得影响本地那一行。
        event["payload"].update({
            "order_no": "被对端顶着写的单号",
            "source": "被对端顶着写的来源快照",
            "sales_agent": "被对端顶着写的销售快照",
            "sales_user": self.taker.pk,
            "order_source": "被对端顶着写的来源字典",
            "created_at": "1999-01-01T00:00:00+00:00",
        })
        result = apply_events([event], binding=self.binding)

        self.assertEqual(result.applied, 1, result)
        self.order.refresh_from_db()
        # ⚠️ P4 起接手类落地即未分配（业务方）：本条事件是 `order.assign`。
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        # ⭐ 业务方裁定：下家的客户价（= 接单价）取**发单价**，
        #    上家的客户价（载荷里的 `total_price`）不再被采用。
        self.assertEqual(self.order.total_price, Decimal("10.00"))
        self.assertEqual(self.order.remark, "改过的备注")
        self.assertEqual(self.order.project_text, "改过的项目名")
        self.assertEqual(self.order.customer_phone, "改过的电话")
        self.assertEqual(self.order.customer_qq, "改过的QQ")
        self.assertEqual(self.order.content, "改过的需求")
        self.assertEqual(self.order.game_id, self.other_game.pk)
        # 分组限制的几列逐条钉住（⛔ 不是"没报错"）。
        self.assertEqual(self.order.order_no, LOCAL_NO)
        self.assertEqual(self.order.source_order_no, PEER_NO)
        self.assertEqual(self.order.source, "原来源快照")
        self.assertEqual(self.order.sales_agent, "原销售快照")
        self.assertEqual(self.order.sales_user_id, self.kefu.pk)
        self.assertIsNone(self.order.order_source_id)
        self.assertEqual(self.order.created_by, "原创建人快照")

    def test_the_update_kind_covers_the_whitelist_and_leaves_the_rest_alone(self):
        """⭐ D-0377：`kind="order.update"` 的字段集**恰好集合**——已建单的普通更新
        同样落 §10.1 那组内容列 + 按名字解析的游戏；归属 / 标识列一行不碰；
        `hall_price`（打手价）与客户价语义照 D-0307（价走 `total_price`，键忽略）。
        判别力：把更新支 `update_fields` 里的任一内容列摘掉 ⇒ 对应断言红。"""
        event = self._event(
            kind="order.update",
            occurred_at=self.T2,
            hall_price="123.45",
            remark="更新支改过的备注",
            project_text="更新支改过的项目名",
            customer_phone="更新支改过的电话",
            customer_qq="更新支改过的QQ",
            content="更新支改过的需求",
            game_name=self.other_game.name,
        )
        # ⛔ 载荷里的 `total_price` 键不被采用（D-0307：下家客户价只跟发单价）。
        event["payload"]["total_price"] = "999.00"
        # 载荷里塞进**不该被消费**的键：它们即使存在也不得影响本地那一行。
        event["payload"].update({
            "order_no": "被对端顶着写的单号",
            "source": "被对端顶着写的来源快照",
            "sales_agent": "被对端顶着写的销售快照",
            "sales_user": self.taker.pk,
            "order_source": "被对端顶着写的来源字典",
            "created_at": "1999-01-01T00:00:00+00:00",
        })
        result = apply_events([event], binding=self.binding)

        self.assertEqual(result.applied, 1, result)
        self.order.refresh_from_db()
        # 夹具单在待抢单（池内态）⇒ 入站 IN_PROGRESS 被 D-0364 池内豁免保住
        # （状态照旧 applied、不进 skipped；⚠️ 不是「状态照抄」档）。
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        # 客户价 = 载荷 hall_price（123.45），⛔ 不是载荷里那个被忽略的 total_price（999.00）；
        # 打手价一格不动（D-0307）。
        self.assertEqual(self.order.total_price, Decimal("123.45"))
        self.assertIsNone(self.order.hall_price)
        # ⭐ D-0377：内容面恰好这六列跟着落（扩字段后的恰好集合）。
        self.assertEqual(self.order.remark, "更新支改过的备注")
        self.assertEqual(self.order.project_text, "更新支改过的项目名")
        self.assertEqual(self.order.customer_phone, "更新支改过的电话")
        self.assertEqual(self.order.customer_qq, "更新支改过的QQ")
        self.assertEqual(self.order.content, "更新支改过的需求")
        self.assertEqual(self.order.game_id, self.other_game.pk)
        # 分组限制的几列逐条钉住（⛔ 不是"没报错"）。
        self.assertEqual(self.order.order_no, LOCAL_NO)
        self.assertEqual(self.order.source_order_no, PEER_NO)
        self.assertEqual(self.order.source, "原来源快照")
        self.assertEqual(self.order.sales_agent, "原销售快照")
        self.assertEqual(self.order.sales_user_id, self.kefu.pk)
        self.assertIsNone(self.order.order_source_id)
        self.assertEqual(self.order.created_by, "原创建人快照")

    def test_the_same_timestamp_is_a_benign_stale_skip(self):
        """⚠️ 相等 = 同一条事件重投 ⇒ `stale`（⛔ 不许判成"应用"，否则撤单会写第二条日志）。"""
        result = apply_events(
            [self._event(status=OrderStatus.COMPLETED, occurred_at=self.T1)],
            binding=self.binding,
        )
        self.assertEqual(result.applied, 0, result)
        self.assertEqual(result.skipped, 1, result)
        self.assertEqual(result.skipped_details[0]["code"], SKIP_STALE)
        self.assertEqual(result.skipped_details[0]["target_ref"], PEER_NO)
        # 零写入的自证：载荷里那个会改状态的值没有落库（去重真的提前返回了）。
        self.order.refresh_from_db()
        self.link.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(self.link.last_applied_at, self.T1)

    def test_an_older_event_is_a_benign_stale_skip(self):
        result = apply_events(
            [self._event(status=OrderStatus.COMPLETED, occurred_at=self.T0)],
            binding=self.binding,
        )
        self.assertEqual(result.applied, 0, result)
        self.assertEqual(result.skipped, 1, result)
        self.assertEqual(result.skipped_details[0]["code"], SKIP_STALE)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)

    def test_an_empty_last_applied_at_is_treated_as_older_than_any_event(self):
        """⭐ C4（2026-09-26 修复波）：`last_applied_at` 为空（P1 建的行）⇒ 视为比任何事件都旧 ⇒ 应用。

        判别力：把「已有单 ⇒ 一律按 `occurred_at <= last_applied_at` 判旧」写成无 `is not None`
        守卫的真值比较（或把空值当"最新"）⇒ 本用例的 `applied` 断言红。
        """
        self.link.last_applied_at = None
        self.link.save(update_fields=["last_applied_at"])

        result = apply_events(
            [self._event(status=OrderStatus.IN_PROGRESS, occurred_at=self.T0)],
            binding=self.binding,
        )

        self.assertEqual(result.applied, 1, result)
        self.assertEqual(result.skipped, 0, result)
        self.order.refresh_from_db()
        self.link.refresh_from_db()
        # ⚠️ P4 起接手类落地即未分配（业务方）：本条事件是 `order.assign`。
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(self.link.last_applied_at, self.T0)

    def test_a_missing_time_on_an_existing_order_is_a_hard_skip(self):
        """已有单但事件没带时刻 ⇒ 无法裁决新旧 ⇒ 硬跳过 `bad_shape`（spec §16.3 第 4 条）。"""
        result = apply_events(
            [self._event(status=OrderStatus.COMPLETED)], binding=self.binding
        )
        self.assertEqual(result.applied, 0, result)
        self.assertEqual(result.skipped, 1, result)
        self.assertEqual(result.skipped_details[0]["code"], SKIP_BAD_SHAPE)
        self.order.refresh_from_db()
        self.link.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(self.link.last_applied_at, self.T1)

    def test_a_non_iso_time_is_a_hard_skip(self):
        """不是可解析 ISO 时刻 ⇒ 同样是 `bad_shape`（⛔ 不许当成"很旧"而静默丢弃）。"""
        event = self._event(status=OrderStatus.COMPLETED, occurred_at=self.T2)
        event["occurred_at"] = "不是时刻"
        result = apply_events([event], binding=self.binding)
        self.assertEqual(result.applied, 0, result)
        self.assertEqual(result.skipped_details[0]["code"], SKIP_BAD_SHAPE)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)

    def test_a_naive_time_is_a_hard_skip_not_a_500(self):
        """无时区的 ISO 串无法与库里的 aware 时刻比较 ⇒ 硬跳过（⛔ 不许冒成 500）。"""
        event = self._event(status=OrderStatus.COMPLETED, occurred_at=self.T2)
        event["occurred_at"] = "2026-09-26T11:00:00"
        result = apply_events([event], binding=self.binding)
        self.assertEqual(result.applied, 0, result)
        self.assertEqual(result.skipped_details[0]["code"], SKIP_BAD_SHAPE)

    def test_the_time_is_read_from_the_event_and_not_from_the_payload(self):
        """事件级字段的**读点**：顶层给了新时刻 ⇒ 应用；只有 `payload` 里有 ⇒ 硬跳过。

        判别力：把读点改成 `payload.get("occurred_at")`，本用例的第一段会变成 `bad_shape`、
        第二段会变成"应用"——两段的取值同时反过来。
        """
        # 第一段：顶层是新时刻、`payload` 里塞一个**旧**时刻 ⇒ 按顶层裁决（应用）。
        with_top = self._event(status=OrderStatus.COMPLETED, occurred_at=self.T2)
        with_top["payload"]["occurred_at"] = self.T0.isoformat()
        first = apply_events([with_top], binding=self.binding)
        self.assertEqual(first.applied, 1, first)
        self.order.refresh_from_db()
        # ⚠️ P4 起接手类落地即未分配（业务方）：本条事件是 `order.assign`。
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)

        # 第二段：只有 `payload` 里有新时刻、顶层没有 ⇒ 仍然是 `bad_shape`。
        self.order.status = OrderStatus.WAITING_GRAB
        self.order.save(update_fields=["status"])
        payload_only = self._event(status=OrderStatus.COMPLETED)
        payload_only["payload"]["occurred_at"] = self.T2.isoformat()
        second = apply_events([payload_only], binding=self.binding)
        self.assertEqual(second.applied, 0, second)
        self.assertEqual(second.skipped_details[0]["code"], SKIP_BAD_SHAPE)

    # ── ①′ 水位自检（⭐ B5 / L-10）───────────────────────
    def test_the_watermark_is_the_event_time_and_a_future_jump_logs_a_warning(self):
        """⭐ B5 核实口径：`last_applied_at` 写的就是**事件时刻**（本用例钉值）；
        水位比本机时钟超前超 1 小时 ⇒ `logger.warning` 一条（**只报不拦**：事件照常应用，
        拒收会把「对端钟快」变成「事件全被跳过」，比失灵的对账更糟）。"""
        from django.utils import timezone as dj_tz

        far_future = dj_tz.now() + timedelta(hours=3)
        with self.assertLogs("apps.business.peer_inbound", level="WARNING") as logs:
            result = apply_events(
                [self._event(occurred_at=far_future)], binding=self.binding
            )
        self.assertEqual(result.applied, 1, result)
        self.link.refresh_from_db()
        self.assertEqual(self.link.last_applied_at, far_future)
        self.assertTrue(
            any("超前" in line for line in logs.output),
            f"水位超前没有告警：{logs.output}",
        )

    def test_the_watermark_helper_warns_on_a_regression(self):
        """⭐ B5 的倒退那一腿：今天的 stale 闸把它结构性挡死（`occurred_at <= 水位` 先跳过）
        ⇒ 这条腿直接钉在辅助函数上（给将来新增的水位写点站岗）。"""
        from apps.business.peer_inbound import _warn_watermark_anomaly

        with self.assertLogs("apps.business.peer_inbound", level="WARNING") as logs:
            _warn_watermark_anomaly(
                self.T1, self.T0, now=self.T1, binding=self.binding,
                peer_order_no=PEER_NO,
            )
        self.assertTrue(
            any("倒退" in line for line in logs.output),
            f"水位倒退没有告警：{logs.output}",
        )

    # ── ② 无 link：create 或 unknown_order ────────────────────────────────
    def test_a_grab_for_an_order_we_never_received_still_creates_it(self):
        """指派那次丢了、抢单这次到 ⇒ 照样落单（不漏单，spec §16.3 末表）。"""
        result = apply_events(
            [self._event(status=OrderStatus.IN_PROGRESS, occurred_at=self.T2,
                         order_no="尚未收到的单", kind="order.grab")],
            binding=self.binding,
        )
        self.assertEqual(result.applied, 1, result)
        self.assertEqual(result.skipped, 0, result)
        order = Order.objects.get(source_order_no="尚未收到的单")
        # ⚠️ P4 起接手类落地即未分配（业务方）：本条事件是 `order.grab`。
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        link = order.peer_links.get()
        self.assertEqual(link.upstream_binding_id, self.binding.pk)
        self.assertEqual(link.last_applied_at, self.T2)

    def test_an_assign_without_a_time_still_creates_the_order(self):
        """首单**丢了时刻**也照常落单（⛔ 不许因为一个时间戳把首单拒掉），`last_applied_at` 留空。"""
        result = apply_events(
            [self._event(status=OrderStatus.UNASSIGNED, order_no="没有时刻的首单")],
            binding=self.binding,
        )
        self.assertEqual(result.applied, 1, result)
        order = Order.objects.get(source_order_no="没有时刻的首单")
        self.assertIsNone(order.peer_links.get().last_applied_at)

    def test_withdraw_for_an_order_we_never_received_is_a_hard_skip(self):
        """撤单指向一张本地没有的单 ⇒ `unknown_order`（发送侧要记失败，spec §16.3 末表）。"""
        result = apply_events(
            [self._withdraw(reason="deleted", occurred_at=self.T2,
                            order_no="从未收到的单")],
            binding=self.binding,
        )
        self.assertEqual(result.applied, 0, result)
        self.assertEqual(result.skipped, 1, result)
        self.assertEqual(result.skipped_details[0]["code"], SKIP_UNKNOWN_ORDER)
        self.assertFalse(Order.objects.filter(source_order_no="从未收到的单").exists())
        self.assertFalse(OperationLog.objects.filter(action="order.peer_withdraw").exists())

    # ── ③ 撤单落库 ────────────────────────────────────────────────────────
    def test_withdraw_sets_refunded_and_writes_the_verbatim_reason(self):
        result = apply_events(
            [self._withdraw(reason="taker_removed", occurred_at=self.T2,
                            actor_name="测试客服甲")],
            binding=self.binding,
        )
        self.assertEqual(result.applied, 1, result)
        self.assertEqual(result.skipped_details, [])
        self.order.refresh_from_db()
        self.link.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)
        self.assertEqual(self.link.last_applied_at, self.T2)
        row = OperationLog.objects.filter(action="order.peer_withdraw").latest("id")
        self.assertEqual(row.actor, "测试客服甲")
        self.assertEqual(
            row.summary,
            f"【测试客服甲】将订单状态改为：已退单，原因：发单方将订单接手人移除（订单 {PEER_NO}）",
        )
        self.assertEqual(row.reason, "发单方将订单接手人移除")
        self.assertEqual(row.to_value, "已退单")
        self.assertEqual(row.order_no, LOCAL_NO)
        self.assertEqual(row.source_order_no, PEER_NO)
        self.assertEqual(row.target_kind, "order")
        self.assertFalse(row.actor_id)          # 机器来源：本地没有人在操作

    def test_a_second_identical_withdraw_writes_no_second_log(self):
        """同一事件重投 ⇒ `stale`，⛔ 不许写第二条撤单日志（P1 的"重投只落一次"同族）。"""
        withdraw = self._withdraw(reason="taker_removed", occurred_at=self.T2)
        apply_events([withdraw], binding=self.binding)
        result = apply_events([withdraw], binding=self.binding)

        self.assertEqual(result.skipped_details[0]["code"], SKIP_STALE)
        self.assertEqual(
            OperationLog.objects.filter(action="order.peer_withdraw").count(), 1
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)

    def test_the_withdraw_uses_the_event_level_actor_and_falls_back_to_the_account(self):
        """`{昵称}` 取事件级 `actor_name`；缺失时回落绑定里的上家子账号（spec §16.2 / Ruling 1）。"""
        apply_events(
            [self._withdraw(reason="refunded", occurred_at=self.T2, actor_name=None)],
            binding=self.binding,
        )
        row = OperationLog.objects.filter(action="order.peer_withdraw").latest("id")
        self.assertEqual(row.actor, "peer-account-a")

    def test_the_withdraw_reason_is_read_from_the_event_and_not_from_the_payload(self):
        """`withdraw_reason` 只认事件级：塞进 `payload` 不算（⇒ `bad_shape`，一字不落库）。"""
        event = self._withdraw(reason="unused", occurred_at=self.T2)
        del event["withdraw_reason"]
        event["payload"]["withdraw_reason"] = "taker_removed"
        result = apply_events([event], binding=self.binding)

        self.assertEqual(result.applied, 0, result)
        self.assertEqual(result.skipped_details[0]["code"], SKIP_BAD_SHAPE)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertFalse(OperationLog.objects.filter(action="order.peer_withdraw").exists())

    def test_an_unknown_withdraw_reason_is_a_hard_skip(self):
        """原因不在三个枚举里 ⇒ `bad_shape`，⛔ 不自己编中文文案（spec §16.2）。"""
        result = apply_events(
            [self._withdraw(reason="no_such_reason", occurred_at=self.T2)],
            binding=self.binding,
        )
        self.assertEqual(result.applied, 0, result)
        self.assertEqual(result.skipped_details[0]["code"], SKIP_BAD_SHAPE)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertFalse(OperationLog.objects.filter(action="order.peer_withdraw").exists())

    def test_the_withdraw_runs_the_refund_hook_and_clears_the_performance_rows(self):
        """⭐ 撤单**直接写状态列 + 显式跑退单钩子**（2026-10-22 P0-7 / `D-0288`，本用例改准）。

        ⚠️ **原文**（本用例曾名 `test_the_withdraw_leaves_the_performance_rows_byte_identical`）
        钉的是「撤单让绩效行**逐字节不变**」，理由是「同步一条上家撤单不该顺带清本地绩效」。
        那个口径在钱包成为发薪唯一出口之后**站不住了**：每一张完成单在完成那一刻就已自动入账
        （`settled_by="system"`，钱进了打手钱包）⇒ 撤单只写状态列 = **钱留在打手余额里**。
        今天改钉的是**反方向**（两列清 0 + 留痕），并且**钩子失败要整笔回滚**那条在
        `tests/test_settle_p1_peer_withdraw.py` 里钉。

        判别力（两个方向各一条）：
        ① 把 `on_order_refunded` 那一跳删掉 ⇒ 下面的 `temp_amount` 断言红（停在 `20.00` / `12.00`）；
        ② 把传进去的来源态写成 `REFUNDED`（= 赋值**之后**的状态）⇒ 钩子判成「重跑」⇒
           第 1.5 步的留痕与第 1.6 步的回冲全跳过 ⇒ 留痕那条断言红。
        """
        main = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=self.taker,
            temp_amount=Decimal("20.00"), bonus_amount=Decimal("8.00"),
        )
        sales = Performance.objects.create(
            order=self.order, emp_type=EmpType.SALES, user=self.kefu,
            temp_amount=Decimal("12.00"), bonus_amount=Decimal("12.00"),
        )
        # 夹具自证：真的有两行、且两行的金额都不是 0（否则下面的「清零」断言没有判别力）。
        self.assertEqual(
            Performance.objects.filter(order=self.order).count(), 2
        )

        result = apply_events(
            [self._withdraw(reason="taker_removed", occurred_at=self.T2)],
            binding=self.binding,
        )

        self.assertEqual(result.applied, 1, result)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)
        # 打手行：两个金额列清零，**行与 `user` 都保留**（退单钩子的第 2 步）。
        main.refresh_from_db()
        self.assertEqual(main.temp_amount, Decimal("0.00"))
        self.assertEqual(main.bonus_amount, Decimal("0.00"))
        self.assertEqual(main.user_id, self.taker.pk)
        # 客服行：第 2.7 步也清 0，并留下一条 `MANUAL amount=0` 的留痕（带原值）。
        sales.refresh_from_db()
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        trace = PerformanceAdjustment.objects.get(performance=sales)
        self.assertEqual(trace.kind, PerformanceAdjustment.Kind.MANUAL)
        self.assertEqual(trace.amount, Decimal("0.00"))
        self.assertIn("退单清空客服行", trace.reason)

    # ── ④ 状态白名单仍然是硬跳过 ──────────────────────────────────────────
    def test_an_unknown_status_on_an_existing_order_is_not_applied(self):
        """⛔ 未知状态**响亮失败**（`unknown_status`）：不落、不回落到默认态、不动已有行。"""
        result = apply_events(
            [self._event(status="NOT_A_STATUS", occurred_at=self.T2)],
            binding=self.binding,
        )
        self.assertEqual(result.applied, 0, result)
        self.assertEqual(result.skipped_details[0]["code"], SKIP_UNKNOWN_STATUS)
        self.assertIn("NOT_A_STATUS", result.skipped_details[0]["reason"])
        self.order.refresh_from_db()
        self.link.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(self.link.last_applied_at, self.T1)

    def test_a_missing_game_name_on_an_existing_order_is_a_hard_skip(self):
        """未知 / 缺失的游戏名在 T5 之前仍是硬跳过（`bad_shape`，理由同 P1）。"""
        event = self._event(occurred_at=self.T2)
        del event["payload"]["game_name"]
        result = apply_events([event], binding=self.binding)
        self.assertEqual(result.applied, 0, result)
        self.assertEqual(result.skipped_details[0]["code"], SKIP_BAD_SHAPE)
        self.order.refresh_from_db()
        self.assertEqual(self.order.game_id, self.game.pk)
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)

    # ── ⑤ skipped_details 形状与来源字典纪律 ──────────────────────────────
    def test_skipped_details_carry_exactly_the_three_keys_for_every_code(self):
        """一条 `skipped_details` = `{"target_ref", "code", "reason"}`，四种 code 各来一条。"""
        events = [
            # `stale`
            self._event(status=OrderStatus.COMPLETED, occurred_at=self.T1),
            # `bad_shape`（已有单、没有时刻）
            self._event(status=OrderStatus.COMPLETED),
            # `unknown_status`
            self._event(status="NOT_A_STATUS", occurred_at=self.T2),
            # `unknown_order`
            self._withdraw(reason="deleted", occurred_at=self.T2, order_no="没有的单"),
        ]
        result = apply_events(events, binding=self.binding)

        self.assertEqual(result.applied, 0, result)
        self.assertEqual(result.skipped, 4, result)
        self.assertEqual(
            [d["code"] for d in result.skipped_details],
            [SKIP_STALE, SKIP_BAD_SHAPE, SKIP_UNKNOWN_STATUS, SKIP_UNKNOWN_ORDER],
        )
        for detail in result.skipped_details:
            self.assertEqual(set(detail), {"target_ref", "code", "reason"})
            self.assertTrue(detail["target_ref"])
            self.assertTrue(detail["reason"])

    def test_a_non_object_event_is_a_bad_shape_skip(self):
        """`events` 是外部来的数组 ⇒ 非对象项跳过（⛔ 不是 500），`target_ref` 给空串。"""
        result = apply_events(["nope", 7], binding=self.binding)
        self.assertEqual(result.applied, 0, result)
        self.assertEqual(result.skipped, 2, result)
        for detail in result.skipped_details:
            self.assertEqual(detail["code"], SKIP_BAD_SHAPE)
            self.assertEqual(detail["target_ref"], "")

    def test_a_write_that_updates_reuses_the_existing_source_row(self):
        """更新一支**不建来源字典**（来源是首单的归属，spec §10.4 末段）。"""
        before = OrderSource.objects.count()
        apply_events([self._event(occurred_at=self.T2)], binding=self.binding)
        self.assertEqual(OrderSource.objects.count(), before)
        self.assertIsNone(Order.objects.get(pk=self.order.pk).order_source_id)

    def test_a_batch_mixes_applied_and_skipped_without_losing_the_counts(self):
        """一批里既有应用又有跳过时，计数与明细逐条对得上。"""
        result = apply_events(
            [
                self._event(occurred_at=self.T0),                    # stale
                self._event(status=OrderStatus.COMPLETED,
                            occurred_at=self.T2 + timedelta(minutes=1)),  # 应用
                self._event(status=OrderStatus.COMPLETED,
                            occurred_at=self.T2),                    # stale（刚应用过）
            ],
            binding=self.binding,
        )
        self.assertEqual(result.applied, 1, result)
        self.assertEqual(result.skipped, 2, result)
        self.assertEqual(len(result.skipped_details), 2)
        self.order.refresh_from_db()
        # ⚠️ P4 起接手类落地即未分配（业务方）：被应用的那条也是 `order.assign`。
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)


# ── ⭐ 审计批 A：入站正确性三把闸 ────────────────────────────────
class PeerInboundCorrectnessGatesTests(TenantTestCase):
    """A1 / H-1（remark/content 类型闸）、A2 / M-2（未知 kind 接受集闸）、A7 / L-5（负价）。

    ⚠️ 判据一律打在**数据库行**上（refresh_from_db / 重取），⛔ 不是看内存对象；
    ⚠️ 「无重试风暴」的判据 = `skipped == 0`（发送侧对无明细的批次按成功记账，
    `peer_worker._hard_skip_reason` 口径）——畸形展示字段**不该**让对端退避重投。
    """

    T1 = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
    T2 = datetime(2026, 9, 26, 11, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.game = Game.objects.create(name="审计批闸夹具游戏")
        self.kefu = get_user_model().objects.create_user(
            username="audit-gate-kefu", password="pw-syn"
        )
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室乙",
            peer_base_url="https://peer-gate.test.invalid",
            peer_account="peer-account-gate",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-gate-local.test.invalid",
            bound_by=self.kefu,
        )
        self.order = Order.objects.create(
            order_no="20260926000000000041",
            game=self.game,
            status=OrderStatus.WAITING_GRAB,
            remark="原备注",
            content="原需求",
            source_order_no="GATE-1",
        )
        self.link = PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.binding,
            peer_order_no="GATE-1",
            last_applied_at=self.T1,
        )

    def _event(self, *, ref="GATE-1", kind="order.assign", occurred_at=None, **payload):
        base = {
            "status": OrderStatus.IN_PROGRESS,
            "hall_price": "50.00",
            "remark": "新备注",
            "game_name": self.game.name,
            "content": "新需求",
        }
        base.update(payload)
        event = {"kind": kind, "target_ref": ref, "payload": base}
        if occurred_at is not None:
            event["occurred_at"] = occurred_at.isoformat()
        return event

    # ── A1 / H-1：remark / content 类型闸 ──────────────────────────────────
    def test_malformed_remark_on_the_update_branch_is_treated_as_absent(self):
        """畸形 `remark`（dict）⇒ 事件照常应用、字段按「没带」写空、**零跳过明细**。"""
        outcome = apply_events(
            [self._event(occurred_at=self.T2, remark={"bad": 1}, content=[1, 2])],
            binding=self.binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped, 0, outcome)
        self.assertEqual(outcome.skipped_details, [])
        row = Order.objects.get(pk=self.order.pk)
        # ⚠️ 与「缺键 / None」的既有口径逐字一致：写空串（⛔ 不是半截 dict 进列）。
        self.assertEqual(row.remark, "")
        self.assertEqual(row.content, "")
        # 事件其余字段照常应用（状态随接手类落「未分配」，P4 口径）。
        self.assertEqual(row.status, OrderStatus.UNASSIGNED)

    def test_malformed_remark_on_the_create_branch_is_treated_as_absent(self):
        """建单支同闸：首条事件带畸形 `remark` ⇒ 落单、字段空、零跳过。"""
        outcome = apply_events(
            [{
                "kind": "order.update",
                "target_ref": "GATE-NEW-1",
                "occurred_at": self.T2.isoformat(),
                "payload": {
                    "status": OrderStatus.IN_PROGRESS,
                    "hall_price": "30.00",
                    "remark": {"a": [1, 2]},
                    "content": 42,
                    "game_name": self.game.name,
                },
            }],
            binding=self.binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped, 0, outcome)
        row = Order.objects.get(source_order_no="GATE-NEW-1")
        self.assertEqual(row.remark, "")
        self.assertEqual(row.content, "")
        self.assertEqual(row.hall_price, Decimal("30.00"))

    def test_a_well_formed_remark_still_lands(self):
        """对照（防闸过宽）：正常字符串 `remark` 照常覆盖。"""
        outcome = apply_events(
            [self._event(occurred_at=self.T2, remark="正常新备注")],
            binding=self.binding,
        )
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(
            Order.objects.get(pk=self.order.pk).remark, "正常新备注"
        )

    # ── A2 / M-2：未知 kind 的接受集闸 ─────────────────────────────────────
    def test_unknown_kind_with_a_status_outside_the_accepted_set_is_rejected(self):
        """未知 kind + 集外状态（`PENDING_REVIEW` 合法但下家不认）⇒ 硬跳过有台账。"""
        outcome = apply_events(
            [self._event(
                kind="order.anything", status=OrderStatus.PENDING_REVIEW,
                occurred_at=self.T2,
            )],
            binding=self.binding,
        )
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertEqual(outcome.skipped, 1, outcome)
        detail = outcome.skipped_details[0]
        self.assertEqual(detail["code"], "status_not_accepted_for_kind")
        self.assertEqual(detail["target_ref"], "GATE-1")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)  # 一个字不改

    def test_unknown_kind_with_an_accepted_status_still_applies(self):
        """集内四值照常（既有 P3 语义）：未知 kind + `ISSUE` ⇒ 照抄落库。"""
        outcome = apply_events(
            [self._event(kind="order.anything", status=OrderStatus.ISSUE,
                         occurred_at=self.T2)],
            binding=self.binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(
            Order.objects.get(pk=self.order.pk).status, OrderStatus.ISSUE
        )

    def test_first_event_update_with_a_status_outside_the_set_writes_nothing(self):
        """建单支同闸：首条 `order.update` + 集外状态 ⇒ 跳过，**零写入**（连游戏行都不建）。"""
        games_before = Game.objects.count()
        outcome = apply_events(
            [self._event(
                ref="GATE-NEW-2", kind="order.update",
                status=OrderStatus.PENDING_REVIEW, occurred_at=self.T2,
            )],
            binding=self.binding,
        )
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertEqual(
            outcome.skipped_details[0]["code"], "status_not_accepted_for_kind"
        )
        self.assertFalse(Order.objects.filter(source_order_no="GATE-NEW-2").exists())
        self.assertEqual(Game.objects.count(), games_before)   # 被拒事件零写入

    def test_known_kinds_are_not_touched_by_the_new_gate(self):
        """回归：`withdraw`（显式落 REFUNDED）与接手类（显式落 UNASSIGNED）不受闸管。"""
        withdraw = self._event(
            kind="order.withdraw", status=OrderStatus.WAITING_GRAB,
            occurred_at=self.T2,
        )
        withdraw["withdraw_reason"] = "taker_removed"      # 事件级枚举（spec §16.2）
        outcome = apply_events([withdraw], binding=self.binding)
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(
            Order.objects.get(pk=self.order.pk).status, OrderStatus.REFUNDED
        )

    # ── D-0306（终裁）：订单流负价放行 ─────────────────────────
    def test_an_explicit_zero_price_lands_as_zero(self):
        """⭐ L-4（2026-10-26）：`payload.hall_price` 显式传 `0` ⇒ 客户价落 **0.00**。

        旧写法 `payload.get("hall_price") or order.total_price` 把 0 当真值假 ⇒ 保留旧价
        （本夹具 `total_price=10.00`，旧写法会停在 10.00）。判据必须是 `is None`。
        """
        outcome = apply_events(
            [self._event(occurred_at=self.T2, hall_price=0)],
            binding=self.binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)
        row = Order.objects.get(pk=self.order.pk)
        self.assertEqual(row.total_price, Decimal("0.00"))

    def test_a_negative_hall_price_lands_on_the_order_flow(self):
        """⭐ D-0306：**订单流**的负价照落（不再整条跳过）。

        旧口径（ A7 / L-5）判 `negative_price_rejected`；
        业务方终裁「订单流负价放行、大厅负价不同步」⇒ 本条改钉：
        事件照常 applied，负价落 `total_price`；`hall_price` 一格不动（D-0307）。
        """
        outcome = apply_events(
            [self._event(occurred_at=self.T2, hall_price="-99999")],
            binding=self.binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped, 0, outcome)
        row = Order.objects.get(pk=self.order.pk)
        # 本条事件是**接手类**（`order.assign`）⇒ 建/更新支仍按 P3/R10 照抄发单价；
        # D-0307 只改了 `order.update` 的改价语义（那一支见
        # `tests/test_peer_p3_inbound_down.py::test_an_order_update_lands_the_price_as_the_customer_price`）。
        self.assertEqual(row.total_price, Decimal("-99999"))
        self.assertEqual(row.hall_price, Decimal("-99999"))

    def test_zero_and_positive_prices_still_apply(self):
        """对照（D-0015 的边界）：`0` 与正数照常落库。"""
        outcome = apply_events(
            [self._event(occurred_at=self.T2, hall_price="0")],
            binding=self.binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(Order.objects.get(pk=self.order.pk).hall_price, Decimal("0"))
