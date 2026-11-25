"""计全付后台适配器；未知结果必须停止自动重试。"""
import base64
from collections import Counter
from contextvars import ContextVar
from datetime import timedelta
from io import BytesIO
import json
import logging
import os
import re
import subprocess
import uuid
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from PIL import Image, UnidentifiedImageError
from django.db import connection, transaction
from django.utils import timezone

from django.conf import settings


_private_upstream_call = ContextVar('settlement_private_upstream_call', default=False)


class _PrivateUpstreamFilter(logging.Filter):
    def filter(self, record):
        return not _private_upstream_call.get()


_private_filter = _PrivateUpstreamFilter()
for _logger_name in ('httpx', 'httpcore.proxy', 'httpcore.http2', 'httpcore.socks',
                     'httpcore.http11', 'httpcore.connection'):
    logging.getLogger(_logger_name).addFilter(_private_filter)


class UpstreamError(Exception):
    pass


class CaptchaRecognitionFailed(UpstreamError):
    pass


class UnknownResult(UpstreamError):
    pass


class JeepayAdapter:
    def __init__(self):
        url = settings.SETTLEMENT_UPSTREAM_URL.rstrip('/')
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname != 'agent.jeepay.vip' or parsed.path or parsed.query:
            raise UpstreamError('控制服务未配置')
        if not all((settings.SETTLEMENT_USERNAME, settings.SETTLEMENT_PASSWORD, settings.SETTLEMENT_MERCHANT, settings.SETTLEMENT_TESSERACT)):
            raise UpstreamError('控制服务未配置')
        self.url = url
        self.http = httpx.Client(base_url=url, timeout=15, follow_redirects=False)
        self._session()

    def _session(self):
        from .models import UpstreamSession

        if connection.in_atomic_block:
            raise UpstreamError('登录不能在未提交的事务内发起')
        with transaction.atomic(durable=True):
            if connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_xact_lock(hashtextextended('settlement-login:' || current_schema(), 0))")
                    if not cursor.fetchone()[0]:
                        raise UpstreamError('上游登录正在处理')
            row, _ = UpstreamSession.objects.get_or_create(key='agent')
            row = UpstreamSession.objects.select_for_update(nowait=True).get(key='agent')
            now = timezone.now()
            cipher = Fernet(settings.SETTLEMENT_FERNET_KEY.encode())
            if row.login_attempt:
                raise UpstreamError('上游登录结果待核对')
            if row.token and row.token_created_at and row.token_created_at > now - timedelta(minutes=10):
                try:
                    self.http.headers['iToken'] = cipher.decrypt(bytes(row.token)).decode()
                    self.http.headers['oemsitehost'] = 'agent.jeepay.vip'
                    return
                except (InvalidToken, UnicodeError):
                    raise UpstreamError('登录会话不可用') from None
            if row.attempted_at and row.attempted_at > now - timedelta(seconds=3):
                raise UpstreamError('上游登录冷却中')
            attempt = uuid.uuid4()
            row.login_attempt = attempt
            row.attempted_at = now
            row.save(update_fields=['attempted_at', 'login_attempt'])

        error = None
        try:
            token = self._login_once()
            values = {'token': cipher.encrypt(token.encode()), 'token_created_at': timezone.now()}
        except Exception as exc:
            error = exc
            values = {}
        values['attempted_at'] = timezone.now()
        if error is None or (isinstance(error, UpstreamError) and not isinstance(error, UnknownResult)):
            values['login_attempt'] = None
        with transaction.atomic(durable=True):
            changed = UpstreamSession.objects.filter(key='agent', login_attempt=attempt).update(**values)
        if changed != 1:
            raise UpstreamError('上游登录状态已改变，请联系管理员')
        if error is not None:
            if isinstance(error, UpstreamError):
                raise error
            raise UpstreamError('上游登录暂不可用') from None
        self.http.headers['iToken'] = token
        self.http.headers['oemsitehost'] = 'agent.jeepay.vip'

    def _login_once(self):
        b64 = lambda x: base64.b64encode(x.encode()).decode()
        try:
            result = self._call('GET', '/api/anon/auth/vercode', timeout=8)
        except UnknownResult:
            raise UpstreamError('验证码获取失败，请稍后重试') from None
        try:
            image = base64.b64decode(result['imageBase64Data'].split(',')[-1], validate=True)
            if len(image) > 200000:
                raise ValueError
            with Image.open(BytesIO(image)) as source:
                if source.width * source.height > 1000000:
                    raise ValueError
                rgb = source.convert('RGB')
                counts = Counter(rgb.getdata())
                code = ''
                for threshold, psm in ((20, '8'), (30, '7')):
                    pixels = [0 if counts[c] >= threshold and c != (255, 255, 255) else 255 for c in rgb.getdata()]
                    bitmap = Image.new('L', rgb.size)
                    bitmap.putdata(pixels)
                    bitmap = bitmap.resize((rgb.width * 4, rgb.height * 4), Image.Resampling.NEAREST)
                    buffer = BytesIO()
                    bitmap.save(buffer, format='PNG')
                    proc = subprocess.run([settings.SETTLEMENT_TESSERACT, 'stdin', 'stdout', '--psm', psm,
                                           '-c', 'tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'],
                                          input=buffer.getvalue(), capture_output=True, timeout=2, check=True)
                    code = proc.stdout.decode('utf-8').strip()
                    if re.fullmatch('[A-Za-z0-9]{4}', code):
                        break
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, KeyError, UnidentifiedImageError):
            raise UpstreamError('验证码识别不可用') from None
        if not re.fullmatch('[A-Za-z0-9]{4}', code):
            raise CaptchaRecognitionFailed('验证码识别失败，请稍后重试')
        payload = {'ia': b64(settings.SETTLEMENT_USERNAME), 'ip': b64(settings.SETTLEMENT_PASSWORD), 'vc': b64(code), 'vt': b64(result['vercodeToken']), 'mc': '', 'sc': '', 'rd': '', 'snd': '', 'lt': b64('WEB')}
        auth = self._call('POST', '/api/anon/auth/validate', json=payload, timeout=8)
        if not isinstance(auth, dict) or not auth.get('iToken'):
            raise UpstreamError('上游登录未完成')
        return auth['iToken']

    def _call(self, method, path, **kwargs):
        guard = _private_upstream_call.set(True)
        try:
            response = self.http.request(method, path, **kwargs)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            raise UnknownResult('上游请求结果待核对') from None
        finally:
            _private_upstream_call.reset(guard)
        if (method == 'POST' and path == '/api/anon/auth/validate' and isinstance(body, dict)
                and body.get('code') == 9999 and body.get('msg') == '验证码有误！'):
            raise CaptchaRecognitionFailed('验证码识别失败，请稍后重试')
        if not isinstance(body, dict) or body.get('code') != 0:
            raise UpstreamError('上游操作未成功')
        return body.get('data') or {}

    def create_app(self, name, secret):
        data = {'mchNo': settings.SETTLEMENT_MERCHANT, 'appName': name, 'appSecret': secret, 'appSignType': '["MD5"]', 'appSignTypeObject': ['MD5'], 'state': 1, 'defaultFlag': 0}
        result = self._call('POST', '/api/mchApps', json=data)
        app_id = result.get('appId') if isinstance(result, dict) else None
        if not app_id:
            raise UnknownResult('应用创建结果待核对')
        return app_id

    def authorize(self, app_id, account, real_name):
        data = self._call('GET', f'/api/mch/payConfigs/alipayTransAuthorApply/{app_id}/alitransympay', params={'channel': 'tinyapp', 'aliAccountNo': account, 'aliAccountName': real_name})
        return self._authorization_url(data.get('authorizeLink', ''))

    def identity(self, app_id):
        data = self._call('GET', f'/api/mch/payConfigs/alitransymOpenIdAuthUrl/{app_id}/alitransympay')
        return self._authorization_url(data.get('authorizeUrl', ''))

    def identity_refresh(self, app_id, account, real_name):
        signed = self._call('GET', f'/api/mch/payConfigs/alipayTransAuthorQuery/{app_id}/alitransympay')
        if signed.get('status') != 'NORMAL' or not signed.get('agreementNo'):
            raise UpstreamError('签约状态待核对')
        result = self._call('GET', f'/api/mch/payConfigs/alitransymOpenIdQuery/{app_id}/alitransympay')
        field = 'aliOpenId' if result.get('aliOpenId') else 'aliPid'
        identity = result.get(field)
        if not identity:
            return False
        if not isinstance(identity, str) or len(identity) > 128 or not re.fullmatch(r'[A-Za-z0-9_-]+', identity):
            raise UpstreamError('付款身份格式异常')
        if field == 'aliPid' and not re.fullmatch(r'2088[0-9]{8,30}', identity):
            raise UpstreamError('付款身份格式异常')
        query = {'infoId': app_id, 'ifCode': 'alitransympay', 'configMode': 'agentMch'}
        config = self._call('GET', '/api/payConfig/interfaceSavedConfigs', params=query)
        try:
            params = json.loads(config.get('ifParams') or '{}')
        except (TypeError, ValueError):
            raise UpstreamError('授权配置读取失败') from None
        if not isinstance(params, dict) or config.get('state') != 1 or any(
            params.get(k) != v for k, v in (('aliAccountNo', account), ('aliAccountName', real_name), ('agreementNo', signed['agreementNo']))
        ) or any(params.get(k) and params.get(k) != result.get(k) for k in ('aliOpenId', 'aliPid')):
            raise UpstreamError('付款身份信息待核对')
        if params.get(field) != identity:
            params[field] = identity
            payload = dict(config)
            payload.update(query)
            payload['ifParams'] = json.dumps(params, ensure_ascii=False, separators=(',', ':'))
            self._call('POST', '/api/payConfig/interfaceParams', json=payload)
        config = self._call('GET', '/api/payConfig/interfaceSavedConfigs', params=query)
        try:
            params = json.loads(config.get('ifParams') or '{}')
        except (TypeError, ValueError):
            raise UnknownResult('付款身份保存结果待核对') from None
        if not isinstance(params, dict) or config.get('state') != 1 or any(
            params.get(k) != v for k, v in ((field, identity), ('aliAccountNo', account), ('aliAccountName', real_name), ('agreementNo', signed['agreementNo']))
        ):
            raise UnknownResult('付款身份保存结果待核对')
        return True

    def revoke(self, app_id):
        result = self._call('POST', f'/api/mch/payConfigs/alipayTransAuthorCancel/{app_id}/alitransympay')
        if not isinstance(result, dict) or result.get('status') not in (None, '', 'UPSTREAM_NOT_EXIST'):
            raise UnknownResult('解除授权结果待核对')
        query = {'infoId': app_id, 'ifCode': 'alitransympay', 'configMode': 'agentMch'}
        config = self._call('GET', '/api/payConfig/interfaceSavedConfigs', params=query)
        try:
            params = json.loads(config.get('ifParams') or '{}')
            if not isinstance(params, dict):
                raise ValueError
        except (TypeError, ValueError):
            raise UnknownResult('解除授权配置待核对') from None
        for field in ('agreementNo', 'aliOpenId', 'aliPid'):
            params[field] = ''
        payload = dict(config)
        payload.update(query)
        payload['ifParams'] = json.dumps(params, ensure_ascii=False, separators=(',', ':'))
        self._call('POST', '/api/payConfig/interfaceParams', json=payload)
        config = self._call('GET', '/api/payConfig/interfaceSavedConfigs', params=query)
        try:
            params = json.loads(config.get('ifParams') or '{}')
            if not isinstance(params, dict) or any(params.get(field) for field in ('agreementNo', 'aliOpenId', 'aliPid')):
                raise ValueError
        except (TypeError, ValueError):
            raise UnknownResult('解除授权配置待核对') from None
        return True

    @staticmethod
    def _authorization_url(url):
        if not isinstance(url, str):
            raise UnknownResult('授权结果待核对')
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in {'ur.alipay.com', 'b.alipay.com', 'qr.alipay.com', 'open.alipay.com', 'openauth.alipay.com'} or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise UnknownResult('授权结果待核对')
        return url

    def refresh(self, app_id, account, real_name, next_action):
        result = self._call('GET', f'/api/mch/payConfigs/alipayTransAuthorQuery/{app_id}/alitransympay')
        if result.get('status') != 'NORMAL' or not result.get('agreementNo'):
            return {'configured': False, 'next_action': 'agreement'}
        query = {'infoId': app_id, 'ifCode': 'alitransympay', 'configMode': 'agentMch'}
        config = self._call('GET', '/api/payConfig/interfaceSavedConfigs', params=query)
        try:
            params = json.loads(config.get('ifParams') or '{}')
        except (TypeError, ValueError):
            raise UpstreamError('授权配置读取失败') from None
        if not isinstance(params, dict):
            raise UpstreamError('授权配置读取失败')
        pid = params.get('aliPid')
        open_id = params.get('aliOpenId')
        for field, expected in (('aliAccountNo', account), ('aliAccountName', real_name), ('agreementNo', result['agreementNo'])):
            if params.get(field) not in (None, '', expected):
                return {'configured': False, 'next_action': 'identity', 'review_code': 'IDENTITY_UNVERIFIED'}
        if open_id and not pid and any(params.get(k) != v for k, v in (('aliAccountNo', account), ('aliAccountName', real_name), ('agreementNo', result['agreementNo']))):
            return {'configured': False, 'next_action': 'identity', 'review_code': 'IDENTITY_INCOMPLETE'}
        if pid in (None, '') and open_id in (None, ''):
            matches = (config.get('state') == 1 and params.get('agreementNo') == result['agreementNo']
                       and params.get('aliAccountNo') == account and params.get('aliAccountName') == real_name)
            if matches:
                return {'configured': False, 'next_action': 'agreement', 'signed_without_pid': True}
            return {'configured': False, 'next_action': 'identity', 'review_code': 'IDENTITY_INCOMPLETE'}
        if pid not in (None, '') and (not isinstance(pid, str) or not re.fullmatch(r'2088[0-9]{8,30}', pid)):
            return {'configured': False, 'next_action': 'identity', 'review_code': 'IDENTITY_UNVERIFIED'}
        if open_id not in (None, '') and (not isinstance(open_id, str) or len(open_id) > 128 or not re.fullmatch(r'[A-Za-z0-9_-]+', open_id)):
            return {'configured': False, 'next_action': 'identity', 'review_code': 'IDENTITY_UNVERIFIED'}
        if params.get('agreementNo') != result['agreementNo'] or params.get('aliAccountNo') != account or params.get('aliAccountName') != real_name or config.get('state') != 1:
            params.update({'agreementNo': result['agreementNo'], 'aliAccountNo': account, 'aliAccountName': real_name})
            payload = dict(config)
            payload.update(query)
            payload['state'] = 1
            payload['ifParams'] = json.dumps(params, ensure_ascii=False, separators=(',', ':'))
            self._call('POST', '/api/payConfig/interfaceParams', json=payload)
            config = self._call('GET', '/api/payConfig/interfaceSavedConfigs', params=query)
            try:
                params = json.loads(config.get('ifParams') or '{}')
            except (TypeError, ValueError):
                raise UpstreamError('授权配置读取失败') from None
        ready = bool(isinstance(params, dict) and config.get('state') == 1 and params.get('agreementNo') == result['agreementNo'] and params.get('aliAccountNo') == account and params.get('aliAccountName') == real_name and params.get('aliPid') == pid and params.get('aliOpenId') == open_id)
        return {'configured': ready, 'next_action': 'agreement' if ready else 'identity'}
