"""D-0347 团团通大厅镜像：**同步抢单直连**（业务方拍板「能抢就是抢到 抢不到就是抢不到」）。

覆盖（每条都用例钉住）：

① **成交**：请求内直推 `hall.claim`（≤3 秒）⇒ 响应车带回 `order.assign` + `hall.claim_ok`
   ⇒ 喂既有 `apply_events` ⇒ 当场 `promoted`（不排 outbox）；
② **拒绝**：上家当场回绝 ⇒ 当场 1012 + 行按分因收口（`gone`）；
③ **无结论**（超时 / 老对端响应无键 / 多跳 forwarded）⇒ 当场放回 `listed` +
   1017「对端未响应，请稍后重试」（⛔ 不发撤销：协议没有这个动词）；
④ **迟到接受**（E-0236 家族既有处置）：放回后上家的 `order.assign` 才到 ⇒
   行是 `listed` ⇒ 落**未分配池**（补派，不挂打手）；放回后被别的打手抢走 ⇒
   **转正给现持有人**；
⑤ **并发双打手**：A 成交后 B 再点 ⇒ 当场 1012、B 不发 HTTP；
⑥ **env 注入**：`PEER_CLAIM_SYNC_TIMEOUT` 调超时、坏值回默认；
⑦ **对端响应车**：`apply_upstream_events` 的 `Outcome.claim_replies` 形状
   （accepted 带 assign+ok / refused 带 reason / replay 只带 ok / forwarded 空）
   + `_outcome_to_dict` 空时无新键（老响应逐字节不变）；
⑧ **视图**：内部端点成交档 `data.status="promoted"`、无结论档 1017 + msg 逐字。

⚠️ 全链用例在**同一个租户 schema** 里摆出两侧夹具（承 `test_peer_hall_claim.ClaimChainFixture`）；
HTTP 用 `tests._peer_stream_fakes.fake_sync_claim_upstream` 拦截（E-0029：本线代码经
`peer_client` 模块属性查找），假身调**真** `apply_upstream_events` 生成判决、用**真端点**
的 `_outcome_to_dict` 拼响应——只 mock 网络运输，受理机与响应组装都是真的。夹具值全部合成值。
"""
import os
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django_tenants.test.cases import TenantTestCase

from apps.business import peer_dispatch
from apps.business.claim import ClaimError
from apps.business.models import (
    EmpType,
    OrderStatus,
    PeerHallMirror,
    PeerOrderLink,
    PeerSyncEvent,
    Performance,
)
from apps.business.peer_hall import (
    PEER_CLAIM_UNRESPONSIVE_MESSAGE,
    PEER_CLAIM_UNRESPONSIVE_NOTE,
    claim_peer_mirror,
    claim_peer_mirror_sync,
)
from apps.business.peer_inbound import apply_events, apply_upstream_events
from apps.business.peer_sync import HALL_CLAIM_KIND, HALL_TAKE_KIND
from apps.core.error_codes import ErrorCode
from tests._peer_stream_fakes import fake_sync_claim_upstream
from tests.p5_auth_helpers import make_logged_in_client
from tests.test_peer_hall_claim import ClaimChainFixture

#: 后端默认同步窗（`peer_hall.PEER_CLAIM_SYNC_TIMEOUT` 的缺省值；钉进出站调用）。
DEFAULT_SYNC_TIMEOUT = 3.0

UNRESPONSIVE_CODE = ErrorCode.PLATFORM_QUERY_FAILED  # 1017


def claim_event_wire(target_ref: str) -> dict:
    """下家直推一条 `hall.claim` 的线形状（与 `claim_peer_mirror_sync` 发的同形）。"""
    return {
        "idempotency_key": "syn-sync-claim-1",
        "kind": HALL_CLAIM_KIND,
        "target_ref": target_ref,
        "occurred_at": ClaimChainFixture._now().isoformat(),
        "payload": {"claim_by": "syn-打手"},
    }


class SyncClaimDealTests(ClaimChainFixture):
    """① 成交：当场转正，不排 outbox。"""

    def test_accepted_claim_promotes_in_request_without_outbox(self):
        captured: dict = {}
        self.share_with_downstream()
        with fake_sync_claim_upstream(self, captured):
            row = claim_peer_mirror_sync(
                user_id=self.taker.pk, mirror_id=self.mirror.pk
            )
        # 当场成交：行 promoted、正式单落地、挂抢单打手、代练中、价 = 展示价。
        self.assertEqual(row.status, PeerHallMirror.Status.PROMOTED)
        order = row.promoted_order
        self.assertIsNotNone(order)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(order.hall_price, Decimal("50.00"))
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(main.user_id, self.taker.pk)
        # ⛔ 不排 outbox：人类单跳的 claim 不再进队列（机器 / 多跳才排）。
        self.assertFalse(
            PeerSyncEvent.objects.filter(
                binding_role="upstream", kind="hall.claim"
            ).exists()
        )
        # 出站形状：真绑定地址、真令牌、默认 3 秒超时、一条 hall.claim 事件。
        self.assertEqual(captured["base_url"], "https://peer-hall-up.test.invalid")
        self.assertEqual(captured["timeout"], DEFAULT_SYNC_TIMEOUT)
        sent = captured["events"][0]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["kind"], "hall.claim")
        self.assertEqual(sent[0]["target_ref"], self.upstream_order.order_no)


class SyncClaimRefusedTests(ClaimChainFixture):
    """② 拒绝：当场 1012，行按分因收口。"""

    def test_refused_claim_closes_the_row_and_raises_1012(self):
        captured: dict = {}
        self.share_with_downstream()
        # 上家那张单已被别人接走（上家侧事实）。
        Performance.objects.filter(
            order=self.upstream_order, emp_type=EmpType.MAIN
        ).update(user=self.down_account)
        self.upstream_order.status = OrderStatus.IN_PROGRESS
        self.upstream_order.save(update_fields=["status", "updated_at"])
        with fake_sync_claim_upstream(self, captured):
            with self.assertRaises(ClaimError) as caught:
                claim_peer_mirror_sync(
                    user_id=self.taker.pk, mirror_id=self.mirror.pk
                )
        self.assertEqual(caught.exception.code, ErrorCode.ORDER_ALREADY_TAKEN)
        self.assertEqual(str(caught.exception), "该笔订单已被其他打手接取")
        self.mirror.refresh_from_db()
        # 永久拒绝（「已不在对方大厅」）⇒ 卡彻底下架。
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.GONE)
        # 零转正。
        self.assertIsNone(self.mirror.promoted_order_id)


class SyncClaimUnresponsiveTests(ClaimChainFixture):
    """③ 无结论：当场放回大厅 + 1017（超时 / 老对端 / 转发）。"""

    def _assert_released(self, caught):
        self.assertEqual(caught.exception.code, UNRESPONSIVE_CODE)
        self.assertEqual(str(caught.exception), PEER_CLAIM_UNRESPONSIVE_MESSAGE)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(self.mirror.claiming_by_id)
        self.assertIsNone(self.mirror.claiming_at)
        self.assertEqual(self.mirror.last_error, PEER_CLAIM_UNRESPONSIVE_NOTE)

    def test_transport_timeout_releases_the_row_to_the_hall(self):
        from apps.business.peer_client import PeerUnreachable

        self.share_with_downstream()
        with mock.patch(
            "apps.business.peer_client.push_events",
            side_effect=PeerUnreachable("syn-对端超时"),
        ):
            with self.assertRaises(ClaimError) as caught:
                claim_peer_mirror_sync(
                    user_id=self.taker.pk, mirror_id=self.mirror.pk
                )
        self._assert_released(caught)

    def test_old_peer_without_claim_replies_releases_too(self):
        """老版本对端（响应没有 `claim_replies`）⇒ 同一支：放回 + 1017。"""
        captured: dict = {}
        self.share_with_downstream()
        with fake_sync_claim_upstream(self, captured, drop_replies=True):
            with self.assertRaises(ClaimError) as caught:
                claim_peer_mirror_sync(
                    user_id=self.taker.pk, mirror_id=self.mirror.pk
                )
        self._assert_released(caught)


class LateVerdictTests(ClaimChainFixture):
    """④ 迟到接受（E-0236 家族既有处置，全部落在既有分支上）：补派 / 归现持有人。"""

    def setUp(self):
        super().setUp()
        self.taker2 = get_user_model().objects.create_user(
            username="syn-sync-claim-taker2", password="pw-syn"
        )
        from apps.accounts.models import StaffProfile
        from tests.p5_auth_helpers import grant_nodes

        StaffProfile.objects.create(user=self.taker2).games.set([self.game])
        taker_group, _ = Group.objects.get_or_create(name="大厅节点")
        self.taker2.groups.add(taker_group)
        grant_nodes(self.taker2, "grab_hall.take_order")

    def _claim_then_lose_the_response(self):
        """先同步抢一次、让响应车丢失（对端真实受理并排了 assign）。"""
        captured: dict = {}
        self.share_with_downstream()
        with fake_sync_claim_upstream(self, captured, drop_replies=True):
            with self.assertRaises(ClaimError) as caught:
                claim_peer_mirror_sync(
                    user_id=self.taker.pk, mirror_id=self.mirror.pk
                )
        self.assertEqual(caught.exception.code, UNRESPONSIVE_CODE)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)

    def test_late_assign_lands_in_the_unassigned_pool(self):
        """放回后迟到的 `order.assign` ⇒ 建**未分配**单（补派，不挂打手），行保持 listed。"""
        self._claim_then_lose_the_response()
        self.deliver_assign_to_downstream()
        link = PeerOrderLink.objects.get(
            upstream_binding=self.up_binding,
            peer_order_no=self.upstream_order.order_no,
        )
        order = link.order
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        # 既有口径（apply_events docstring）：peer 建单支**不建绩效行** ⇒ 补派单无 MAIN 行
        # = 「不挂打手」的可观察事实。
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).exists()
        )
        # 镜像行不在 `claiming` ⇒ 不转正，卡留在厅里（随后由 hall.take 收口）。
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        # 上家摘牌（单已指派 ⇒ 离厅）⇒ 行收口 gone（既有 take 支）。
        made = peer_dispatch.enqueue_hall_event(
            self.upstream_order, kind=HALL_TAKE_KIND
        )
        self.assertTrue(made)
        for event in made:
            apply_events(
                [{
                    "idempotency_key": event.idempotency_key,
                    "kind": event.kind,
                    "target_ref": event.target_ref,
                    "occurred_at": (event.meta or {}).get("occurred_at"),
                    "payload": event.payload,
                }],
                binding=self.up_binding,
            )
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.GONE)

    def test_late_assign_promotes_for_the_new_holder(self):
        """放回后被**别的打手**抢走 ⇒ 迟到的 assign 转正给现持有人（谁占着卡归谁）。"""
        self._claim_then_lose_the_response()
        claim_peer_mirror(user_id=self.taker2.pk, mirror_id=self.mirror.pk)
        self.deliver_assign_to_downstream()
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        order = self.mirror.promoted_order
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(main.user_id, self.taker2.pk)


class SyncClaimConcurrencyTests(ClaimChainFixture):
    """⑤ 并发双打手：输家当场 1012、不发 HTTP。"""

    def test_second_taker_gets_1012_without_another_http_call(self):
        captured: dict = {}
        self.share_with_downstream()
        taker2 = get_user_model().objects.create_user(
            username="syn-sync-claim-loser", password="pw-syn"
        )
        from apps.accounts.models import StaffProfile
        from tests.p5_auth_helpers import grant_nodes

        StaffProfile.objects.create(user=taker2).games.set([self.game])
        grant_nodes(taker2, "grab_hall.take_order")
        with fake_sync_claim_upstream(self, captured) as push:
            row = claim_peer_mirror_sync(
                user_id=self.taker.pk, mirror_id=self.mirror.pk
            )
            self.assertEqual(row.status, PeerHallMirror.Status.PROMOTED)
            with self.assertRaises(ClaimError) as caught:
                claim_peer_mirror_sync(user_id=taker2.pk, mirror_id=self.mirror.pk)
        self.assertEqual(caught.exception.code, ErrorCode.ORDER_ALREADY_TAKEN)
        # ⛔ 输家一个 HTTP 都不发（预占输在本地条件更新 / 状态闸）。
        self.assertEqual(push.call_count, 1)


class SyncClaimEnvTests(ClaimChainFixture):
    """⑥ env 注入：`PEER_CLAIM_SYNC_TIMEOUT` 调超时、坏值回默认、夹紧边界。"""

    @staticmethod
    def _capture_timeout_fake(captured):
        from apps.business.peer_client import PeerUnreachable

        def fake(base_url, token, events, *, timeout=None):
            captured["timeout"] = timeout
            raise PeerUnreachable("syn-超时")

        return fake

    def _timeout_used(self, env_value) -> float:
        captured: dict = {}
        self.share_with_downstream()
        with mock.patch.dict(os.environ, {"PEER_CLAIM_SYNC_TIMEOUT": env_value}):
            with mock.patch(
                "apps.business.peer_client.push_events",
                side_effect=self._capture_timeout_fake(captured),
            ):
                with self.assertRaises(ClaimError):
                    claim_peer_mirror_sync(
                        user_id=self.taker.pk, mirror_id=self.mirror.pk
                    )
        return captured["timeout"]

    def test_env_value_reaches_the_outbound_call(self):
        self.assertEqual(self._timeout_used("1.5"), 1.5)

    def test_bad_env_value_falls_back_to_the_default(self):
        self.assertEqual(self._timeout_used("abc"), DEFAULT_SYNC_TIMEOUT)

    def test_env_value_is_clamped(self):
        self.assertEqual(self._timeout_used("999"), 30.0)
        self.assertEqual(self._timeout_used("0.1"), 0.5)


class ClaimRepliesWireTests(ClaimChainFixture):
    """⑦ 对端响应车：`Outcome.claim_replies` 的形状与 `_outcome_to_dict` 的键纪律。"""

    def _deliver(self, *, order_taken=False, replay=False):
        self.share_with_downstream()
        if order_taken:
            Performance.objects.filter(
                order=self.upstream_order, emp_type=EmpType.MAIN
            ).update(user=self.down_account)
            self.upstream_order.status = OrderStatus.IN_PROGRESS
            self.upstream_order.save(update_fields=["status", "updated_at"])
        if replay:
            # A9 重放证据：这条绑定名下已有 `hall.claim_ok` 应答行（任何状态）。
            PeerSyncEvent.objects.create(
                binding_role="downstream",
                binding_id=str(self.down_binding.pk),
                direction="out",
                kind="hall.claim_ok",
                target_type="order",
                target_ref=self.upstream_order.order_no,
                status=PeerSyncEvent.Status.DONE,
                payload={},
                meta={"occurred_at": self._now().isoformat()},
                idempotency_key="syn-replay-evidence",
            )
        return apply_upstream_events(
            [claim_event_wire(self.upstream_order.order_no)],
            binding=self.down_binding,
        )

    def test_accepted_reply_carries_assign_and_claim_ok(self):
        outcome = self._deliver()
        self.assertEqual(len(outcome.claim_replies), 1)
        entry = outcome.claim_replies[0]
        self.assertEqual(entry["target_ref"], self.upstream_order.order_no)
        self.assertEqual(entry["outcome"], "accepted")
        kinds = [event["kind"] for event in entry["reply_events"]]
        self.assertEqual(kinds, ["order.assign", "hall.claim_ok"])
        for event in entry["reply_events"]:
            self.assertIn("idempotency_key", event)
            self.assertIn("occurred_at", event)
            self.assertIsInstance(event["payload"], dict)

    def test_refused_reply_carries_the_reason(self):
        from apps.business.peer_inbound import CLAIM_REFUSED_NOTE

        outcome = self._deliver(order_taken=True)
        entry = outcome.claim_replies[0]
        self.assertEqual(entry["outcome"], "refused")
        self.assertEqual(len(entry["reply_events"]), 1)
        self.assertEqual(
            entry["reply_events"][0]["payload"],
            {"reason": CLAIM_REFUSED_NOTE},
        )

    def test_replayed_reply_carries_only_claim_ok(self):
        outcome = self._deliver(order_taken=True, replay=True)
        entry = outcome.claim_replies[0]
        self.assertEqual(entry["outcome"], "replay")
        self.assertEqual(
            [event["kind"] for event in entry["reply_events"]], ["hall.claim_ok"]
        )

    def test_forwarded_reply_has_empty_events(self):
        """中间层收到对**它自己镜像卡**的 claim ⇒ forwarded（判决在更上游，当场给不出）。"""
        from apps.business.models import PeerUpstreamBinding
        from apps.business.peer_sync import (
            HALL_THROW_KIND,
            hash_token,
            new_token,
        )

        operator = get_user_model().objects.create_user(
            username="syn-fwd-op", password="pw-syn"
        )
        up2 = PeerUpstreamBinding.objects.create(
            peer_name="syn-更上层",
            peer_base_url="https://peer-fwd-up.test.invalid",
            peer_account="syn-fwd-up",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-fwd-local.test.invalid",
            bound_by=operator,
            hall_sync_enabled=True,
        )
        card = PeerHallMirror.objects.create(
            peer_upstream_binding=up2,
            peer_base_url="https://peer-fwd-up.test.invalid",
            peer_order_no="syn-fwd-card-1",
            status=PeerHallMirror.Status.LISTED,
            game=self.game,
            payload={"order_no": "syn-fwd-card-1"},
            remote_hall_price=Decimal("30.00"),
            first_seen_at=self._now(),
        )
        # 共享证据：这张卡真的 `hall.throw` 投给过 down_binding（Z20 的判据要它）。
        made = peer_dispatch.enqueue_hall_mirror_event(card, kind=HALL_THROW_KIND)
        self.assertTrue(made)
        PeerSyncEvent.objects.filter(pk__in=[e.pk for e in made]).update(
            status=PeerSyncEvent.Status.DONE,
            reason="",
            processed_at=self._now(),
            next_attempt_at=None,
        )
        outcome = apply_upstream_events(
            [claim_event_wire("syn-fwd-card-1")], binding=self.down_binding
        )
        entry = outcome.claim_replies[0]
        self.assertEqual(entry["outcome"], "forwarded")
        self.assertEqual(entry["reply_events"], [])

    def test_outcome_dict_omits_the_key_when_empty(self):
        from apps.business.api.peer_views import _outcome_to_dict
        from apps.business.peer_inbound import Outcome

        bare = _outcome_to_dict(
            Outcome(applied=1, skipped=0, skipped_details=[])
        )
        self.assertNotIn("claim_replies", bare)
        carried = _outcome_to_dict(
            Outcome(
                applied=1, skipped=0, skipped_details=[],
                claim_replies=({"target_ref": "r", "outcome": "refused",
                                "reply_events": []},),
            )
        )
        self.assertEqual(len(carried["claim_replies"]), 1)


class InternalSyncClaimViewTests(ClaimChainFixture):
    """⑧ 内部端点：成交档 `status="promoted"`、无结论档 1017 + msg 逐字。"""

    def setUp(self):
        super().setUp()
        self.api = make_logged_in_client(
            username="syn-sync-claim-web",
            groups=("打手",),
            nodes=("grab_hall.take_order",),
            games=[self.game],
        )

    def test_sales_account_is_denied_before_reserving_or_contacting_the_peer(self):
        self.up_binding.bound_by = get_user_model().objects.get(username="syn-sync-claim-web")
        self.up_binding.save(update_fields=["bound_by"])
        self.share_with_downstream()
        events_before = set(PeerSyncEvent.objects.values_list("pk", flat=True))
        with mock.patch("apps.business.peer_client.push_events") as push:
            response = self.api.post(
                f"/api/business/hall/peer/{self.mirror.pk}/claim/",
                {}, content_type="application/json",
            )
        self.assertEqual(response.json()["code"], ErrorCode.BUSINESS)
        self.assertIn("销售提成归属人", response.json()["msg"])
        push.assert_not_called()
        self.mirror.refresh_from_db()
        self.upstream_order.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(self.mirror.claiming_by_id)
        self.assertIsNone(self.mirror.promoted_order_id)
        self.assertEqual(self.upstream_order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(set(PeerSyncEvent.objects.values_list("pk", flat=True)), events_before)

    def test_view_returns_promoted_on_the_sync_deal(self):
        captured: dict = {}
        self.share_with_downstream()
        with fake_sync_claim_upstream(self, captured):
            response = self.api.post(
                f"/api/business/hall/peer/{self.mirror.pk}/claim/",
                {}, content_type="application/json",
            )
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["status"], "promoted")
        self.assertEqual(body["data"]["mirror_pk"], self.mirror.pk)

    def test_view_returns_1017_with_the_exact_message_on_timeout(self):
        from apps.business.peer_client import PeerUnreachable

        self.share_with_downstream()
        with mock.patch(
            "apps.business.peer_client.push_events",
            side_effect=PeerUnreachable("syn-对端超时"),
        ):
            response = self.api.post(
                f"/api/business/hall/peer/{self.mirror.pk}/claim/",
                {}, content_type="application/json",
            )
        body = response.json()
        self.assertEqual(body["code"], UNRESPONSIVE_CODE, body)
        self.assertEqual(body["msg"], PEER_CLAIM_UNRESPONSIVE_MESSAGE)
        # 卡已放回：再抢是新的开始（这里只钉行状态）。
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
