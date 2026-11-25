"""上下游 P3 Task 5：投递内核认**上行**绑定（spec §7.2 / §16.6）。

P2 的投递内核（`apps/business/peer_worker.py`）里，`_attempt` 假定每条出站事件都属于一条
`PeerDownstreamBinding`。P3 让同步变双向：Task 2 的 `enqueue_upstream_status` 会写出
`binding_role="upstream"` 的 `order.update` 事件 ⇒ 内核必须按 `binding_role` **显式分叉**。

本文件钉五件事（缺一条就漏一种错法）：

① **推得上家**：上行事件用**上游绑定**的 `peer_base_url` / `token_out` 推，载荷只带状态；
② **失效判据按方向不同**：上游绑定 `enabled=False` 或 `deleted_at` 非空 ⇒ 事件转终态
   `failed`（结果名 `dropped`），⛔ 不刷健康度、⛔ 一个 HTTP 都不发；
③ **成功记账两个方向同形**：`last_ok_at = now` + `consecutive_failures = 0`；
④ **失败**记账写 `last_error` / `last_error_at` / `consecutive_failures`，但
   **自动停绑只对下行**——⛔ 不给上行加停绑（业务方没裁定过，Task 9 的残差）；
   `PeerUpstreamBinding` **没有** `state` / `revoked_at` 两列，写成上行的分支会 `AttributeError`；
⑤ **认不出的 `binding_role` 响亮抛** `ValueError`（⛔ 不写"先当下游、找不到再当上游"）。

⚠️ 判据一律钉**字面值**（`"PENDING_REVIEW"` / `"绑定已解除/失效，不再投递"`），
⛔ 不与实现里的常量对拍（E-0024：自指断言在被断言的常量改坏时不会红）。
⚠️ `mock.patch` 打在**被查找的那个模块**（`apps.business.peer_worker.push_events`），
⛔ 不是定义它的 `peer_client`（E-0029）。
⚠️ 夹具一律**合成值**（域名 `*.test.invalid`、上家单号 `UP-SYN-P3-1`），⛔ 不含真实租户数据。
⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
"""
from __future__ import annotations

import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import peer_worker
from apps.business.models import PeerSyncEvent, PeerUpstreamBinding
from apps.business.peer_client import PeerUnreachable
from apps.business.peer_sync import hash_token, new_token
from apps.business.peer_worker import deliver_one

#: 上家那一侧的单号（上行事件的 `target_ref`），与本仓合成单号形状一致。
UPSTREAM_ORDER_NO = "UP-SYN-P3-1"


class _UpstreamDeliveryFixture(TenantTestCase):
    """一条**启用且未解绑**的上游绑定 + 一条 `pending` 的上行 `order.update` 事件。

    ⚠️ `PeerUpstreamBinding` 的 `peer_name` / `peer_base_url` / `peer_account` /
    `token_in_hash` / `token_out` / `local_base_url` / `bound_by` **全部非空** ⇒ 少给一个
    就 `IntegrityError`，夹具按字段形状给全（照 `tests/test_peer_worker.py` 的租户夹具）。
    ⚠️ 令牌两列按字段口径给：上家发给我的只存哈希、我发给上家的存明文。
    """

    def setUp(self):
        self.operator = get_user_model().objects.create_user(
            username="peer-p3-up-operator", password="pw-peer-p3-up"
        )
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家乙",
            peer_base_url="https://peer-up.test.invalid",
            peer_account="peer-account-up",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-local.test.invalid",
            bound_by=self.operator,
        )
        self.now = timezone.now()
        self.upstream_event = self._event()

    def _event(self, *, kind="order.update", target_ref=UPSTREAM_ORDER_NO, **over):
        return PeerSyncEvent.objects.create(
            binding_role="upstream",
            binding_id=str(self.upstream_binding.pk),
            direction="out",
            kind=kind,
            target_type="order",
            target_ref=target_ref,
            status=PeerSyncEvent.Status.PENDING,
            payload={"status": "PENDING_REVIEW"},
            meta={"occurred_at": self.now.isoformat()},
            idempotency_key=uuid.uuid4().hex,
            **over,
        )


class UpstreamDeliveryTests(_UpstreamDeliveryFixture):
    """① / ③ 上行事件的正常投递与成功记账。"""

    def test_an_upstream_event_pushes_to_the_upstream_binding(self):
        with mock.patch("apps.business.peer_worker.push_events", return_value={}) as push:
            outcome = deliver_one(self.upstream_event.pk, now=self.now)
        self.assertEqual(outcome, "done")
        args, kwargs = push.call_args
        self.assertEqual(args[0], self.upstream_binding.peer_base_url)   # 推到上家
        self.assertEqual(args[1], self.upstream_binding.token_out)       # 带**我发给上家**的令牌
        self.assertEqual(args[2][0]["kind"], "order.update")
        # 上家要能用 `target_ref` 直接查到它自己那张单（⛔ 不是本地下家单号）。
        self.assertEqual(args[2][0]["target_ref"], UPSTREAM_ORDER_NO)
        # 业务方：「下家只回传状态」⇒ 载荷只有那一格。
        self.assertEqual(args[2][0]["payload"], {"status": "PENDING_REVIEW"})

    def test_upstream_success_writes_health(self):
        """成功记账与下行同形：`last_ok_at = now` + 清零连续失败。"""
        self.upstream_binding.consecutive_failures = 3
        self.upstream_binding.last_error = "上一次的旧失败"
        self.upstream_binding.save(
            update_fields=["consecutive_failures", "last_error"]
        )

        with mock.patch("apps.business.peer_worker.push_events", return_value={}):
            result = deliver_one(self.upstream_event.pk, now=self.now)

        self.assertEqual(result, "done")
        self.upstream_event.refresh_from_db()
        self.upstream_binding.refresh_from_db()
        self.assertEqual(self.upstream_event.status, PeerSyncEvent.Status.DONE)
        self.assertEqual(self.upstream_event.processed_at, self.now)
        self.assertEqual(self.upstream_binding.last_ok_at, self.now)
        self.assertEqual(self.upstream_binding.consecutive_failures, 0)


class UpstreamInvalidBindingTests(_UpstreamDeliveryFixture):
    """② 失效判据按方向不同：上游 = `deleted_at` 非空或 `enabled=False`。"""

    def test_a_disabled_upstream_binding_drops_the_event(self):
        self.upstream_binding.enabled = False
        self.upstream_binding.save(update_fields=["enabled"])

        with mock.patch("apps.business.peer_worker.push_events") as push:
            result = deliver_one(self.upstream_event.pk, now=self.now)

        self.assertEqual(result, "dropped")
        push.assert_not_called()
        self.upstream_event.refresh_from_db()
        self.assertEqual(self.upstream_event.status, "failed")
        self.assertEqual(self.upstream_event.reason, "绑定已解除/失效，不再投递")
        # ⛔ 一条都没发出去 ⇒ 不算一次投递尝试、⛔ 也不刷绑定健康度。
        self.assertEqual(self.upstream_event.attempts, 0)
        self.assertIsNone(self.upstream_event.next_attempt_at)
        self.upstream_binding.refresh_from_db()
        self.assertEqual(self.upstream_binding.consecutive_failures, 0)
        self.assertEqual(self.upstream_binding.last_error, "")
        self.assertIsNone(self.upstream_binding.last_error_at)

    def test_a_soft_deleted_upstream_binding_drops_the_event(self):
        """解绑（`deleted_at` 非空）与停用同样是"不再投"——上游没有 `state` 列。

        判别力：失效判据只写 `not binding.enabled` ⇒ 本用例红（软删的绑定照样推）。
        """
        self.upstream_binding.deleted_at = self.now
        self.upstream_binding.save(update_fields=["deleted_at"])

        with mock.patch("apps.business.peer_worker.push_events") as push:
            result = deliver_one(self.upstream_event.pk, now=self.now)

        self.assertEqual(result, "dropped")
        push.assert_not_called()
        self.upstream_event.refresh_from_db()
        self.assertEqual(self.upstream_event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(self.upstream_event.reason, "绑定已解除/失效，不再投递")

    def test_the_withdraw_recheck_is_not_applied_to_upstream_events(self):
        """`_recheck_withdraw` **只对下行**：它要读 `binding.account_id`，上游绑定没有这一列。

        ⚠️ 上行本期**没有**撤单事件（spec §7.1）⇒ 这条夹具是**合成形状**，唯一目的是钉住
        「复核按方向分叉」这条分支：去掉 `role == "downstream"` 那一半 ⇒ 本用例以
        `AttributeError`（而不是断言失败）红。
        """
        event = self._event(kind="order.withdraw")

        with mock.patch(
            "apps.business.peer_worker.push_events", return_value={}
        ) as push:
            result = deliver_one(event.pk, now=self.now)

        self.assertEqual(result, "done")
        push.assert_called_once()
        self.assertEqual(push.call_args.args[2][0]["target_ref"], UPSTREAM_ORDER_NO)


class UpstreamFailureTests(_UpstreamDeliveryFixture):
    """④ 失败记账泛化，但**自动停绑只对下行**。"""

    def test_a_single_upstream_failure_writes_the_ledger_without_revoking(self):
        with mock.patch(
            "apps.business.peer_worker.push_events",
            side_effect=PeerUnreachable("boom"),
        ):
            result = deliver_one(self.upstream_event.pk, now=self.now)

        self.assertEqual(result, "retry")
        self.upstream_event.refresh_from_db()
        self.upstream_binding.refresh_from_db()
        self.assertEqual(self.upstream_event.reason, "boom")
        self.assertEqual(self.upstream_event.attempts, 1)
        self.assertEqual(self.upstream_binding.consecutive_failures, 1)
        self.assertEqual(self.upstream_binding.last_error, "boom")
        self.assertEqual(self.upstream_binding.last_error_at, self.now)
        # ⛔ 一条操作日志都不许写（停绑是下行专有）。
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_upstream_failures_write_health_but_never_auto_revoke(self):
        """上行失败**不**自动停绑（业务方没裁定过；⛔ 不擅自加）。"""
        with mock.patch("apps.business.peer_worker.push_events",
                        side_effect=PeerUnreachable("boom")):
            for _ in range(10):
                self.upstream_event.status = "pending"
                self.upstream_event.next_attempt_at = None
                self.upstream_event.save(update_fields=["status", "next_attempt_at"])
                deliver_one(self.upstream_event.pk, now=self.now)
        self.upstream_binding.refresh_from_db()
        self.assertTrue(self.upstream_binding.enabled)      # 仍启用
        self.assertGreaterEqual(self.upstream_binding.consecutive_failures, 1)
        # 判别力：把自动停绑那段照搬到上行 ⇒ `AttributeError`（上游没有 `state` /
        # `revoked_at` 两列），或写出一行停绑日志 ⇒ 下面这条立刻红。
        self.assertEqual(OperationLog.objects.count(), 0)
        self.assertEqual(self.upstream_binding.consecutive_failures, 10)
        self.assertEqual(self.upstream_binding.last_error, "boom")
        self.assertEqual(self.upstream_binding.last_error_at, self.now)


class BindingRoleTests(_UpstreamDeliveryFixture):
    """⑤ `binding_role` 必须**显式**分叉，认不出就响亮抛。"""

    def test_an_unknown_binding_role_raises(self):
        """⛔ 不写"先当下游、找不到再当上游"：将来加第三种绑定会被静默当上游。

        判别力：把助手写成 `if role == "downstream": … else: <上游>` ⇒ 本用例红
        （`sideways` 被当成上游，回的是一个绑定而不是抛）。
        """
        event = PeerSyncEvent(binding_role="sideways", binding_id="1")

        with self.assertRaises(ValueError):
            peer_worker._binding_for_event(event)
