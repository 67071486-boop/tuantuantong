import hashlib
import hmac
import re
import time


class ProtocolError(ValueError):
    pass


def _canonical(key_id, method, path, timestamp, body):
    return '\n'.join((key_id, method.upper(), path, timestamp, hashlib.sha256(body).hexdigest())).encode()


def sign_request(secret, key_id, method, path, body, now=None):
    timestamp = str(int(time.time() if now is None else now))
    signature = hmac.new(secret.encode(), _canonical(key_id, method, path, timestamp, body), hashlib.sha256).hexdigest()
    return {'X-TTT-Chat-Key': key_id, 'X-TTT-Chat-Time': timestamp, 'X-TTT-Chat-Signature': signature}


def verify_request(keys, headers, method, path, body, now=None):
    headers = {key.lower(): value for key, value in headers.items()}
    if 'cookie' in headers:
        raise ProtocolError('Cookies are not accepted')
    key_id = headers.get('x-ttt-chat-key', '')
    timestamp = headers.get('x-ttt-chat-time', '')
    try:
        valid_time = abs(int(time.time() if now is None else now) - int(timestamp)) <= 60
    except (ValueError, TypeError):
        valid_time = False
    if key_id not in keys or not valid_time:
        raise ProtocolError('Invalid signing key or expired request')
    signature = headers.get('x-ttt-chat-signature', '')
    if not isinstance(signature, str) or re.fullmatch(r'[0-9a-f]{64}', signature) is None:
        raise ProtocolError('Invalid request signature')
    expected = hmac.new(keys[key_id].encode(), _canonical(key_id, method, path, timestamp, body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ProtocolError('Invalid request signature')
    return key_id
