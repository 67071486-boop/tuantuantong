"""Signed requests to the merchant gateway using the selected payer's app."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hmac
import json
import time
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

from apps.accounts.jeepay import sign_params
from apps.settlement_control.models import Authorization


class PayrollGatewayError(Exception):
    """The gateway did not produce a result we can act on."""


@dataclass(frozen=True, repr=False)
class PayrollCredentials:
    merchant: str
    app_id: str
    secret: str

    @classmethod
    def for_authorization(cls, request_id):
        row = Authorization.objects.filter(client='local', request_id=request_id).first()
        if not row or not row.app_id or not settings.SETTLEMENT_MERCHANT:
            raise PayrollGatewayError('付款应用暂不可用')
        try:
            box = Fernet(settings.SETTLEMENT_FERNET_KEY.encode('ascii'))
            secret = json.loads(box.decrypt(bytes(row.ciphertext)))['secret']
            if not isinstance(secret, str) or not secret:
                raise ValueError
        except (KeyError, TypeError, ValueError, InvalidToken):
            raise PayrollGatewayError('付款应用资料暂不可用') from None
        return cls(settings.SETTLEMENT_MERCHANT, row.app_id, secret)


def _signed_value(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(key, ensure_ascii=False) + ':' + _signed_value(item)
                              for key, item in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(map(_signed_value, value)) + ']'
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def verified_payload(body, key):
    if not isinstance(body, dict) or type(body.get('code')) is not int or body['code'] != 0:
        raise PayrollGatewayError('计全付没有确认请求结果')
    data, signature = body.get('data'), body.get('sign')
    if not isinstance(data, dict) or not isinstance(signature, str) or not signature.isascii():
        raise PayrollGatewayError('计全付返回缺少可验证数据')
    fields = {k: _signed_value(v) if isinstance(v, (dict, list, bool)) else v for k, v in data.items()}
    if not hmac.compare_digest(sign_params(fields, key), signature.upper()):
        raise PayrollGatewayError('计全付返回验签失败')
    return data


PAYMENT_HOSTS = frozenset({'p.tb.cn', 'ur.alipay.com', 'qr.alipay.com', 'open.alipay.com',
                           'openauth.alipay.com', 'b.alipay.com', 'render.alipay.com'})
TRANSFER_FAILURE_MESSAGES = {
    'EXCEED_LIMIT_SM_MIN_AMOUNT': '打款金额低于支付宝单笔最低限额',
}


def safe_payment_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 or c == '\\' for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == 'https' and parsed.hostname in PAYMENT_HOSTS
                and parsed.username is None and parsed.password is None and parsed.port in (None, 443)):
            return value
    except ValueError:
        pass
    return None


def _object(value):
    return json.loads(value, parse_float=Decimal) if isinstance(value, str) else value


@dataclass(frozen=True)
class TransferResult:
    state: int
    success_time: datetime | None
    transfer_no: str
    channel_order_no: str
    error: str = ''


class PayrollGateway:
    def __init__(self, credentials, http):
        self.credentials = credentials
        self.http = http

    def _request(self, path, **data):
        credentials = self.credentials
        payload = dict(data, mchNo=credentials.merchant, appId=credentials.app_id,
                       reqTime=str(time.time_ns() // 1_000_000), version='1.0', signType='MD5')
        payload['sign'] = sign_params(payload, credentials.secret)
        try:
            response = self.http.post(path, json=payload)
            response.raise_for_status()
            return verified_payload(json.loads(response.text, parse_float=Decimal), credentials.secret)
        except (httpx.HTTPError, ValueError):
            raise PayrollGatewayError('计全付请求结果待核对') from None

    def submit(self, transfer, recipient):
        cents = transfer.amount * 100
        if cents <= 0 or cents != int(cents):
            raise PayrollGatewayError('打款金额不合法')
        data = self._request('/api/transferOrder', mchOrderNo=transfer.mch_order_no,
                             ifCode='alitransympay', entryType='ALIPAY_CASH', amount=int(cents),
                             currency='cny', accountNo=recipient['account'], accountName=recipient['name'],
                             transferDesc='报酬结算', channelExtra=json.dumps(
                                 {'mchBatchNo': transfer.gateway_batch.mch_batch_no}, separators=(',', ':')))
        if data.get('mchOrderNo') != transfer.mch_order_no or type(data.get('state')) is not int or data['state'] != 0:
            raise PayrollGatewayError('计全付未确认转账已归入批次')

    def query(self, transfer, recipient):
        data = self._request('/api/transfer/query', mchOrderNo=transfer.mch_order_no)
        try:
            extra = _object(data.get('channelExtra'))
        except ValueError:
            extra = None
        expected = {'mchNo': self.credentials.merchant, 'appId': self.credentials.app_id,
                    'mchOrderNo': transfer.mch_order_no, 'accountNo': recipient['account'],
                    'amount': int(transfer.amount * 100), 'ifCode': 'alitransympay', 'entryType': 'ALIPAY_CASH'}
        if (any(data.get(k) != value for k, value in expected.items())
                or data.get('accountName') not in (None, '', recipient['name'])
                or not isinstance(extra, dict) or extra.get('mchBatchNo') != transfer.gateway_batch.mch_batch_no):
            raise PayrollGatewayError('计全付明细与原付款单不符')
        state = data.get('state')
        if type(state) is not int or state not in range(5):
            raise PayrollGatewayError('计全付转账状态无法确认')
        paid_at = None
        raw_time = data.get('successTime')
        if state == 2 and type(raw_time) is int and 10**12 <= raw_time < 10**13:
            paid_at = datetime.fromtimestamp(raw_time / 1000, tz=timezone.utc)
        numbers = []
        for name in ('transferId', 'channelOrderNo'):
            value = data.get(name) or ''
            if not isinstance(value, str) or len(value) > 128:
                raise PayrollGatewayError('计全付流水号格式异常')
            numbers.append(value)
        error = ''
        if state == 3:
            code = data.get('errCode')
            error = TRANSFER_FAILURE_MESSAGES.get(code, '转账失败') if isinstance(code, str) else '转账失败'
        elif state == 4:
            error = '转账关闭'
        return TransferResult(state, paid_at, *numbers, error)

    def seal(self, gateway_batch, transfers, *, desktop=False):
        data = self._request('/api/transferBatch/query', mchBatchNo=gateway_batch.mch_batch_no,
                             targetTerminalType='PC' if desktop else 'tinyapp',
                             initializeCodeType='SHORT_URL', includeDetail='1')
        if data.get('batchState') in (2, 3, 4, '2', '3', '4'):
            raise PayrollGatewayError('计全付批次已结束，请刷新打款结果')
        try:
            details = _object(data.get('detailList'))
            pay_info = _object(data.get('payDataInfo'))
        except ValueError:
            raise PayrollGatewayError('计全付批次返回无法解析') from None
        expected = {t.mch_order_no: int(t.amount * 100) for t in transfers}
        if (not isinstance(details, list) or len(details) != len(expected)
                or any(not isinstance(row, dict) or not isinstance(row.get('mchOrderNo'), str) for row in details)
                or {row['mchOrderNo']: row.get('amount') for row in details} != expected
                or any(data.get(k) not in (None, v) for k, v in (
                    ('mchNo', self.credentials.merchant), ('appId', self.credentials.app_id),
                    ('mchBatchNo', gateway_batch.mch_batch_no)))):
            raise PayrollGatewayError('计全付批次明细与原付款单不符')
        url = safe_payment_url(pay_info.get('url')) if isinstance(pay_info, dict) else None
        if data.get('payDataType') != 'SHORT_URL' or not url:
            raise PayrollGatewayError('计全付未提供可用的支付宝付款链接')
        return url
