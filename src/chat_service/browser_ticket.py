import base64
import hmac
import json
import re
import time
from urllib.parse import urlsplit

from .protocol import ProtocolError


TICKET_LIFETIME = 120
_TICKET_DOMAIN = b'tuantuantong-chat-browser-v1\0'


def _validate_origin(value):
    if not isinstance(value, str) or not re.fullmatch(r'https://[a-z0-9.-]+(?::[0-9]{1,5})?', value):
        raise ValueError('Browser chat origins must use canonical HTTPS')
    parsed = urlsplit(value)
    if (not parsed.hostname or len(parsed.hostname) > 253 or
            any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', name)
                for name in parsed.hostname.split('.')) or parsed.port in (0, 443)):
        raise ValueError('Invalid browser chat origin')
    return value


def validate_browser(value):
    if (not isinstance(value, dict) or set(value) != {'audience', 'origins'} or
            not isinstance(value['origins'], list) or not 1 <= len(value['origins']) <= 20):
        raise ValueError('Browser chat requires an audience and a bounded list of tenant origins')
    audience = _validate_origin(value['audience'])
    origins = [_validate_origin(origin) for origin in value['origins']]
    if len(origins) != len(set(origins)) or audience in origins:
        raise ValueError('Browser chat origins must be distinct from the audience')
    return value


def _signature(secret, part):
    return hmac.digest(secret.encode(), _TICKET_DOMAIN + part.encode('ascii'), 'sha256').hex()


def issue_browser_ticket(*, key_id, secret, viewer, origin, audience, proof, now=None):
    issued = int(time.time() if now is None else now)
    expires = issued + TICKET_LIFETIME
    payload = {'key': key_id, 'viewer': viewer, 'origin': origin, 'aud': audience,
               'iat': issued, 'exp': expires, 'proof': proof}
    part = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode('ascii').rstrip('=')
    return f'{part}.{_signature(secret, part)}', expires


def verify_browser_ticket(token, tenants, *, origin, host, now=None):
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError
        part, signature = token.split('.')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', part) or not re.fullmatch(r'[a-f0-9]{64}', signature):
            raise ValueError
        fields = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        if not isinstance(fields, dict) or set(fields) != {'key', 'viewer', 'origin', 'aud', 'iat', 'exp', 'proof'}:
            raise ValueError
        tenant = tenants[fields['key']]
        browser = tenant['browser']
        if not hmac.compare_digest(signature, _signature(tenant['secret'], part)):
            raise ValueError
        clock = int(time.time() if now is None else now)
        if (fields['origin'] != origin or origin not in browser['origins'] or fields['aud'] != browser['audience']
                or host != urlsplit(browser['audience']).netloc
                or type(fields['viewer']) is not int or not 1 <= fields['viewer'] < 1_000_000_000
                or type(fields['iat']) is not int or type(fields['exp']) is not int
                or not fields['iat'] <= clock < fields['exp'] <= fields['iat'] + TICKET_LIFETIME
                or not isinstance(fields['proof'], str) or not 1 <= len(fields['proof']) <= 2048):
            raise ValueError
        return fields
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ProtocolError('聊天凭证无效或已过期') from None
