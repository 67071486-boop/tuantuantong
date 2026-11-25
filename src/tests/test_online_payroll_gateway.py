from datetime import datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import httpx
from django.test import SimpleTestCase

from apps.accounts.jeepay import sign_params
from apps.business.online_payroll_gateway import (
    PayrollCredentials, PayrollGateway, PayrollGatewayError, _signed_value, safe_payment_url,
)


class PayrollGatewayTests(SimpleTestCase):
    def setUp(self):
        self.creds = PayrollCredentials('merchant-fixture', 'app-fixture', 'synthetic-key')
        self.child = SimpleNamespace(mch_batch_no='batch-fixture')
        self.transfer = SimpleNamespace(mch_order_no='detail-fixture', amount=Decimal('3.25'), gateway_batch=self.child)
        self.recipient = {'account': 'fixture@example.test', 'name': '测试收款人'}

    def invoke(self, method, response_data, *, code=0, signature=None):
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload['mchNo'], self.creds.merchant)
            self.assertEqual(payload['appId'], self.creds.app_id)
            self.assertEqual(payload['sign'], sign_params(payload, self.creds.secret))
            fields = {k: _signed_value(v) if isinstance(v, (dict, list, bool)) else v
                      for k, v in response_data.items()}
            sign = signature or sign_params(fields, self.creds.secret)
            return httpx.Response(200, text=_signed_value({'code': code, 'data': response_data, 'sign': sign}))

        with httpx.Client(base_url='https://gateway.example.test', transport=httpx.MockTransport(handler)) as client:
            return method(PayrollGateway(self.creds, client))

    def test_batch_short_link_binds_exact_details(self):
        data = {'payDataType': 'SHORT_URL', 'payDataInfo': {'url': 'https://p.tb.cn/synthetic'},
                'detailList': [{'mchOrderNo': 'detail-fixture', 'amount': 325}]}
        seal = lambda gateway: gateway.seal(self.child, [self.transfer])
        self.assertEqual(self.invoke(seal, data), 'https://p.tb.cn/synthetic')
        data['detailList'][0]['amount'] = 326
        with self.assertRaises(PayrollGatewayError):
            self.invoke(seal, data)

    def test_computer_link_queries_same_batch_with_pc_target(self):
        payload = {'payDataType': 'SHORT_URL', 'payDataInfo': '{"url":"https://p.tb.cn/pc-fixture"}',
                   'detailList': '[{"mchOrderNo":"detail-fixture","amount":325}]'}
        gateway = PayrollGateway(self.creds, None)
        from unittest.mock import patch

        with patch.object(gateway, '_request', return_value=payload) as request:
            self.assertEqual(gateway.seal(self.child, [self.transfer], desktop=True), 'https://p.tb.cn/pc-fixture')
        request.assert_called_once_with('/api/transferBatch/query', mchBatchNo='batch-fixture',
                                        targetTerminalType='PC', initializeCodeType='SHORT_URL', includeDetail='1')
        with patch.object(gateway, '_request', return_value=dict(payload, batchState=3)):
            with self.assertRaisesMessage(PayrollGatewayError, '批次已结束'):
                gateway.seal(self.child, [self.transfer], desktop=True)

    def test_query_verifies_binding_and_preserves_decimal_signature(self):
        data = {'mchNo': self.creds.merchant, 'appId': self.creds.app_id, 'amount': 325, 'mchOrderNo': 'detail-fixture',
                'accountNo': self.recipient['account'], 'accountName': self.recipient['name'],
                'ifCode': 'alitransympay', 'entryType': 'ALIPAY_CASH', 'channelExtra': '{"mchBatchNo":"batch-fixture"}',
                'state': 2, 'transferId': 'transfer-fixture', 'channelOrderNo': 'channel-fixture',
                'successTime': 1794625554000, 'mchOrderFeeAmount': Decimal('0.000000')}
        query = lambda gateway: gateway.query(self.transfer, self.recipient)
        result = self.invoke(query, data)
        self.assertEqual(result.state, 2)
        self.assertEqual(result.success_time, datetime.fromtimestamp(1794625554, tz=timezone.utc))
        self.assertEqual(result.channel_order_no, 'channel-fixture')
        with self.assertRaises(PayrollGatewayError):
            self.invoke(query, data, signature='invalid')
        data['accountNo'] = 'other@example.test'
        with self.assertRaises(PayrollGatewayError):
            self.invoke(query, data)

    def test_submit_rejects_failed_envelope_even_when_data_looks_valid(self):
        submit = lambda gateway: gateway.submit(self.transfer, self.recipient)
        with self.assertRaises(PayrollGatewayError):
            self.invoke(submit, {'mchOrderNo': 'detail-fixture', 'state': 0}, code=9999)

    def test_query_translates_known_failure_without_returning_gateway_text(self):
        data = {'mchNo': self.creds.merchant, 'appId': self.creds.app_id, 'amount': 325, 'mchOrderNo': 'detail-fixture',
                'accountNo': self.recipient['account'], 'ifCode': 'alitransympay', 'entryType': 'ALIPAY_CASH',
                'channelExtra': '{"mchBatchNo":"batch-fixture"}', 'state': 3, 'errMsg': self.recipient['account']}
        query = lambda gateway: gateway.query(self.transfer, self.recipient)
        for code, message in (('EXCEED_LIMIT_SM_MIN_AMOUNT', '打款金额低于支付宝单笔最低限额'),
                              ('UNRECOGNIZED', '转账失败'), ([], '转账失败')):
            with self.subTest(code=code):
                result = self.invoke(query, dict(data, errCode=code))
                self.assertEqual(result.state, 3)
                self.assertEqual(result.error, message)

    def test_url_rejects_impersonation_and_unencrypted_links(self):
        for value in ('https://p.tb.cn.example.test/p', 'https://p.tb.cn@evil.test/p', 'http://p.tb.cn/x',
                      'https://p.tb.cn:444/x', 'https://p.tb.cn/\\x', 'https://p.tb.cn\n/x'):
            with self.subTest(value=value):
                self.assertIsNone(safe_payment_url(value))


import unittest
from pathlib import Path

_HAS_DEPLOY = (Path(__file__).resolve().parents[2] / "deploy").is_dir()

@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class PayrollDeployTests(SimpleTestCase):
    def test_deploy_wires_polling_and_prevents_unsafe_legacy_rollback(self):
        from pathlib import Path

        deploy = Path(__file__).resolve().parents[2] / 'deploy'
        base = 'tuantuantong-online-payroll'
        installer = (deploy / 'deploy.sh').read_text()
        for suffix in ('service', 'timer'):
            unit = f'{base}.{suffix}'
            self.assertIn(f'deploy/systemd/{unit}', installer)
            self.assertGreaterEqual(installer.count(f'$R/shared/{unit}'), 2)
        self.assertIn('manage.py poll_online_payroll', (deploy / 'systemd' / f'{base}.service').read_text())
        self.assertIn(f'Unit={base}.service', (deploy / 'systemd' / f'{base}.timer').read_text())
        self.assertIn(f'ONLINE_PAYROLL_UNITS=({base}.timer)', (deploy / 'activate.sh').read_text())
        rollback = (deploy / 'rollback.sh').read_text()
        self.assertIn(f'ONLINE_PAYROLL_UNITS=({base}.timer)', rollback)
        self.assertLess(rollback.index('manage.py poll_online_payroll --check-rollback'),
                        rollback.index('ln -sfn "$PREV" "$R/current"'))
