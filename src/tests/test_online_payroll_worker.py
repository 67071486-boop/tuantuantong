from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.business import online_payroll_worker as worker
from apps.business.online_payroll_gateway import PayrollGatewayError


class PayrollWorkerTests(SimpleTestCase):
    def test_desktop_query_preserves_mobile_credential_and_rejects_same_link(self):
        child = MagicMock(pk=4)
        existing = 'https://p.tb.cn/mobile-fixture'
        transfers = [SimpleNamespace(mch_order_no='original-detail', amount=Decimal('3.25'))]
        child.transfers.all.return_value = transfers
        with (patch.object(worker, '_batch_lock', return_value=nullcontext(True)),
              patch.object(worker.OnlinePayrollGatewayBatch, 'objects') as batches,
              patch.object(worker.domain, 'gateway_can_pay', return_value=True),
              patch.object(worker, 'cipher'), patch.object(worker, 'decrypt', return_value=existing),
              patch.object(worker, 'PayrollCredentials'), patch.object(worker, 'PayrollGateway') as gateway):
            batches.select_related.return_value.get.return_value = child
            gateway.return_value.seal.side_effect = [existing, 'https://p.tb.cn/pc-fixture']
            with self.assertRaisesMessage(PayrollGatewayError, '未返回独立的电脑付款链接'):
                worker.desktop_payment_url(child.pk)
            self.assertEqual(worker.desktop_payment_url(child.pk), 'https://p.tb.cn/pc-fixture')
            gateway.return_value.seal.assert_called_with(child, transfers, desktop=True)
            gateway.return_value.submit.assert_not_called()
            child.save.assert_not_called()

    def advance(self, child_status, *transfer_states):
        child = MagicMock(pk=4, status=child_status)
        loads = [[SimpleNamespace(pk=i, status=state, finalized_at=None, updated_at=i)
                  for i, state in enumerate(states, 1)] for states in transfer_states]
        child.transfers.select_related.return_value.order_by.side_effect = loads
        with (patch.object(worker, '_batch_lock', return_value=nullcontext(True)),
              patch.object(worker, '_recipient', return_value={'account': 'fixture@example.test', 'name': '合成姓名'}),
              patch.object(worker, 'PayrollCredentials'),
              patch.object(worker, 'PayrollGateway') as gateway,
              patch.object(worker.OnlinePayrollGatewayBatch, 'objects') as batches,
              patch.object(worker.OnlinePayrollTransfer, 'objects'),
              patch.object(worker, 'domain') as domain):
            batches.select_related.return_value.get.return_value = child
            gateway.return_value.query.side_effect = PayrollGatewayError('结果未知')
            gateway.return_value.seal.return_value = 'https://p.tb.cn/fixture'
            worker.advance_gateway_batch(child.pk, object())
            return gateway.return_value, domain

    def test_crashed_submission_is_only_queried_and_does_not_seal_partial_batch(self):
        gateway, domain = self.advance('staged', ['submitting', 'staged'], ['submitting', 'staged'])
        self.assertEqual(gateway.query.call_count, 1)
        gateway.submit.assert_not_called()
        gateway.seal.assert_not_called()
        domain.begin_submission.assert_not_called()

    def test_initial_batch_is_sealed_only_after_each_order_was_created(self):
        gateway, domain = self.advance('staged', ['staged', 'staged'], ['staged', 'staged'], ['created', 'created'])
        self.assertEqual(gateway.submit.call_count, 2)
        domain.begin_sealing.assert_called_once_with(4)
        domain.record_sealed.assert_called_once_with(4, payment_url='https://p.tb.cn/fixture')

    def test_existing_batch_recovery_fetches_link_without_resubmitting_orders(self):
        gateway, domain = self.advance('unknown', ['processing', 'succeeded'], ['processing', 'succeeded'])
        gateway.submit.assert_not_called()
        gateway.seal.assert_called_once()
        domain.begin_sealing.assert_not_called()

    def test_initial_processing_skips_batch_already_advanced_by_timer(self):
        child = MagicMock(status='payable')
        with (patch.object(worker, '_batch_lock', return_value=nullcontext(True)),
              patch.object(worker.OnlinePayrollGatewayBatch, 'objects') as batches,
              patch.object(worker.PayrollCredentials, 'for_authorization') as credentials):
            batches.select_related.return_value.get.return_value = child
            self.assertFalse(worker.advance_gateway_batch(4, object(), initial_only=True))
        credentials.assert_not_called()
