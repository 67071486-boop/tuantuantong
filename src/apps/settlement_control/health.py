import os
import re
import secrets
from threading import Lock

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

_instance_lock = Lock()
_instance_pid = None
_instance_value = None


def _reset_after_fork():
    global _instance_lock, _instance_pid, _instance_value
    _instance_lock = Lock()
    _instance_pid = None
    _instance_value = None


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _instance():
    global _instance_pid, _instance_value
    pid = os.getpid()
    with _instance_lock:
        if _instance_pid != pid:
            _instance_pid = pid
            _instance_value = secrets.token_hex(16)
        return _instance_value


def _ready(challenge=None, *, route=None, method=None):
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            if cursor.fetchone() != (1,):
                raise RuntimeError
    except Exception:
        return JsonResponse({'ok': False}, status=503, headers={'Cache-Control': 'no-store'})
    result = {'ok': True, 'service': 'settlement-control', 'instance': _instance()}
    if challenge is not None:
        result['challenge'] = challenge
    if route:
        result['route'] = route
    if method:
        result['method'] = method
    return JsonResponse(result, headers={'Cache-Control': 'no-store'})


def _probe(request, *, business=False):
    challenges = request.GET.getlist('challenge')
    if (business and (len(challenges) != 1 or set(request.GET) != {'challenge'})) or len(challenges) > 1 or challenges and not re.fullmatch(r'[0-9a-f]{32}', challenges[0]):
        return JsonResponse({'ok': False}, status=400, headers={'Cache-Control': 'no-store'})
    return _ready(challenges[0] if challenges else None, route='authorizations' if business else None)


def post_business_probe(request, route):
    allowed_headers = {'host', 'content-type', 'content-length', 'accept', 'accept-encoding', 'user-agent', 'connection', 'cookie', 'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-port', 'x-forwarded-proto', 'x-real-ip', 'x-settlement-probe'}
    if (request.method != 'POST' or request.headers.get('X-Settlement-Probe') != '1' or request.GET
            or request.headers.get('Cookie', '') or request.content_type != 'application/json'
            or set(key.lower() for key in request.headers) - allowed_headers):
        return None
    match = re.fullmatch(rb'\{"challenge":"([0-9a-f]{32})"\}', request.body)
    if not match:
        return None
    return _ready(match.group(1).decode('ascii'), route=route, method='POST')


@require_GET
def health(request):
    return _probe(request)


@require_GET
def business_probe(request):
    return _probe(request, business=True)
