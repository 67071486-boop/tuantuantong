import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from datetime import timedelta
from functools import wraps

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import JsonResponse
from django.utils import timezone
from django.utils.module_loading import import_string
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .locking import LockUnavailable, operation_lock
from .upstream import CaptchaRecognitionFailed, UpstreamError
from .health import business_probe, post_business_probe


def _error(message, status=400):
    return JsonResponse({'error': message}, status=status, headers={'Cache-Control': 'no-store'})


def _unknown():
    return JsonResponse({'code': 'OUTCOME_UNKNOWN', 'error': '操作结果待核对，请勿重复创建'}, status=409, headers={'Cache-Control': 'no-store'})


def _identity_review(row):
    if row.review_code not in {'IDENTITY_INCOMPLETE', 'IDENTITY_UNVERIFIED'}:
        return _error('身份核对状态异常', 503)
    return JsonResponse({'code': row.review_code, 'error': '付款方身份需要平台核对'}, status=409, headers={'Cache-Control': 'no-store'})


def _audit(action):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            from .models import SecurityEvent

            response = view(request, *args, **kwargs)
            client = getattr(request, 'settlement_client', None)
            if client and response.status_code != 429:
                SecurityEvent.objects.create(client=client, action=action, result=response.status_code)
            return response
        return wrapped
    return decorate


def _authenticate(request):
    from .models import Nonce, RateWindow, SecurityEvent

    client = request.headers.get('X-Settlement-Client', '')
    timestamp = request.headers.get('X-Settlement-Timestamp', '')
    nonce = request.headers.get('X-Settlement-Nonce', '')
    signature = request.headers.get('X-Settlement-Signature', '')
    keys = settings.SETTLEMENT_CLIENT_KEYS
    if not isinstance(keys, dict) or any(not isinstance(value, str) for value in keys.values()):
        return None
    if len(keys) != len(set(keys.values())):
        return None
    key = keys.get(client)
    if not isinstance(key, str) or len(key) < 32 or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', client):
        return None
    if len(timestamp) != 10 or not timestamp.isdigit() or abs(time.time() - int(timestamp)) > 300 or not re.fullmatch(r'[A-Za-z0-9_-]{32,64}', nonce):
        return None
    digest = hashlib.sha256(request.body).hexdigest()
    message = '\n'.join((request.method, request.path, timestamp, nonce, digest))
    expected = hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    request.settlement_client = client
    minute = int(time.time()) // 60
    RateWindow.objects.get_or_create(client=client, minute=minute)
    changed = RateWindow.objects.filter(client=client, minute=minute, count__lt=120).update(count=F('count') + 1)
    if not changed:
        logged = RateWindow.objects.filter(client=client, minute=minute, rejected_logged=False).update(rejected_logged=True)
        if logged:
            SecurityEvent.objects.create(client=client, action='rate_limit', result=429)
        return False
    try:
        with transaction.atomic():
            Nonce.objects.create(client=client, nonce=nonce)
    except IntegrityError:
        return None
    Nonce.objects.filter(client=client, created_at__lt=timezone.now() - timedelta(minutes=10)).delete()
    RateWindow.objects.filter(client=client, minute__lt=minute - 2).delete()
    return client


def _response(row, url=None):
    data = {'status': row.status, 'configured': row.configured}
    if row.status in {'pending', 'signed'}:
        data['next_action'] = row.next_action
    if url:
        data['authorization_url'] = url
    return JsonResponse(data, headers={'Cache-Control': 'no-store'})


def _fernet():
    return Fernet(settings.SETTLEMENT_FERNET_KEY.encode())


def _decrypt(row):
    return json.loads(_fernet().decrypt(bytes(row.ciphertext)))


def _adapter():
    return import_string(settings.SETTLEMENT_ADAPTER)()


def _start(row, allowed, working):
    from .models import Authorization

    with transaction.atomic():
        locked = Authorization.objects.select_for_update().get(pk=row.pk)
        if locked.phase not in allowed:
            return None
        previous_phase = locked.phase
        locked.phase = working
        locked.save(update_fields=['phase', 'updated_at'])
        locked._previous_phase = previous_phase
    return locked


@csrf_exempt
@_audit('create')
@require_POST
def create(request):
    from .models import Authorization

    probe = post_business_probe(request, 'create')
    if probe is not None:
        return probe
    if len(request.body) > 4096:
        return _error('请求过大', 413)
    client = _authenticate(request)
    if client is False:
        return _error('请求过于频繁', 429)
    if not client:
        return _error('认证失败', 401)
    try:
        body = json.loads(request.body)
        request_id = uuid.UUID(body['request_id'])
        account, real_name = body['account'], body['real_name']
        if not isinstance(account, str) or not isinstance(real_name, str) or not 1 <= len(account) <= 128 or not 1 <= len(real_name) <= 64:
            raise ValueError
        if set(body) != {'request_id', 'account', 'real_name'}:
            raise ValueError
        payload = json.dumps({'account': account, 'real_name': real_name}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        digest = hmac.new(settings.SETTLEMENT_FERNET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        encrypted = _fernet().encrypt(json.dumps({'account': account, 'real_name': real_name, 'secret': secrets.token_urlsafe(48)}, ensure_ascii=False).encode())
    except (KeyError, ValueError, TypeError):
        return _error('请求参数或服务配置错误')
    name = settings.SETTLEMENT_CLIENT_NAMES.get(client)
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        return _error('系统名称未配置', 503)
    row, _ = Authorization.objects.get_or_create(client=client, request_id=request_id, defaults={'payload_hash': digest, 'ciphertext': encrypted, 'app_name': name.strip() + '-' + request_id.hex[:12]})
    if not hmac.compare_digest(row.payload_hash, digest):
        return _error('请求编号已被其他内容使用', 409)
    if row.phase == 'new':
        try:
            with operation_lock():
                try:
                    adapter = _adapter()
                except CaptchaRecognitionFailed:
                    return JsonResponse({'code': 'CAPTCHA_FAILED'}, status=503, headers={'Cache-Control': 'no-store'})
                except (UpstreamError, InvalidToken, ValueError, TypeError, KeyError):
                    return _error('上游暂不可用，可使用原请求重试', 503)
                active = _start(row, {'new'}, 'creating')
                if active:
                    try:
                        data = _decrypt(active)
                        app_id = adapter.create_app(active.app_name, data['secret'])
                        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', app_id):
                            raise ValueError
                        active.app_id, active.phase = app_id, 'app_created'
                        active.save(update_fields=['app_id', 'phase', 'updated_at'])
                    except (UpstreamError, InvalidToken, ValueError, TypeError, KeyError):
                        active.phase, active.status = 'unknown', 'failed'
                        active.unknown_action = 'create_app'
                        active.save(update_fields=['phase', 'status', 'unknown_action', 'updated_at'])
                        return _unknown()
        except LockUnavailable:
            return _error('管理维护中，请使用原请求稍后重试', 503)
    row.refresh_from_db()
    if row.phase == 'identity_review':
        return _identity_review(row)
    if row.phase == 'app_created':
        return _authorize(row)
    if row.phase in {'unknown', 'creating', 'authorizing', 'refreshing', 'identity_authorizing', 'identity_refreshing', 'revoking'}:
        return _unknown()
    return _response(row)


@csrf_exempt
def authorizations(request):
    if request.method == 'GET':
        return business_probe(request)
    return create(request)


def _authorize(row):
    from .models import Authorization

    if row.phase == 'identity_review':
        return _identity_review(row)
    if row.phase in {'unknown', 'creating', 'authorizing', 'refreshing', 'identity_authorizing', 'identity_refreshing', 'revoking'}:
        return _unknown()
    if row.next_action != 'agreement':
        Authorization.objects.filter(pk=row.pk, phase__in=['app_created', 'pending']).update(phase='identity_review', status='pending', configured=False, review_code='IDENTITY_INCOMPLETE')
        row.refresh_from_db()
        return _identity_review(row)
    try:
        with operation_lock():
            try:
                adapter = _adapter()
            except CaptchaRecognitionFailed:
                return JsonResponse({'code': 'CAPTCHA_FAILED'}, status=503, headers={'Cache-Control': 'no-store'})
            except (UpstreamError, InvalidToken, ValueError, TypeError, KeyError):
                return _error('上游暂不可用，可使用原请求重试', 503)
            active = _start(row, {'app_created', 'pending', 'revoked'}, 'authorizing')
            if not active:
                return _error('操作正在处理或需要核对', 409)
            try:
                data = _decrypt(active)
                link = adapter.authorize(active.app_id, data['account'], data['real_name'])
            except (UpstreamError, InvalidToken, ValueError, KeyError):
                active.phase, active.status = 'unknown', 'failed'
                active.unknown_action = 'identity_authorize' if active.next_action == 'identity' else 'agreement_authorize'
                active.save(update_fields=['phase', 'status', 'unknown_action', 'updated_at'])
                return _unknown()
            active.phase, active.status = 'pending', 'pending'
            active.save(update_fields=['phase', 'status', 'updated_at'])
            return _response(active, link)
    except LockUnavailable:
        return _error('管理维护中，请使用原请求稍后重试', 503)


def _find(request, request_id):
    from .models import Authorization

    client = _authenticate(request)
    if client is False:
        return None, _error('请求过于频繁', 429)
    if not client:
        return None, _error('认证失败', 401)
    row = Authorization.objects.filter(client=client, request_id=request_id).first()
    if not row:
        return None, _error('记录不存在', 404)
    return row, None


@csrf_exempt
@_audit('authorize')
@require_POST
def authorize(request, request_id):
    probe = post_business_probe(request, 'authorize')
    if probe is not None:
        return probe
    row, error = _find(request, request_id)
    if error:
        return error
    if row.phase in {'unknown', 'creating', 'authorizing', 'refreshing', 'identity_authorizing', 'identity_refreshing', 'revoking'}:
        return _unknown()
    if row.status in {'authorized', 'signed'}:
        return _response(row)
    return _authorize(row)


@csrf_exempt
@_audit('refresh')
@require_POST
def refresh(request, request_id):
    from .models import Authorization

    probe = post_business_probe(request, 'refresh')
    if probe is not None:
        return probe
    row, error = _find(request, request_id)
    if error:
        return error
    if row.phase in {'unknown', 'creating', 'authorizing', 'refreshing', 'identity_authorizing', 'identity_refreshing', 'revoking'}:
        return _unknown()
    if row.phase not in {'pending', 'authorized', 'signed', 'identity_review'}:
        return _error('操作尚未完成或需要核对', 409)
    try:
        with operation_lock():
            data = _decrypt(row)
            adapter = _adapter()
            active = _start(row, {'pending', 'authorized', 'signed', 'identity_review'}, 'refreshing')
            if not active:
                return _unknown()
            previous_phase = active._previous_phase
            try:
                result = adapter.refresh(active.app_id, data['account'], data['real_name'], active.next_action)
                if not isinstance(result, dict) or result.get('next_action') not in {'agreement', 'identity'} or not isinstance(result.get('configured'), bool):
                    raise ValueError
                if result.get('review_code') not in (None, 'IDENTITY_INCOMPLETE', 'IDENTITY_UNVERIFIED'):
                    raise ValueError
            except (UpstreamError, InvalidToken, ValueError, KeyError, TypeError):
                Authorization.objects.filter(pk=active.pk, phase='refreshing').update(phase='unknown', unknown_action='refresh')
                return _unknown()
            return _finish_refresh(row, result, previous_phase)
    except LockUnavailable:
        return _error('管理维护中，请稍后重试', 503)
    except CaptchaRecognitionFailed:
        return JsonResponse({'code': 'CAPTCHA_FAILED'}, status=503, headers={'Cache-Control': 'no-store'})
    except (UpstreamError, InvalidToken, ValueError, KeyError, TypeError):
        return _error('查询失败，请稍后重试', 503)


def _finish_refresh(row, result, previous_phase):
    from .models import Authorization

    review_code = result.get('review_code')
    if review_code in {'IDENTITY_INCOMPLETE', 'IDENTITY_UNVERIFIED'} and result['configured'] is False:
        Authorization.objects.filter(pk=row.pk, phase='refreshing').update(phase='identity_review', status='pending', configured=False, review_code=review_code)
        row.refresh_from_db()
        return _identity_review(row)
    if review_code is not None:
        Authorization.objects.filter(pk=row.pk, phase='refreshing').update(phase='unknown', unknown_action='refresh')
        return _unknown()
    if result.get('signed_without_pid') is True and result['configured'] is False:
        row.refresh_from_db()
        if previous_phase == 'identity_review' and row.review_code != 'IDENTITY_INCOMPLETE':
            Authorization.objects.filter(pk=row.pk, phase='refreshing').update(phase='identity_review')
            row.refresh_from_db()
            return _identity_review(row)
        Authorization.objects.filter(pk=row.pk, phase='refreshing').update(status='signed', phase='signed', configured=False, next_action='identity', review_code='')
        row.refresh_from_db()
        return _response(row)
    configured = result['configured']
    if not configured and previous_phase in {'authorized', 'signed'}:
        Authorization.objects.filter(pk=row.pk, phase='refreshing').update(phase=previous_phase)
        return _error('授权状态暂无法确认', 503)
    if not configured and previous_phase == 'identity_review':
        Authorization.objects.filter(pk=row.pk, phase='refreshing').update(phase='identity_review')
        row.refresh_from_db()
        return _identity_review(row)
    if configured:
        Authorization.objects.filter(pk=row.pk, phase='refreshing').update(status='authorized', phase='authorized', configured=True, review_code='')
        row.refresh_from_db()
    else:
        Authorization.objects.filter(pk=row.pk, phase='refreshing').update(phase='pending', next_action=result['next_action'])
        row.refresh_from_db()
    return _response(row)


def _identity_operation(request, request_id, operation):
    row, error = _find(request, request_id)
    if error:
        return error
    if row.phase in {'unknown', 'creating', 'authorizing', 'refreshing', 'identity_authorizing', 'identity_refreshing', 'revoking'}:
        return _unknown()
    if operation == 'revoke' and row.phase == 'revoked':
        return _response(row)
    allowed = {'signed'} if operation != 'revoke' else {'signed', 'authorized'}
    if row.phase not in allowed:
        return _error('当前状态不能执行此操作', 409)
    phase = {'identity': 'identity_authorizing', 'identity_refresh': 'identity_refreshing', 'revoke': 'revoking'}[operation]
    try:
        with operation_lock():
            try:
                adapter = _adapter()
                data = _decrypt(row)
            except CaptchaRecognitionFailed:
                return JsonResponse({'code': 'CAPTCHA_FAILED'}, status=503, headers={'Cache-Control': 'no-store'})
            except (UpstreamError, InvalidToken, ValueError, KeyError, TypeError):
                return _error('上游暂不可用，请稍后重试', 503)
            active = _start(row, allowed, phase)
            if not active:
                return _unknown()
            try:
                if operation == 'identity':
                    link = adapter.identity(active.app_id)
                    active.phase = 'signed'
                    active.save(update_fields=['phase', 'updated_at'])
                    return _response(active, link)
                if operation == 'identity_refresh':
                    ready = adapter.identity_refresh(active.app_id, data['account'], data['real_name'])
                    if not isinstance(ready, bool):
                        raise ValueError
                    active.phase = active.status = 'authorized' if ready else 'signed'
                    active.next_action = '' if ready else 'identity'
                    active.configured = ready
                else:
                    if adapter.revoke(active.app_id) is not True:
                        raise ValueError
                    active.phase = active.status = 'revoked'
                    active.next_action, active.configured = 'agreement', False
                active.save(update_fields=['phase', 'status', 'next_action', 'configured', 'updated_at'])
                return _response(active)
            except (UpstreamError, InvalidToken, ValueError, KeyError, TypeError):
                active.phase, active.unknown_action = 'unknown', operation
                active.configured = False
                active.save(update_fields=['phase', 'unknown_action', 'configured', 'updated_at'])
                return _unknown()
    except LockUnavailable:
        return _error('管理维护中，请稍后重试', 503)


@csrf_exempt
@_audit('identity')
@require_POST
def identity(request, request_id):
    return _identity_operation(request, request_id, 'identity')


@csrf_exempt
@_audit('identity_refresh')
@require_POST
def identity_refresh(request, request_id):
    return _identity_operation(request, request_id, 'identity_refresh')


@csrf_exempt
@_audit('revoke')
@require_POST
def revoke(request, request_id):
    try:
        body = json.loads(request.body)
        if not isinstance(body, dict) or set(body) != {'request_id', 'confirm'} or body['request_id'] != str(request_id) or body['confirm'] is not True:
            raise ValueError
    except (TypeError, ValueError):
        return _error('请确认取消授权')
    return _identity_operation(request, request_id, 'revoke')
