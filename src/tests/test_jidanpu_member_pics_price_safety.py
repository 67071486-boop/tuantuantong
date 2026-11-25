from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.test import override_settings
from qcloud_cos.cos_exception import CosClientError, CosServiceError

from apps.business import jidanpu, jidanpu_member as member, jidanpu_upstream_pics as pics
from apps.business.models import (
    EmpType, JidanpuMemberBinding, JidanpuMemberOrderLink, Order, OrderScreenshot, OrderStatus, Performance,
)
from apps.core.storage import CosStorage
from apps.rbac.permissions import nodes_of
from tests import test_jidanpu_member_pics as fx
from tests import test_jidanpu_member_price_sync as price_fx


@override_settings(MEDIA_STORAGE_BACKEND='local', JIDANPU_MEMBER_PULL_COVERS_SIBLINGS=True)
class ScreenshotSafetyTests(fx._MemberPicsBase):
    def queue(self):
        self.assertEqual(member.queue_pic_repush_for_ready_screenshots(self.order), 1)
        return self.outgoing().filter(status__in=['pending', 'failed']).get()

    def outgoing(self):
        return self._ledger(direction='out')

    def upload(self, event, client, poster=None):
        with mock.patch.object(pics, 'post_to_oss', side_effect=poster or fx._OssRecorder()):
            member.consume_event(event, client=client, http=self.http)
        event.refresh_from_db()
        return self._register_payloads(client)

    def test_control_active_repush_registers_picture(self):
        self._shot()
        event = self.queue()
        actual = self.upload(event, fx._StubClient())
        self.assertEqual([p['orderId'] for p in actual], [fx.JID])
        self.assertEqual(event.status, 'done')

    def test_queued_repush_does_not_upload_to_withdrawn_copy(self):
        self._shot()
        event = self.queue()
        JidanpuMemberOrderLink.objects.filter(pk=self.link.pk).update(withdrawn_at=self.now)
        self.assertEqual(member.queue_pic_repush_for_ready_screenshots(self.order), 0)
        actual = self.upload(event, fx._StubClient())
        self.assertEqual(actual, [], 'confirmed withdrawal must block already queued picture upload')

    def test_partial_upload_retry_preserves_registered_picture(self):
        self._shot(key='audit/one.jpg')
        self._shot(key='audit/two.jpg')
        first_payloads = self.upload(self.queue(), fx._StubClient(), fx._OssRecorder(fail_at=2))
        self.assertEqual(len(first_payloads), 1)
        self.assertEqual(self._ledger().get().status, 'failed')
        self.assertEqual(self.outgoing().get().status, 'done')
        remote_url = first_payloads[0]['imgListStr']
        second_payloads = self.upload(self.queue(), fx._StubClient(pic_envelope=fx._view_pic_shell([{'src':remote_url}])))
        self.assertEqual(len(second_payloads), 1, 'retry should upload only the still missing second picture')

    def assert_first_upload_remains_mapped_after_storage_error(self, error):
        first = self._shot(key="audit/storage-before.jpg")
        self._shot(key="audit/storage-failure.jpg")
        body = mock.Mock()
        body.get_raw_stream.return_value = BytesIO(fx.JPEG_BODY)
        sdk = mock.Mock()
        sdk.get_object.side_effect = [{"Body": body}, error]
        storage = CosStorage.__new__(CosStorage)
        storage.client = sdk
        storage.bucket = "audit-storage"
        with mock.patch.object(pics, "get_storage", return_value=storage):
            registered = self.upload(self.queue(), fx._StubClient())
        self.assertEqual(len(registered), 1)
        failure = self._ledger().get(status="failed")
        self.assertIn(type(error).__name__, failure.reason)
        remote_url = registered[0]["imgListStr"]
        self.assertEqual(member.historical_pushed_pic_keys(self.order, self.binding), {
            first.object_key: {pics.remote_object_key(remote_url)},
        })
        client = fx._StubClient(pic_envelope=fx._view_pic_shell([{"src": remote_url}]))
        self.assertEqual(len(self.upload(self.queue(), client)), 1)

    def test_cos_service_error_preserves_the_successful_picture_mapping(self):
        self.assert_first_upload_remains_mapped_after_storage_error(
            CosServiceError("GET", "synthetic storage failure", 503),
        )

    def test_cos_client_error_preserves_the_successful_picture_mapping(self):
        self.assert_first_upload_remains_mapped_after_storage_error(CosClientError("synthetic timeout"))

    def test_unexpected_second_upload_error_preserves_the_first_mapping(self):
        first = self._shot(key="audit/oss-before.jpg")
        self._shot(key="audit/oss-failure.jpg")
        poster = mock.Mock(side_effect=[None, httpx.InvalidURL("synthetic malformed upload URL")])
        registered = self.upload(self.queue(), fx._StubClient(), poster)

        self.assertEqual(len(registered), 1)
        self.assertIn("InvalidURL", self._ledger().get(status="failed").reason)
        remote_url = registered[0]["imgListStr"]
        self.assertEqual(member.historical_pushed_pic_keys(self.order, self.binding), {
            first.object_key: {pics.remote_object_key(remote_url)},
        })
        client = fx._StubClient(pic_envelope=fx._view_pic_shell([{"src": remote_url}]))
        self.assertEqual(len(self.upload(self.queue(), client)), 1)

    def test_control_completed_upload_is_skipped_when_requeued(self):
        self._shot(key='audit/one.jpg')
        first_payloads = self.upload(self.queue(), fx._StubClient())
        self.assertEqual(len(first_payloads), 1)
        remote_url = first_payloads[0]['imgListStr']
        self.assertEqual(self.upload(self.queue(), fx._StubClient(pic_envelope=fx._view_pic_shell([{'src':remote_url}]))), [])

    def test_picture_arriving_after_upload_started_has_a_followup(self):
        self._shot(key='audit/before.jpg')
        first_event = self.queue()
        poster = fx._OssRecorder()
        def arrive(*args, **kwargs):
            poster(*args, **kwargs)
            self._shot(key='audit/during.jpg')
            self.assertEqual(member.queue_pic_repush_for_ready_screenshots(self.order), 1)
        actual = self.upload(first_event, fx._StubClient(), arrive)
        self.assertEqual(len(actual), 1)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order).count(), 2)
        pending = self.outgoing().filter(status__in=['pending', 'failed']).count()
        self.assertEqual(pending, 1, 'new picture must stay queued for the next upload')
        remote_url = actual[0]['imgListStr']
        poster = fx._OssRecorder()
        client = fx._StubClient(pic_envelope=fx._view_pic_shell([{'src': remote_url}]))
        actual = self.upload(self.outgoing().get(status='pending'), client, poster)
        self.assertEqual(len(poster.calls), 1)
        self.assertEqual(len(actual), 1)
        self.assertEqual(self.outgoing().get().status, 'done')

    def test_control_picture_arriving_after_upload_completed_creates_a_new_task(self):
        self._shot(key='audit/before.jpg')
        first_event = self.queue()
        self.assertEqual(len(self.upload(first_event, fx._StubClient())), 1)
        self._shot(key='audit/after.jpg')
        self.assertNotEqual(self.queue().pk, first_event.pk)

    def defer(self, *, binding=None, link=None):
        binding = binding or self.binding
        link = link or self.link
        outcome = member.apply_remote_completion(
            binding=binding, order=self.order, link=link,
            remote_status='已完成', nodes=nodes_of(self.user), now=self.now,
            client=fx._StubClient(), pic_budget=member.PullPicsBudget(remaining=0),
        )
        self.assertEqual(outcome.verdict, member.REMOTE_DONE_APPLIED)
        return self._ledger(kind='member.status_in').filter(status='skipped', reason__contains='留到下一轮').latest('pk')

    def pull(self, event, *, during_list=None, binding=None):
        client = fx._StubClient(pic_envelope=fx._view_pic_shell([{'src': 'https://audit.test.invalid/returned.jpg'}]))

        def completed_list(*args, **kwargs):
            if during_list is not None:
                during_list()
            return jidanpu.OrderListPage(rows=[], count=0)

        with (
            mock.patch.object(jidanpu, 'fetch_completed_orders_page', side_effect=completed_list),
            mock.patch.object(pics, 'download_pic', return_value=fx.JPEG_BODY),
        ):
            member.pull_statuses(binding or self.binding, now=self.now + timedelta(minutes=10), client=client)
        event.refresh_from_db()
        return [kw['query']['orderId'] for _, path, kw in client.calls if path == fx.VIEW_PIC_PATH]

    def test_control_deferred_pull_uses_the_original_copy(self):
        self.assertEqual(self.pull(self.defer()), [fx.JID])
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order).count(), 1)

    def test_deferred_pull_does_not_follow_replacement_binding_copy(self):
        event = self.defer()
        JidanpuMemberOrderLink.objects.filter(pk=self.link.pk).update(retired_at=self.now)
        self.binding.jidanpu_base_url = 'https://audit-replacement.test.invalid'
        self.binding.save(update_fields=['jidanpu_base_url'])
        JidanpuMemberOrderLink.objects.create(binding=self.binding, order=self.order, jidanpu_order_id='AUDIT-NEW-STORE')
        self.assertEqual(self.pull(event), [], 'old deferred request must not read the replacement store order')

    def test_deferred_pull_keeps_the_original_same_store_copy(self):
        event = self.defer()
        user = User.objects.create_user(username='audit-pics-sibling')
        sibling = JidanpuMemberBinding.objects.create(user=user, jidanpu_base_url=self.binding.jidanpu_base_url, account='audit-other', password='audit-pw', enabled=True)
        JidanpuMemberOrderLink.objects.create(binding=sibling, order=self.order, jidanpu_order_id='AUDIT-OTHER-COPY')
        self.assertEqual(self.pull(event), [fx.JID], 'the deferred job must read its own link, not another copy of the same local order')

    def test_old_store_session_cannot_pull_a_sibling_that_switched_stores(self):
        original = self.defer()
        sibling = JidanpuMemberBinding.objects.create(
            user=User.objects.create_user(username="audit-moved-sibling", is_superuser=True),
            jidanpu_base_url=self.binding.jidanpu_base_url, account="audit-sibling",
            password="synthetic-password", enabled=True,
        )
        siblings_at_start = member._pull_extra_bindings
        replacement_events = []

        def capture_then_switch_stores(binding):
            original_siblings = siblings_at_start(binding)
            self.assertEqual([item.pk for item in original_siblings], [sibling.pk])
            sibling.jidanpu_base_url = "https://audit-sibling-new-store.test.invalid"
            sibling.save(update_fields=["jidanpu_base_url"])
            link = JidanpuMemberOrderLink.objects.create(
                binding=sibling, order=self.order, jidanpu_order_id="AUDIT-SIBLING-NEW-STORE",
            )
            Performance.objects.filter(order=self.order, emp_type=EmpType.MAIN).update(user=sibling.user)
            Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)
            self.order.refresh_from_db()
            replacement_events.append(self.defer(binding=sibling, link=link))
            return original_siblings

        with mock.patch.object(member, "_pull_extra_bindings", side_effect=capture_then_switch_stores):
            self.assertEqual(self.pull(original), [])
        replacement = replacement_events[0]
        replacement.refresh_from_db()
        self.assertEqual(replacement.reason, member.PULL_PICS_DEFERRED_REASON)
        self.assertEqual(self.pull(replacement, binding=sibling), ["AUDIT-SIBLING-NEW-STORE"])
        self.assertEqual(replacement.status, "done")

    def test_stopping_a_same_store_binding_pauses_its_deferred_pull(self):
        sibling = JidanpuMemberBinding.objects.create(
            user=User.objects.create_user(username="audit-paused-sibling", is_superuser=True),
            jidanpu_base_url=self.binding.jidanpu_base_url, account="audit-paused",
            password="synthetic-password", enabled=True,
        )
        link = JidanpuMemberOrderLink.objects.create(
            binding=sibling, order=self.order, jidanpu_order_id="AUDIT-PAUSED-COPY",
        )
        event = self.defer(binding=sibling, link=link)

        def pause_binding():
            JidanpuMemberBinding.objects.filter(pk=sibling.pk).update(enabled=False)

        self.assertEqual(self.pull(event, during_list=pause_binding), [])
        self.assertEqual(event.reason, member.PULL_PICS_DEFERRED_REASON)
        JidanpuMemberBinding.objects.filter(pk=sibling.pk).update(enabled=True)
        self.assertEqual(self.pull(event), ["AUDIT-PAUSED-COPY"])
        self.assertEqual(event.status, "done")

    def test_rotating_a_same_store_binding_password_keeps_its_deferred_pull(self):
        sibling = JidanpuMemberBinding.objects.create(
            user=User.objects.create_user(username="audit-password-sibling", is_superuser=True),
            jidanpu_base_url=self.binding.jidanpu_base_url, account="audit-password",
            password="synthetic-original", enabled=True,
        )
        link = JidanpuMemberOrderLink.objects.create(
            binding=sibling, order=self.order, jidanpu_order_id="AUDIT-PASSWORD-COPY",
        )
        event = self.defer(binding=sibling, link=link)

        def change_password():
            JidanpuMemberBinding.objects.filter(pk=sibling.pk).update(password="synthetic-replacement")

        self.assertEqual(self.pull(event, during_list=change_password), [])
        self.assertEqual(event.reason, member.PULL_PICS_DEFERRED_REASON)
        self.assertEqual(self.pull(event), ["AUDIT-PASSWORD-COPY"])
        self.assertEqual(event.status, "done")

    def test_deferred_pull_rechecks_the_withdrawal_after_the_list_read(self):
        event = self.defer()

        def withdraw():
            JidanpuMemberOrderLink.objects.filter(pk=self.link.pk).update(withdrawn_at=self.now)

        self.assertEqual(self.pull(event, during_list=withdraw), [])
        self.assertIn('未补拉截图', event.reason)
        self.assertFalse(OrderScreenshot.objects.filter(order=self.order).exists())

    def test_an_older_read_does_not_close_a_new_binding_task(self):
        original = self.defer()
        new_events = []

        def rebind():
            JidanpuMemberOrderLink.objects.filter(pk=self.link.pk).update(retired_at=self.now)
            replacement_binding = JidanpuMemberBinding.objects.get(pk=self.binding.pk)
            replacement_binding.jidanpu_base_url = 'https://audit-replacement.test.invalid'
            replacement_binding.save(update_fields=['jidanpu_base_url'])
            replacement_link = JidanpuMemberOrderLink.objects.create(
                binding=replacement_binding, order=self.order, jidanpu_order_id='AUDIT-NEW-STORE',
            )
            Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)
            self.order.refresh_from_db()
            new_events.append(self.defer(binding=replacement_binding, link=replacement_link))

        self.assertEqual(self.pull(original, during_list=rebind), [])
        replacement = new_events[0]
        self.assertNotEqual(replacement.pk, original.pk)
        replacement.refresh_from_db()
        self.assertEqual(replacement.status, 'skipped')
        self.assertIn('留到下一轮', replacement.reason)
        self.binding.refresh_from_db()
        self.assertEqual(self.pull(replacement), ['AUDIT-NEW-STORE'])
        self.assertEqual(replacement.status, 'done')

    def test_a_legacy_deferred_task_without_a_link_snapshot_is_not_guessed(self):
        event = self.defer()
        event.payload = {'member_binding_id': self.binding.pk}
        event.save(update_fields=['payload'])
        self.assertEqual(self.pull(event), [])
        self.assertEqual(event.status, 'skipped')
        self.assertIn('无法确认', event.reason)
        self.assertIsNotNone(event.processed_at)

    def test_a_replaced_copy_is_not_adopted_by_an_old_repush(self):
        self._shot()
        event = self.queue()
        JidanpuMemberOrderLink.objects.filter(pk=self.link.pk).update(retired_at=self.now)
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order, jidanpu_order_id='AUDIT-REPLACEMENT',
        )
        self.assertEqual(self.upload(event, fx._StubClient()), [])
        self.assertEqual(event.status, 'skipped')
        self.assertIn('原下家副本', event.reason)

    def test_late_old_picture_trigger_does_not_replace_the_new_store_task(self):
        self._shot()
        real_queue = member.queue_member_event
        replacement_events = []

        def enqueue_new_store_first(order, old_binding, **task):
            self.assertEqual(task["payload"][member.PIC_LINK_SNAPSHOT_KEY]["id"], self.link.pk)
            JidanpuMemberOrderLink.objects.filter(pk=self.link.pk).update(retired_at=self.now)
            current_binding = JidanpuMemberBinding.objects.get(pk=old_binding.pk)
            current_binding.jidanpu_base_url = "https://audit-new-picture-store.test.invalid"
            current_binding.save(update_fields=["jidanpu_base_url"])
            JidanpuMemberOrderLink.objects.create(
                binding=current_binding, order=order, jidanpu_order_id="AUDIT-NEW-PICTURE-STORE",
            )
            replacement_events.append(real_queue(
                order, current_binding, kind=member.KIND_ASSIGN, rearm=True, payload=member.PIC_RE_PUSH_PAYLOAD,
            ))
            return real_queue(order, old_binding, **task)

        with mock.patch.object(member, "queue_member_event", side_effect=enqueue_new_store_first):
            member.queue_pic_repush_for_ready_screenshots(self.order)
        event = replacement_events[0]
        actual = self.upload(event, fx._StubClient())
        self.assertEqual([item["orderId"] for item in actual], ["AUDIT-NEW-PICTURE-STORE"])
        self.assertEqual(event.status, "done")
        for obsolete in self.outgoing().exclude(pk=event.pk):
            self.assertEqual(self.upload(obsolete, fx._StubClient()), [])
            self.assertEqual(obsolete.status, "skipped")


class DownstreamPriceSafetyTests(price_fx._Fixture):
    def test_price_update_does_not_relist_a_copy_already_taken_downstream(self):
        class TakenCopy(price_fx._PriceStub):
            def fetch_order_by_key1(self, *args, **kwargs):
                row = super().fetch_order_by_key1(*args, **kwargs)
                return {**row, "orderSta": "代练中", "recManName": "合成下家打手"}

        order = self._order(price_fx._no(81), status=OrderStatus.WAITING_GRAB)
        self._link(order, in_hall=True)
        self.assertEqual(member.queue_price_update(order), 1)
        event = self._price_events(order).get()
        remote = TakenCopy(in_hall=True, apply_throw=False)
        with price_fx._stub(remote):
            member.consume_event(event, client=price_fx.CLIENT)
        self.assertIn('change_order_price', remote.names())
        self.assertIn('fetch_order_by_key1', remote.names())
        event.refresh_from_db()
        self.assertNotIn('throw_order', remote.names())
        self.assertEqual(event.status, 'done')
        self.assertEqual(remote.price, Decimal('88.88'))

    def test_price_update_does_not_relist_without_confirmed_hall_state(self):
        order = self._order(price_fx._no(82), status=OrderStatus.WAITING_GRAB)
        self._link(order, in_hall=True)
        self.assertEqual(member.queue_price_update(order), 1)
        event = self._price_events(order).get()
        remote = price_fx._PriceStub()
        remote.is_throw = None
        with price_fx._stub(remote):
            member.consume_event(event, client=price_fx.CLIENT)
        self.assertEqual(remote.names(), ['change_order_price', 'fetch_order_by_key1'])
        event.refresh_from_db()
        self.assertEqual(event.status, 'failed')
        self.assertIn('无法确认', event.reason)
