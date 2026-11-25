"""打手 App 按需同步群二维码（2026-10-23，业务方拍板）。

端点：`POST /api/business/my-orders/<order_no>/screenshots/sync/`
（`apps/business/api/my_orders_views.py::MyOrdersViewSet.sync_screenshots`）。
消费：`apps/business/jidanpu_upstream.py::run_pull_pics_requests`（`jidanpu_sync` 每一拍调）。

## 判据（逐条对上任务书的五条）

1. **本人 + 上家单 + 缺图 ⇒ 入队一条 `upstream.pull_pics_in`**
   `test_the_owner_of_a_linked_order_enqueues_exactly_one_pull_request`；
2. **冷却内重复调用不再入队**（正对照：过了冷却可以再入队）
   `test_a_repeat_call_inside_the_cooldown_does_not_enqueue_a_second_request`；
3. **非本人 ⇒ 拒绝，且不入队** `test_another_taker_is_rejected_and_nothing_is_enqueued`；
4. **非上家单 / 已有群二维码 ⇒ `triggered=false` 且不入队**
   `test_a_non_upstream_order_is_not_triggered_and_enqueues_nothing` /
   `test_an_order_that_already_has_a_group_qr_is_not_triggered`；
5. **已有图不重复打上游**：端点侧同上；消费侧
   `test_a_pending_request_is_skipped_when_the_group_qr_arrived_meanwhile`
   （请求排完之后群码才到 ⇒ 消费时**一次上游都不打**）。

另有三条补强（不是任务书要求，但线上那张单正卡在这里）：

- **手工请求不吃自动的 5 次额度**
  `test_the_manual_request_is_not_capped_by_the_five_automatic_attempts`
  ——某租户 2026-10-23 那张单的台账已经烧完 5 次，手工触发必须照样能拉；
- **消费真的走既有那一份拉图实现**
  `test_the_pending_request_is_consumed_by_the_existing_puller`
  （⚠️ 断言打在**请求行**的终态 + `pull_order_screenshots` 的调用参数上）；
- **单不存在 / 已软删** 与详情端点同出口（200 + `1002`）。

⚠️ 夹具一律**合成值**（E-0077）；断言一律**字面值**（E-0024）。
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_upstream, jidanpu_upstream_pics
from apps.business.models import (
    EmpType,
    Game,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamOrderLink,
    Order,
    OrderScreenshot,
    OrderStatus,
    Performance,
)
from tests.p5_auth_helpers import make_logged_in_client

# ── 合成业务值（租户内唯一、两租户取值不同 ⇒ 不用主键指认；AGENTS §5-G）──────────
NO_A = "10000000000000000021"       # 打手甲接的上家单
NO_PLAIN = "10000000000000000023"   # 打手甲接的、**没有**上家链路的单
NO_MISSING = "10000000000000000029"
GAME = "合成游戏-群二维码同步"
REMOTE_A = "3316543106026011201"
REMOTE_PLAIN = "3316543106026011203"
BASE_URL = "https://upstream-sync.test.invalid"
ACCOUNT = "upstream-sync-a"
PASSWORD = "pw-canary-sync"          # 合成 canary：⛔ 不是真密码
NODES = ("my_orders.send_to_my_system",)


def _url(order_no):
    return f"/api/business/my-orders/{order_no}/screenshots/sync/"


class _Base(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name=GAME)
        self._clients = {}
        self.client = make_logged_in_client(
            username="群码打手甲", groups=("代练员",), nodes=NODES
        )
        self._clients["群码打手甲"] = self.client
        self.taker_a = User.objects.get(username="群码打手甲")
        self.client = make_logged_in_client(
            username="群码打手乙", groups=("代练员",), nodes=NODES
        )
        self._clients["群码打手乙"] = self.client
        self.taker_b = User.objects.get(username="群码打手乙")
        # 绑定行的 `user` 是**录入人**（D-0256 改准），⛔ 不要求是接单人本人。
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.taker_a,
            jidanpu_base_url=BASE_URL,
            account=ACCOUNT,
            password=PASSWORD,
            enabled=True,
        )
        # 主夹具：打手甲接的上家单（有链路、没有群码）。
        self.order_a = self._linked_order(NO_A, self.taker_a, remote_id=REMOTE_A)
        # 测试默认身份回到打手甲。
        self.client = self._clients["群码打手甲"]

    def _order(self, order_no, user, *, price=Decimal("10.00")):
        order = Order.objects.create(
            game=self.game, order_no=order_no, status=OrderStatus.IN_PROGRESS,
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=user, temp_amount=price,
        )
        return order

    def _linked_order(self, order_no, user, *, remote_id):
        order = self._order(order_no, user)
        JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=order, upstream_order_id=remote_id,
        )
        return order

    def _screenshot(self, order, *, img_type, status=OrderScreenshot.STATUS_READY,
                    suffix="png"):
        return OrderScreenshot.objects.create(
            order=order, img_type=img_type,
            object_key=f"{self.tenant.schema_name}/orders/{order.order_no}/x-{suffix}",
            uploaded_by=self.taker_a, status=status,
        )

    def _requests(self, order_no):
        return JidanpuSyncEvent.objects.filter(
            kind=jidanpu_upstream.KIND_PULL_PICS_IN,
            target_type="order",
            target_ref=order_no,
            payload__has_key="trigger",
        )

    def _post(self, order_no):
        return self.client.post(_url(order_no), data={}, content_type="application/json")


class EnqueueTests(_Base):
    """端点的入队判据（任务书第 1–5 条的那一半）。"""

    def test_the_owner_of_a_linked_order_enqueues_exactly_one_pull_request(self):
        """① 本人 + 上家单 + 缺图 ⇒ **恰好一条** `upstream.pull_pics_in` 请求行。"""
        response = self._post(NO_A)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0)
        data = body["data"]
        self.assertIs(data["triggered"], True)
        self.assertTrue(data["reason"])
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_a).count(), 0)

        rows = self._requests(NO_A)
        self.assertEqual(rows.count(), 1)
        event = rows.get()
        self.assertEqual(event.direction, "in")
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.kind, "upstream.pull_pics_in")
        self.assertEqual(event.target_type, "order")
        self.assertEqual(event.target_ref, NO_A)
        self.assertEqual(
            event.payload["trigger"], jidanpu_upstream.PULL_PICS_TRIGGER_APP
        )
        self.assertEqual(event.payload["upstream_binding_id"], self.binding.pk)
        self.assertEqual(event.payload["requested_by_user_id"], self.taker_a.pk)
        # ⛔ 请求里一次上游都不打（拉的活是 `jidanpu_sync` 下一拍的事）。
        self.assertEqual(OrderScreenshot.objects.count(), 0)

    def test_a_repeat_call_inside_the_cooldown_does_not_enqueue_a_second_request(self):
        """② 冷却内重复调用 ⇒ 不再入队（回 `retry_after_seconds`）；过了冷却可以再入队。"""
        first = self._post(NO_A).json()["data"]
        self.assertIs(first["triggered"], True)
        second = self._post(NO_A).json()["data"]
        self.assertIs(second["triggered"], False)
        self.assertGreaterEqual(second["retry_after_seconds"], 1)
        self.assertEqual(self._requests(NO_A).count(), 1, "冷却内重复调用又排了一条")

        # 正对照：把那条请求行的时间推回冷却之外 ⇒ 第二次真的能再排一条。
        stale = timezone.now() - timedelta(
            seconds=jidanpu_upstream.PULL_PICS_REQUEST_COOLDOWN_SECONDS + 1
        )
        JidanpuSyncEvent.objects.filter(pk=self._requests(NO_A).get().pk).update(
            created_at=stale
        )
        third = self._post(NO_A).json()["data"]
        self.assertIs(third["triggered"], True)
        self.assertEqual(self._requests(NO_A).count(), 2)

    def test_another_taker_is_rejected_and_nothing_is_enqueued(self):
        """③ 非本人 ⇒ **403**（与详情端点同一句式），且一条请求都不排。"""
        self.client = self._clients["群码打手乙"]
        response = self._post(NO_A)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["msg"], "该单不是你接的，不能同步群二维码")
        self.assertEqual(self._requests(NO_A).count(), 0)
        # 反向对照（非空哨兵）：接单人自己调同一张单是 200 + triggered=true。
        self.client = self._clients["群码打手甲"]
        self.assertIs(self._post(NO_A).json()["data"]["triggered"], True)

    def test_a_non_upstream_order_is_not_triggered_and_enqueues_nothing(self):
        """④-a 没有上家链路 ⇒ `triggered=false` + 人话原因，不入队。"""
        self._order(NO_PLAIN, self.taker_a)
        data = self._post(NO_PLAIN).json()["data"]
        self.assertIs(data["triggered"], False)
        self.assertIn("不是从上家鸡蛋铺来的", data["reason"])
        self.assertEqual(self._requests(NO_PLAIN).count(), 0)

    def test_an_order_that_already_has_a_group_qr_is_not_triggered(self):
        """④-b 本地已有**可用**群二维码 ⇒ `triggered=false`，不入队（已有图不重复打上游）。"""
        self._screenshot(self.order_a, img_type=OrderScreenshot.IMG_GROUP_QR, suffix="qr")
        data = self._post(NO_A).json()["data"]
        self.assertIs(data["triggered"], False)
        self.assertIn("已经有群二维码", data["reason"])
        self.assertEqual(self._requests(NO_A).count(), 0)

    def test_a_pending_group_qr_does_not_count_as_having_one(self):
        """④-b 的反面：`status=pending`（还没认领）的那一行**不算**「已有群码」。

        ⚠️ 判据必须是「**可用的**群码」：待认领行取不到签名 URL、页面上还是空的那一格
        ⇒ 拿它挡掉同步会让打手永远看不到图。
        """
        self._screenshot(
            self.order_a, img_type=OrderScreenshot.IMG_GROUP_QR,
            status=OrderScreenshot.STATUS_PENDING, suffix="pending",
        )
        data = self._post(NO_A).json()["data"]
        self.assertIs(data["triggered"], True)
        self.assertEqual(self._requests(NO_A).count(), 1)

    def test_a_disabled_binding_is_not_triggered(self):
        """上家线停用 ⇒ 排了也没人消费 ⇒ 如实回 `triggered=false`，不入队。"""
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        data = self._post(NO_A).json()["data"]
        self.assertIs(data["triggered"], False)
        self.assertIn("已停用", data["reason"])
        self.assertEqual(self._requests(NO_A).count(), 0)

    def test_a_missing_or_soft_deleted_order_is_the_same_business_not_found(self):
        """单不存在 ⇒ 200 + `1002`（与 `order_detail` 同一出口），且不入队。"""
        missing = self._post(NO_MISSING)
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(missing.json()["code"], 1002)
        self.assertEqual(missing.json()["msg"], "记录不存在或已被删除")
        Order.objects.filter(pk=self.order_a.pk).update(
            deleted_at=datetime(2026, 10, 23, tzinfo=dt_timezone.utc)
        )
        self.assertFalse(Order.objects.filter(pk=self.order_a.pk).exists())
        soft_deleted = self._post(NO_A)
        self.assertEqual(soft_deleted.status_code, 200)
        self.assertEqual(soft_deleted.json()["code"], 1002)
        self.assertEqual(self._requests(NO_A).count(), 0)


class ConsumeTests(_Base):
    """消费侧：`run_pull_pics_requests` 真的调既有的拉图实现、并把结论写回请求行。"""

    def _enqueue(self):
        self.assertIs(self._post(NO_A).json()["data"]["triggered"], True)
        return self._requests(NO_A).get()

    def test_the_pending_request_is_consumed_by_the_existing_puller(self):
        """请求被消费 ⇒ 请求行落 `done` + `processed_at`；拉图走**同一份**实现。"""
        event = self._enqueue()
        pulled = jidanpu_upstream_pics.PulledShot(
            object_key="synth/qr.png", img_type=OrderScreenshot.IMG_GROUP_QR,
        )
        outcome = jidanpu_upstream_pics.PullOutcome(pulled=(pulled,))
        client = object()
        with mock.patch.object(
            jidanpu_upstream_pics, "pull_order_screenshots", return_value=outcome,
        ) as pull:
            handled = jidanpu_upstream.run_pull_pics_requests(
                self.binding, now=timezone.now(), client=client,
            )
        self.assertEqual(handled, 1)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertIn("新存 1 张", event.reason)
        self.assertIsNotNone(event.processed_at)
        # 判据打在**调用参数**上：复用的是既有那一个拉图函数（⛔ 不是第二条实现）。
        pull.assert_called_once()
        kwargs = pull.call_args.kwargs
        self.assertIs(pull.call_args.args[0], client)
        self.assertEqual(kwargs["order"].pk, self.order_a.pk)
        self.assertEqual(kwargs["remote_order_id"], REMOTE_A)
        self.assertEqual(kwargs["uploaded_by"].pk, self.taker_a.pk)

    def test_the_manual_request_is_not_capped_by_the_five_automatic_attempts(self):
        """线上那张单（已经自动失败 5 次）点开详情后**照样**能拉一次。

        前提自证：先把 5 条「自动尝试」台账铺满（`payload` 里**没有** `trigger`），
        然后手工请求 + 消费 ⇒ 仍然调到拉图函数、请求行落 `done`。
        ⚠️ 自动那一轮的计数必须**看不见**手工请求行（`payload.trigger` 缺席的那些才计数），
        否则打手点一次就把自动额度烧掉、第 6 次自动轮起又会把手工请求当成「到顶」。
        """
        for index in range(jidanpu_upstream.PULL_PICS_MAX_ATTEMPTS):
            JidanpuSyncEvent.objects.create(
                direction="in", kind=jidanpu_upstream.KIND_PULL_PICS_IN,
                status="skipped", target_type="order", target_ref=NO_A,
                reason=f"上家这张单还没有可存的图（第 {index + 1}/5 次自动尝试）",
                payload={"upstream_binding_id": self.binding.pk},
            )
        event = self._enqueue()
        outcome = jidanpu_upstream_pics.PullOutcome(
            pulled=(
                jidanpu_upstream_pics.PulledShot(
                    object_key="synth/qr.png",
                    img_type=OrderScreenshot.IMG_GROUP_QR,
                ),
            )
        )
        with mock.patch.object(
            jidanpu_upstream_pics, "pull_order_screenshots", return_value=outcome,
        ) as pull:
            jidanpu_upstream.run_pull_pics_requests(
                self.binding, now=timezone.now(), client=object(),
            )
        pull.assert_called_once()
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_a_pending_request_is_skipped_when_the_group_qr_arrived_meanwhile(self):
        """⑤ 请求排完之后群码才到 ⇒ 消费时**一次上游都不打**，请求行落 `skipped`。"""
        event = self._enqueue()
        self._screenshot(self.order_a, img_type=OrderScreenshot.IMG_GROUP_QR, suffix="qr")
        with mock.patch.object(
            jidanpu_upstream_pics, "pull_order_screenshots",
        ) as pull:
            jidanpu_upstream.run_pull_pics_requests(
                self.binding, now=timezone.now(), client=object(),
            )
        pull.assert_not_called()
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertIn("已经有群二维码", event.reason)

    def test_a_failed_pull_is_recorded_on_the_request_row_and_never_raised(self):
        """拉图失败只落请求行（人话带「手动触发」），⛔ 不向上抛（不拖垮本轮其它请求）。"""
        event = self._enqueue()
        with mock.patch.object(
            jidanpu_upstream_pics, "pull_order_screenshots",
            side_effect=jidanpu_upstream_pics.PicDownloadError("下家图片地址回 HTTP 500"),
        ):
            handled = jidanpu_upstream.run_pull_pics_requests(
                self.binding, now=timezone.now(), client=object(),
            )
        self.assertEqual(handled, 1)
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertIn("手动触发", event.reason)
        self.assertIn("PicDownloadError", event.reason)
