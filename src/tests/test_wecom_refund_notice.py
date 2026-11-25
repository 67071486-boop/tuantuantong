from datetime import timedelta
from time import monotonic
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context
from rest_framework.exceptions import APIException

from apps.business.models import Game, Order
from apps.internal_chat.models import SystemNoticeSettings
from apps.platform.provisioning import provision_tenant
from apps.wecom import refund_notices
from apps.wecom.archive import ArchiveError
from apps.wecom.models import (
    WecomAppConfig, WecomArchiveMessage, WecomArchiveOrderGroup, WecomGroupChat, WecomRefundNotice, WecomZoneKeywordHit,
)
from tests.p5_auth_helpers import make_logged_in_client


class RefundNoticeTests(TenantTestCase):
    @classmethod
    def get_test_schema_name(cls):
        return "kexing"

    def setUp(self):
        self.start = timezone.now() - timedelta(minutes=5)
        self.now = timezone.now() - timedelta(minutes=1)
        self.enterContext(override_settings(
            WECOM_ARCHIVE_TENANT_SCHEMA="kexing", WECOM_ARCHIVE_PROGRAM_ID="synth-refund-program",
            WECOM_ZONE_ENABLED=True, TT_CHAT_ORDER_GROUPS_ENABLED=True,
            WECOM_REFUND_NOTICE_RULE_ID="synth-refund-rule", WECOM_REFUND_NOTICE_CHAT_ID="synth-refund-chat",
            WECOM_REFUND_NOTICE_START_AT=self.start.isoformat(),
        ))
        self.config = WecomAppConfig.objects.create(
            tenant_schema="kexing", enabled=True, corp_id="synth-refund-corp", agent_id=7,
            secret="synthetic-secret", identity_revision=4,
        )
        self.game = Game.objects.create(name="合成规则测试游戏")
        self.contact = self._staff("refund-contact")
        self.order = Order.objects.create(
            game=self.game, order_no="20261122000000001801", source_order_no="SYN-180001",
            sales_user=self.contact,
        )
        self.chat = WecomGroupChat.objects.create(
            chat_id="synth-refund-chat", name=self.order.source_order_no, identity_revision=4,
        )

    def _staff(self, username, nodes=("order.view_detail",)):
        client = make_logged_in_client(username=username, groups=("客服",), nodes=nodes)
        return get_user_model().objects.get(pk=client.session["_auth_user_id"])

    def _message(self, msg_id, *, chat=None, sent_at=None):
        return WecomArchiveMessage.objects.create(
            identity_revision=4, msg_id=msg_id, chat_id=chat or self.chat.chat_id,
            sent_at=sent_at or self.now, encrypted_secret_key="synthetic-encrypted-key", public_key_version=1,
        )

    def _hit(self, msg_id, rule_id="synth-refund-rule"):
        return WecomZoneKeywordHit.objects.create(
            identity_revision=4, program_id="synth-refund-program", msg_id=msg_id,
            rule_id=rule_id, has_hit_keyword=True,
        )

    def test_waits_for_summary_sends_once_and_routes_to_current_sales_contact(self):
        context = self._message("synth-context", sent_at=self.now - timedelta(minutes=1))
        match = self._message("synth-match")
        self._message("synth-different-chat", chat="synth-other-chat")
        self._message("synth-newer", sent_at=self.now + timedelta(seconds=1))
        hit = self._hit(match.msg_id)
        updated_contact = self._staff("changed-refund-contact")
        summary = "群里提出退款，尚未说明原因。"
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", side_effect=[
                {"jobid": "synth-summary-job"}, {"status": 0}, {"status": 1, "response_data": summary},
            ]) as call,
            patch.object(refund_notices, "send_order_notification", return_value={"id": 42}) as send,
        ):
            refund_notices.run_due(now=self.now + timedelta(minutes=1))
            delivery = WecomRefundNotice.objects.get(hit=hit)
            self.assertEqual(delivery.order_id, self.order.pk)
            self.assertEqual(delivery.summary_job_id, "synth-summary-job")
            sent_messages = call.call_args.args[2]["msg_list"]
            self.assertEqual([row["msgid"] for row in sent_messages], [context.msg_id, match.msg_id])
            send.assert_not_called()

            Order.objects.filter(pk=self.order.pk).update(sales_user=updated_contact)
            refund_notices.run_due(now=self.now + timedelta(minutes=2))
            send.assert_not_called()

            refund_notices.run_due(now=self.now + timedelta(minutes=3))
            delivery.refresh_from_db()
            self.assertIsNotNone(delivery.sent_at)
            self.assertEqual(delivery.recipient_id, updated_contact.pk)
            send.assert_called_once()
            self.assertEqual(send.call_args.args, (updated_contact,))
            self.assertEqual(send.call_args.kwargs["order_no"], self.order.order_no)
            self.assertIn(summary, send.call_args.kwargs["text"])
            self.assertIn("我要退款", send.call_args.kwargs["text"])
            refund_notices.run_due(now=self.now + timedelta(minutes=5))
            send.assert_called_once()

    def test_only_new_hits_of_the_configured_rule_and_chat_are_bound(self):
        self._hit(self._message("synth-old-hit", sent_at=self.start - timedelta(seconds=1)).msg_id)
        self._hit(self._message("synth-other-group-hit", chat="synth-other-chat").msg_id)
        self._hit(self._message("synth-other-rule-hit").msg_id, rule_id="synth-other-rule")
        self._hit("synth-not-archived-yet")
        with patch.object(refund_notices, "call_zone_operation") as model:
            self.assertEqual(refund_notices.run_due(), 0)
            model.assert_not_called()
        self.assertFalse(WecomRefundNotice.objects.exists())

    def test_disabled_window_never_sends_and_resume_only_processes_new_hits(self):
        rules = SystemNoticeSettings.objects.create(pk=1, wecom_aftersales=False)
        missed = self._hit(self._message("refund-while-disabled", sent_at=self.now).msg_id)
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", return_value={"jobid": "synth-resumed"}) as model,
            patch.object(refund_notices, "send_order_notification") as send,
        ):
            self.assertEqual(refund_notices.run_due(now=self.now), 0)
            model.assert_not_called()
            send.assert_not_called()
            self.assertFalse(WecomRefundNotice.objects.exists())

            cutoff = self.now + timedelta(seconds=10)
            rules.wecom_aftersales = True
            rules.wecom_aftersales_resumed_at = cutoff
            rules.save(update_fields=["wecom_aftersales", "wecom_aftersales_resumed_at"])
            eligible = self._hit(self._message("refund-after-resume", sent_at=cutoff).msg_id)
            refund_notices.run_due(now=cutoff)
            self.assertFalse(WecomRefundNotice.objects.filter(hit=missed).exists())
            self.assertEqual(WecomRefundNotice.objects.get(hit=eligible).summary_job_id, "synth-resumed")
            model.assert_called_once()

    def test_rule_disabled_after_recipient_resolution_does_not_send(self):
        hit = self._hit(self._message("refund-disabled-during-delivery").msg_id)
        notice = WecomRefundNotice.objects.create(
            hit=hit, order=self.order, chat_id=self.chat.chat_id,
            message_sent_at=self.now, next_attempt_at=self.now, summary="合成会话摘要",
        )

        def resolve_then_disable(*args):
            SystemNoticeSettings.objects.create(pk=1, wecom_aftersales=False)
            return [self.contact]

        with (
            patch.object(refund_notices, "_recipients", side_effect=resolve_then_disable),
            patch.object(refund_notices, "send_order_notification") as send,
        ):
            self.assertEqual(refund_notices.run_due(now=self.now + timedelta(minutes=1)), 0)
            send.assert_not_called()
        notice.refresh_from_db()
        self.assertIsNone(notice.sent_at)

    def test_pilot_configuration_on_another_tenant_does_not_read_or_push_hits(self):
        other, _ = provision_tenant(schema_name="synth_refund_other", name="合成非试点", domain="refund-other.test.com")
        with (
            self.settings(WECOM_ARCHIVE_TENANT_SCHEMA="synth_refund_other"), tenant_context(other),
            patch.object(refund_notices, "call_zone_operation") as model,
            patch.object(refund_notices, "send_order_notification") as send,
        ):
            WecomAppConfig.objects.create(
                tenant_schema="synth_refund_other", enabled=True, corp_id="synth-other-corp",
                agent_id=7, secret="synthetic-secret", identity_revision=4,
            )
            WecomGroupChat.objects.create(chat_id=self.chat.chat_id, name=self.order.source_order_no, identity_revision=4)
            self._hit(self._message("synth-private-other-hit").msg_id)
            self.assertEqual(refund_notices.run_due(), 0)
            self.assertFalse(WecomRefundNotice.objects.exists())
            model.assert_not_called()
            send.assert_not_called()

    def test_two_active_orders_with_the_same_group_are_not_guessed(self):
        second = Order.objects.create(game=self.game, order_no="20261122000000001802", sales_user=self.contact)
        WecomArchiveOrderGroup.objects.create(
            order=second, config=self.config, identity_revision=4, program_id="synth-refund-program",
            matched_order_no=second.order_no, chat_id=self.chat.chat_id,
        )
        hit = self._hit(self._message("synth-ambiguous-hit").msg_id)
        with patch.object(refund_notices, "call_zone_operation") as model:
            self.assertEqual(refund_notices.run_due(), 0)
            model.assert_not_called()
        delivery = WecomRefundNotice.objects.get(hit=hit)
        self.assertIsNone(delivery.order_id)
        self.assertEqual(delivery.problem, "ambiguous")

    def test_replaced_source_number_and_recycled_order_do_not_block_exact_current_title(self):
        stale = Order.objects.create(
            game=self.game, order_no="20261122000000001803", source_order_no="SYN-NEW-01803",
        )
        recycled = Order.all_objects.create(
            game=self.game, order_no="20261122000000001804", source_order_no=self.order.source_order_no,
            deleted_at=timezone.now(),
        )
        for order, matched_number in ((stale, "SYN-STALE-01803"), (recycled, recycled.order_no)):
            WecomArchiveOrderGroup.objects.create(
                order=order, config=self.config, identity_revision=4, program_id="synth-refund-program",
                matched_order_no=matched_number, chat_id=self.chat.chat_id,
            )
        hit = self._hit(self._message("synth-exact-title-hit").msg_id)
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", return_value={"jobid": "synth-exact-job"}),
        ):
            refund_notices.run_due()
        self.assertEqual(WecomRefundNotice.objects.get(hit=hit).order_id, self.order.pk)

    def test_ambiguous_source_link_does_not_block_a_different_current_title(self):
        old_source = "SYN-DUP-SOURCE"
        old = Order.objects.create(game=self.game, order_no="20261122000000001808", source_order_no=old_source)
        Order.objects.create(game=self.game, order_no="20261122000000001809", source_order_no=old_source)
        WecomArchiveOrderGroup.objects.create(
            order=old, config=self.config, identity_revision=4, program_id="synth-refund-program",
            matched_order_no=old_source, chat_id=self.chat.chat_id,
        )
        hit = self._hit(self._message("synth-duplicate-source-hit").msg_id)
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", return_value={"jobid": "synth-current-title-job"}),
        ):
            refund_notices.run_due()
        self.assertEqual(WecomRefundNotice.objects.get(hit=hit).order_id, self.order.pk)

    def test_pending_group_disregards_stale_title_but_uses_current_saved_group_link(self):
        linked = Order.objects.create(game=self.game, order_no="20261122000000001810", sales_user=self.contact)
        WecomGroupChat.objects.filter(pk=self.chat.pk).update(pending=True)
        hit = self._hit(self._message("synth-pending-group-hit").msg_id)
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", return_value={"jobid": "synth-pending-title-job"}) as model,
        ):
            refund_notices.run_due(now=self.now + timedelta(minutes=1))
            self.assertIsNone(WecomRefundNotice.objects.get(hit=hit).order_id)
            model.assert_not_called()
            WecomArchiveOrderGroup.objects.create(
                order=linked, config=self.config, identity_revision=4, program_id="synth-refund-program",
                matched_order_no=linked.order_no, chat_id=self.chat.chat_id,
            )
            refund_notices.run_due(now=self.now + timedelta(minutes=2))
        self.assertEqual(WecomRefundNotice.objects.get(hit=hit).order_id, linked.pk)

    def test_renamed_group_matching_another_order_never_rebinds_or_dispatches_pending_summary(self):
        new_order = Order.objects.create(game=self.game, order_no="20261122000000001805", sales_user=self.contact)
        hit = self._hit(self._message("synth-rename-group-hit").msg_id)
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", return_value={"jobid": "synth-rename-job"}) as model,
            patch.object(refund_notices, "send_order_notification") as send,
        ):
            refund_notices.run_due(now=self.now + timedelta(minutes=1))
            WecomGroupChat.objects.filter(pk=self.chat.pk).update(name=new_order.order_no)
            refund_notices.run_due(now=self.now + timedelta(minutes=2))
            model.assert_called_once()
            send.assert_not_called()
        delivery = WecomRefundNotice.objects.get(hit=hit)
        self.assertEqual(delivery.order_id, self.order.pk)
        self.assertEqual(delivery.problem, "ambiguous")

    def test_removed_game_access_prevents_delivery_and_receipt_remains_bound(self):
        hit = self._hit(self._message("synth-access-hit").msg_id)
        self.contact.staff_profile.games.clear()
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", side_effect=[
                {"jobid": "synth-denied-job"}, {"status": 1, "response_data": "退款原因待确认。"},
            ]),
            patch.object(refund_notices, "send_order_notification") as send,
        ):
            refund_notices.run_due(now=self.now + timedelta(minutes=1))
            refund_notices.run_due(now=self.now + timedelta(minutes=2))
            send.assert_not_called()
        delivery = WecomRefundNotice.objects.get(hit=hit)
        self.assertEqual(delivery.order_id, self.order.pk)
        self.assertIsNone(delivery.sent_at)
        self.assertEqual(delivery.problem, "no_visible_recipient")

    def test_failed_summary_leaves_outcome_without_sending(self):
        hit = self._hit(self._message("synth-summary-fails-hit").msg_id)
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", side_effect=[
                {"jobid": "synth-failed-job"}, {"status": 2},
            ]) as model,
            patch.object(refund_notices, "send_order_notification") as send,
        ):
            for minutes in (1, 2, 3):
                refund_notices.run_due(now=self.now + timedelta(minutes=minutes))
            self.assertEqual(model.call_count, 2)
            send.assert_not_called()
        delivery = WecomRefundNotice.objects.get(hit=hit)
        self.assertIsNotNone(delivery.closed_at)
        self.assertIsNone(delivery.sent_at)
        self.assertEqual(delivery.problem, "summary_failed")

    def test_delivery_retry_keeps_successful_summary_and_deduplication_key(self):
        hit = self._hit(self._message("synth-delivery-retry-hit").msg_id)
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", side_effect=[
                {"jobid": "synth-delivery-job"}, {"status": 1, "response_data": "双方尚未确认退款。"},
            ]) as model,
            patch.object(refund_notices, "send_order_notification", side_effect=[APIException(), {"id": 42}]) as send,
        ):
            refund_notices.run_due(now=self.now + timedelta(minutes=1))
            refund_notices.run_due(now=self.now + timedelta(minutes=2))
            delivery = WecomRefundNotice.objects.get(hit=hit)
            self.assertIsNone(delivery.sent_at)
            self.assertEqual(delivery.problem, "delivery_unavailable")
            self.assertEqual(delivery.summary, "双方尚未确认退款。")
            refund_notices.run_due(now=self.now + timedelta(minutes=3))
            self.assertEqual(model.call_count, 2)
            self.assertEqual(send.call_args_list[0], send.call_args_list[1])
        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.sent_at)

    def test_stale_selected_delivery_cannot_bypass_retry_time(self):
        hit = self._hit(self._message("synth-stale-selected-hit").msg_id)
        now = timezone.now()
        delivery = WecomRefundNotice.objects.create(
            hit=hit, order=self.order, chat_id=self.chat.chat_id, message_sent_at=self.now,
            summary="客户要求退款。", next_attempt_at=now + timedelta(minutes=2),
        )
        with patch.object(refund_notices, "send_order_notification", return_value={"id": 42}) as send:
            refund_notices._process(delivery.pk, self.config, hit.program_id, now, monotonic() + 1, deliver=True)
            send.assert_not_called()
            refund_notices.run_due(now=now + timedelta(minutes=2))
            send.assert_called_once()

    def test_source_change_before_delivery_order_lock_prevents_stale_routing(self):
        hit = self._hit(self._message("synth-locked-source-hit").msg_id)
        WecomRefundNotice.objects.create(
            hit=hit, order=self.order, chat_id=self.chat.chat_id, message_sent_at=self.now,
            summary="客户要求退款。", next_attempt_at=self.now,
        )
        select_for_update = Order.objects.select_for_update

        def changed_source(*args, **kwargs):
            Order.objects.filter(pk=self.order.pk).update(source_order_no="SYN-DIFFERENT-ORDER")
            return select_for_update(*args, **kwargs)

        with (
            patch.object(Order.objects, "select_for_update", side_effect=changed_source),
            patch.object(refund_notices, "send_order_notification", return_value={"id": 42}) as send,
        ):
            refund_notices.run_due()
            send.assert_not_called()
        self.assertEqual(WecomRefundNotice.objects.get(hit=hit).problem, "unmatched")

    def test_summary_removes_invalid_unicode_and_preserves_valid_text(self):
        hit = self._hit(self._message("synth-invalid-characters-hit").msg_id)
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", side_effect=[
                {"jobid": "synth-invalid-characters-job"},
                {"status": 1, "response_data": "\ud800客户要求退款。\x00\udfff"},
            ]),
            patch.object(refund_notices, "send_order_notification", return_value={"id": 42}) as send,
        ):
            refund_notices.run_due(now=self.now + timedelta(minutes=1))
            refund_notices.run_due(now=self.now + timedelta(minutes=2))
            send.assert_called_once()
        self.assertEqual(WecomRefundNotice.objects.get(hit=hit).summary, "客户要求退款。")

    def test_unavailable_summary_has_bounded_retries_and_never_sends(self):
        hit = self._hit(self._message("synth-summary-retry-hit").msg_id)
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", side_effect=ArchiveError("合成超时")) as model,
            patch.object(refund_notices, "send_order_notification") as send,
        ):
            for minutes in (1, 2, 3, 4, 8, 16, 40, 60):
                refund_notices.run_due(now=self.now + timedelta(minutes=minutes))
            self.assertEqual(model.call_count, 5)
            send.assert_not_called()
        delivery = WecomRefundNotice.objects.get(hit=hit)
        self.assertIsNotNone(delivery.closed_at)
        self.assertIsNone(delivery.sent_at)
        self.assertEqual(delivery.problem, "summary_unavailable")
