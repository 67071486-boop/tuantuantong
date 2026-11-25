import json
import threading
import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, schema_context
from jidanpu.errors import ApiError

from apps.accounts.models import StaffProfile
from apps.business import jidanpu, jidanpu_member
from apps.business.claim import claim_order
from apps.business.models import (
    EmpType, Game, JidanpuMemberBinding, JidanpuMemberOrderLink, JidanpuSyncEvent,
    Order, OrderStatus, Performance,
)
from tests import test_jidanpu_member_consume as consume_fixture
from tests.p5_auth_helpers import grant_games, make_logged_in_client


class MemberLaunchFreshnessTests(consume_fixture._Base):
    def _queued_launch(self):
        grant_games(self.user, self.game)
        self.assertEqual(jidanpu_member.queue_hall_launch(self.order), 1)
        return JidanpuSyncEvent.objects.get(kind="member.launch", target_ref=self.order.order_no)

    def test_a_claimed_order_is_not_sent_back_to_the_downstream_hall(self):
        event = self._queued_launch()
        Performance.objects.create(order=self.order, emp_type=EmpType.MAIN)
        claimed = claim_order(
            user_id=self.user.pk, order_id=self.order.pk, nodes={"grab_hall.take_order"},
            enforce_limits=False, enforce_deposit=False,
        )
        self.assertEqual(claimed.status, "IN_PROGRESS")
        self.assertEqual(Performance.objects.get(order=self.order, emp_type=EmpType.MAIN).user_id, self.user.pk)
        upstream = consume_fixture._Upstream(lookup=self._launch_lookup())
        with consume_fixture._stub(upstream):
            result = jidanpu_member.consume_event(event, client=consume_fixture.CLIENT)
        self.assertEqual(upstream.names(), [])
        self.assertEqual(result, "skipped")
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(order=self.order).exists())

    def test_disabling_hall_sync_keeps_an_already_queued_launch(self):
        event = self._queued_launch()
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(hall_sync_enabled=False)
        upstream = consume_fixture._Upstream(lookup=self._launch_lookup())
        with consume_fixture._stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(event, client=consume_fixture.CLIENT), "done")
        self.assertIn("add_order", upstream.names())
        self.assertIn("throw_order", upstream.names())
        self.assertTrue(JidanpuMemberOrderLink.objects.get(order=self.order).in_hall)

    def test_a_stale_launch_keeps_its_recording_progress(self):
        event = self._queued_launch()
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(payload={
            **event.payload, "recorded": True, "jidanpu_order_id": consume_fixture.JID,
            "recording_source_order_no": self.order.order_no,
        })
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.UNASSIGNED)
        upstream = consume_fixture._Upstream(lookup=self._launch_lookup())
        with consume_fixture._stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(event, client=consume_fixture.CLIENT), "skipped")
        self.assertEqual(upstream.names(), [])
        event.refresh_from_db()
        self.assertTrue(event.payload["recorded"])
        self.assertEqual(event.payload["jidanpu_order_id"], consume_fixture.JID)
        self.assertEqual(event.payload["recording_source_order_no"], self.order.order_no)


class MemberHallOwnershipTests(consume_fixture._Base):
    def _same_store(self, *, older_hall=False, newer_hall=True):
        grant_games(self.user, self.game)
        self.binding.hall_sync_enabled = older_hall
        self.binding.save(update_fields=["hall_sync_enabled"])
        other = self._other_binding(enabled=True)
        other.hall_sync_enabled = newer_hall
        other.save(update_fields=["hall_sync_enabled"])
        grant_games(other.user, self.game)
        return other

    def test_an_older_hall_off_binding_does_not_block_the_enabled_same_store_binding(self):
        other = self._same_store()
        self.assertEqual(jidanpu_member.queue_hall_launch(self.order), 1)
        event = JidanpuSyncEvent.objects.get(kind="member.launch", target_ref=self.order.order_no)
        self.assertEqual(event.payload["member_binding_id"], other.pk)

    def test_reconcile_uses_the_same_hall_owner_as_the_trigger(self):
        other = self._same_store()
        jidanpu_member.reconcile_and_queue(self.binding, now=timezone.now())
        jidanpu_member.reconcile_and_queue(other, now=timezone.now())
        rows = JidanpuSyncEvent.objects.filter(kind="member.launch", target_ref=self.order.order_no)
        self.assertEqual(rows.count(), 1)
        event = rows.get()
        self.assertEqual(event.payload["member_binding_id"], other.pk)

    def test_two_hall_on_bindings_still_send_only_one_copy(self):
        self._same_store(older_hall=True)
        self.assertEqual(jidanpu_member.queue_hall_launch(self.order), 1)
        event = JidanpuSyncEvent.objects.get(kind="member.launch", target_ref=self.order.order_no)
        self.assertEqual(event.payload["member_binding_id"], self.binding.pk)

    def test_a_newer_hall_off_binding_does_not_take_the_older_hall_owners_place(self):
        self._same_store(older_hall=True, newer_hall=False)
        self.assertEqual(jidanpu_member.queue_hall_launch(self.order), 1)
        event = JidanpuSyncEvent.objects.get(kind="member.launch", target_ref=self.order.order_no)
        self.assertEqual(event.payload["member_binding_id"], self.binding.pk)

    def test_a_hall_on_binding_without_game_scope_is_not_a_replacement_owner(self):
        other = self._same_store()
        StaffProfile.objects.get(user=other.user).games.clear()
        self.assertEqual(jidanpu_member.queue_hall_launch(self.order), 0)
        jidanpu_member.reconcile_and_queue(other, now=timezone.now())
        self.assertFalse(JidanpuSyncEvent.objects.filter(kind="member.launch").exists())

    def test_an_existing_same_store_copy_is_neither_withdrawn_nor_recorded_again(self):
        other = self._same_store()
        link = self._link(in_hall=True)
        jidanpu_member.reconcile_and_queue(self.binding, now=timezone.now())
        jidanpu_member.reconcile_and_queue(other, now=timezone.now())
        event = JidanpuSyncEvent.objects.get(kind="member.launch", target_ref=self.order.order_no)
        upstream = consume_fixture._Upstream(lookup=self._launch_lookup())
        with consume_fixture._stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(event, client=consume_fixture.CLIENT), "skipped")
        self.assertNotIn("add_order", upstream.names())
        self.assertFalse(JidanpuSyncEvent.objects.filter(kind="member.withdraw").exists())
        link.refresh_from_db()
        self.assertTrue(link.in_hall)
        self.assertIsNone(link.withdrawn_at)
        self.assertEqual(JidanpuMemberOrderLink.objects.filter(order=self.order).count(), 1)


class MemberStatusIntentTests(consume_fixture._Base):
    def _own_linked_order(self, *, status=OrderStatus.IN_PROGRESS):
        self._link(in_hall=False)
        Order.objects.filter(pk=self.order.pk).update(status=status)
        self.order.refresh_from_db()
        Performance.objects.create(order=self.order, user=self.user, emp_type=EmpType.MAIN)

    def test_a_new_refund_recovery_replaces_the_older_pool_status_intent(self):
        self._own_linked_order()
        jidanpu_member.queue_status_out(self.order, from_status=OrderStatus.WAITING_GRAB)
        jidanpu_member.queue_status_out(self.order, from_status=OrderStatus.REFUNDED)
        rows = JidanpuSyncEvent.objects.filter(kind="member.status_out", target_ref=self.order.order_no)
        self.assertEqual(rows.count(), 1)
        event = rows.get()
        upstream = consume_fixture._Upstream(remote_sta="已退单")
        with consume_fixture._stub(upstream):
            result = jidanpu_member.consume_event(event, client=consume_fixture.CLIENT)
        self.assertEqual([payload["status_label"] for name, payload in upstream.calls if name == "set_order_status"], ["代练中"])
        self.assertEqual(result, "done")
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertEqual(event.payload["from_status"], "REFUNDED")

    def _manual_status_push(self, *, old_automatic):
        self._own_linked_order(status=OrderStatus.PENDING_REVIEW)
        client = make_logged_in_client(
            username="followup-manual-taker", groups=("代练员",),
            nodes=("my_orders.send_to_my_system",),
        )
        user = get_user_model().objects.get(username="followup-manual-taker")
        self.binding.user = user
        self.binding.save(update_fields=["user"])
        Performance.objects.filter(order=self.order, emp_type=EmpType.MAIN).update(user=user)
        if old_automatic:
            jidanpu_member.queue_status_out(self.order, from_status=OrderStatus.IN_PROGRESS)
        upstream = consume_fixture._Upstream(remote_sta="代练中")
        pair = (mock.Mock(spec=["close"]), mock.Mock(spec=["close"]))
        self.addCleanup(jidanpu.member_pool().release_all)
        with consume_fixture._stub(upstream), mock.patch.object(jidanpu, "_open_client_pair", return_value=pair):
            response = client.post(
                "/api/business/member/jidanpu-push-now/",
                json.dumps({"order_nos": [self.order.order_no], "action": "status"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        return response.json()["data"], upstream

    def test_manual_status_push_replaces_an_older_automatic_intent(self):
        data, upstream = self._manual_status_push(old_automatic=True)
        self.assertEqual(data["pushed"], 1, data)
        self.assertEqual([payload["status_label"] for name, payload in upstream.calls if name == "set_order_status"], ["待验收"])
        event = JidanpuSyncEvent.objects.get(kind="member.status_out", direction="out")
        self.assertEqual(event.status, "done")
        self.assertIs(event.payload["manual"], True)

    def test_manual_status_push_without_an_older_intent_still_works(self):
        data, upstream = self._manual_status_push(old_automatic=False)
        self.assertEqual(data["pushed"], 1, data)
        self.assertIn("set_order_status", upstream.names())

    def test_a_new_automatic_request_does_not_inherit_manual_status_permission(self):
        self._own_linked_order(status=OrderStatus.PENDING_REVIEW)
        jidanpu_member.queue_status_out(self.order, manual=True)
        jidanpu_member.queue_status_out(self.order, from_status=OrderStatus.IN_PROGRESS)
        event = JidanpuSyncEvent.objects.get(kind="member.status_out", direction="out")
        upstream = consume_fixture._Upstream(remote_sta="代练中")
        with consume_fixture._stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(event, client=consume_fixture.CLIENT), "skipped")
        self.assertEqual(upstream.names(), [])
        event.refresh_from_db()
        self.assertNotIn("manual", event.payload)

    def test_a_missing_new_source_state_does_not_inherit_old_refund_recovery(self):
        self._own_linked_order()
        jidanpu_member.queue_status_out(self.order, from_status=OrderStatus.REFUNDED)
        jidanpu_member.queue_status_out(self.order)
        event = JidanpuSyncEvent.objects.get(kind="member.status_out", direction="out")
        upstream = consume_fixture._Upstream(remote_sta="已退单")
        with consume_fixture._stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(event, client=consume_fixture.CLIENT), "skipped")
        self.assertEqual(upstream.names(), [])
        event.refresh_from_db()
        self.assertNotIn("from_status", event.payload)

    def test_non_status_tasks_keep_their_recording_progress_when_rearmed(self):
        event = jidanpu_member.queue_member_event(self.order, self.binding, kind="member.assign", payload={
            "recorded": True, "jidanpu_order_id": consume_fixture.JID,
            "recording_source_order_no": self.order.order_no,
        })
        jidanpu_member.queue_member_event(self.order, self.binding, kind="member.assign", rearm=True)
        event.refresh_from_db()
        self.assertTrue(event.payload["recorded"])
        self.assertEqual(event.payload["jidanpu_order_id"], consume_fixture.JID)
        self.assertEqual(event.payload["recording_source_order_no"], self.order.order_no)

    def test_a_loaded_manual_snapshot_cannot_override_the_new_automatic_intent(self):
        self._own_linked_order(status=OrderStatus.PENDING_REVIEW)
        jidanpu_member.queue_status_out(self.order, manual=True)
        stale = JidanpuSyncEvent.objects.get(kind="member.status_out", direction="out")
        jidanpu_member.queue_status_out(self.order, from_status=OrderStatus.IN_PROGRESS)
        upstream = consume_fixture._Upstream(remote_sta="代练中")
        with consume_fixture._stub(upstream):
            result = jidanpu_member.consume_event(stale, client=consume_fixture.CLIENT)
        self.assertEqual(upstream.names(), [])
        self.assertEqual(result, "skipped")
        stale.refresh_from_db()
        self.assertEqual(stale.status, "skipped")
        self.assertNotIn("manual", stale.payload)

    def test_a_loaded_recovery_snapshot_cannot_override_the_new_pool_intent(self):
        self._own_linked_order()
        jidanpu_member.queue_status_out(self.order, from_status=OrderStatus.REFUNDED)
        stale = JidanpuSyncEvent.objects.get(kind="member.status_out", direction="out")
        jidanpu_member.queue_status_out(self.order, from_status=OrderStatus.WAITING_GRAB)
        upstream = consume_fixture._Upstream(remote_sta="已退单")
        with consume_fixture._stub(upstream):
            result = jidanpu_member.consume_event(stale, client=consume_fixture.CLIENT)
        self.assertEqual(upstream.names(), [])
        self.assertEqual(result, "skipped")
        stale.refresh_from_db()
        self.assertEqual(stale.status, "skipped")
        self.assertEqual(stale.payload["from_status"], "WAITING_GRAB")

    def test_a_loaded_exhausted_snapshot_uses_the_reopened_retry_budget(self):
        self._own_linked_order(status=OrderStatus.ISSUE)
        jidanpu_member.queue_status_out(self.order, from_status=OrderStatus.IN_PROGRESS)
        JidanpuSyncEvent.objects.filter(kind="member.status_out").update(
            status="failed", attempts=5, processed_at=timezone.now(),
        )
        stale = JidanpuSyncEvent.objects.get(kind="member.status_out", direction="out")
        jidanpu_member.queue_status_out(self.order, manual=True)
        upstream = consume_fixture._Upstream(status=ApiError("合成写入失败", code=None), remote_sta="代练中")
        with consume_fixture._stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(stale, client=consume_fixture.CLIENT), "failed")
        stale.refresh_from_db()
        self.assertEqual(stale.attempts, 1)
        self.assertNotIn("不再自动重试", stale.reason)

    def _new_intent_while_sending(self, *, fail_old_write):
        self._own_linked_order(status=OrderStatus.ISSUE)
        upstream = consume_fixture._Upstream(remote_sta="代练中")
        moment = timezone.now()
        with mock.patch.object(timezone, "now", return_value=moment):
            jidanpu_member.queue_status_out(self.order, manual=True)
            jidanpu_member.queue_status_out(self.order, manual=True)
            event = JidanpuSyncEvent.objects.get(kind="member.status_out", direction="out")

            def write(client, **payload):
                jidanpu_member.queue_status_out(self.order, manual=True)
                if fail_old_write:
                    raise ApiError("合成旧请求失败", code=None)
                upstream.set_order_status(client, **payload)

            with consume_fixture._stub(upstream), mock.patch.object(jidanpu, "set_order_status", side_effect=write):
                jidanpu_member.consume_event(event, client=consume_fixture.CLIENT)
        event.refresh_from_db()
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.attempts, 0)
        with consume_fixture._stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(event, client=consume_fixture.CLIENT), "done")
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_an_old_success_cannot_finish_a_new_intent_with_the_same_clock_value(self):
        self._new_intent_while_sending(fail_old_write=False)

    def test_an_old_failure_cannot_spend_a_new_intents_retry_budget(self):
        self._new_intent_while_sending(fail_old_write=True)


class StatusIntentConcurrencyTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        connection.set_schema_to_public()
        cls.tenant = get_tenant_model()(schema_name="member_intent_concurrency")
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(tenant=cls.tenant, domain="member-intent-race.test.invalid")
        cls.domain.save()
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def test_a_new_request_waiting_for_an_old_completion_stays_pending(self):
        self.assertTrue(connection.get_autocommit())
        game = Game.objects.create(name="合成并发游戏")
        user = get_user_model().objects.create_user(username="member-intent-race")
        binding = JidanpuMemberBinding.objects.create(
            user=user, jidanpu_base_url=consume_fixture.BASE_URL,
            account="race-account", password="race-password", enabled=True,
        )
        order = Order.objects.create(game=game, order_no=consume_fixture.ORDER_NO, status=OrderStatus.ISSUE)
        JidanpuMemberOrderLink.objects.create(binding=binding, order=order, jidanpu_order_id=consume_fixture.JID)
        jidanpu_member.queue_status_out(order, from_status=OrderStatus.IN_PROGRESS)
        event = JidanpuSyncEvent.objects.get(kind="member.status_out", direction="out")
        started = threading.Event()
        result = {}

        def enqueue():
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name):
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_backend_pid()")
                        result["pid"] = cursor.fetchone()[0]
                    started.set()
                    result["queued"] = jidanpu_member.queue_status_out(order, manual=True)
            except Exception as exc:
                result["error"] = exc
            finally:
                connections.close_all()

        worker = threading.Thread(target=enqueue, daemon=True)
        upstream = consume_fixture._Upstream(remote_sta="代练中")
        try:
            with transaction.atomic():
                JidanpuSyncEvent.objects.select_for_update().get(pk=event.pk)
                worker.start()
                self.assertTrue(started.wait(10))
                blocked = False
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [result["pid"]])
                        blocked = cursor.fetchone()[0]
                    if blocked:
                        break
                    time.sleep(0.02)
                self.assertTrue(blocked, "新请求应在独立连接中等待旧任务行锁")
                with consume_fixture._stub(upstream):
                    self.assertEqual(jidanpu_member.consume_event(event, client=consume_fixture.CLIENT), "done")
        finally:
            if worker.ident is not None:
                worker.join(10)
        self.assertFalse(worker.is_alive())
        if "error" in result:
            raise result["error"]
        pending = JidanpuSyncEvent.objects.filter(kind="member.status_out", status="pending")
        self.assertEqual(pending.count(), 1)
        self.assertIs(pending.get().payload["manual"], True)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
