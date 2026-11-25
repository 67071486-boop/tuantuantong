import hashlib
import hmac
import re
from urllib.parse import quote

import httpx

from .config import ServiceError


def quoted(value):
    return quote(str(value), safe='')


class Matrix:
    def __init__(self, url, registration_secret, client=None):
        self.url, self.secret = url.rstrip('/'), registration_secret
        self.client = client or httpx.Client(timeout=30, trust_env=False)

    def call(self, method, path, token=None, body=None, raw=None, mime=None, download=False):
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        if mime:
            headers['Content-Type'] = mime
        try:
            response = self.client.request(method, self.url + path, headers=headers, json=body, content=raw)
        except httpx.HTTPError:
            raise ServiceError('Matrix is temporarily unavailable', 503) from None
        if download and response.is_success:
            return response.content, response.headers.get('content-type', 'application/octet-stream')
        try:
            result = response.json()
        except ValueError:
            raise ServiceError('Matrix returned invalid JSON', 503) from None
        code = result.get('errcode') if isinstance(result, dict) else None
        safe_code = code if isinstance(code, str) and re.fullmatch(r'M_[A-Z0-9_]{1,80}', code) else None
        delay_ms = result.get('retry_after_ms') if isinstance(result, dict) else None
        retry_after = (min(delay_ms, 3_600_000) / 1000 if isinstance(delay_ms, int)
                       and not isinstance(delay_ms, bool) and delay_ms >= 0 else None)
        if not response.is_success:
            raise ServiceError('Matrix rejected request: ' + str(response.status_code), 503, safe_code, retry_after)
        if not isinstance(result, dict) or 'errcode' in result:
            raise ServiceError('Matrix returned an error envelope', 503, safe_code, retry_after)
        return result

    def account(self, tenant):
        username = 'tt_bridge_' + hashlib.sha256(tenant.encode()).hexdigest()[:40]
        password = hmac.new(self.secret.encode(), ('account:' + tenant).encode(), hashlib.sha256).hexdigest()
        login = {'type': 'm.login.password', 'identifier': {'type': 'm.id.user', 'user': username},
                 'password': password, 'device_id': 'TT_CHAT_BRIDGE'}
        path = '/_synapse/admin/v1/register'
        nonce = self.call('GET', path).get('nonce')
        if not isinstance(nonce, str):
            raise ServiceError('Matrix registration nonce missing', 503)
        mac = hmac.new(self.secret.encode(), '\x00'.join((nonce, username, password, 'notadmin')).encode(), hashlib.sha1).hexdigest()
        try:
            self.call('POST', path, body={'nonce': nonce, 'username': username, 'password': password,
                                        'admin': False, 'mac': mac, 'inhibit_login': True})
        except ServiceError as error:
            if error.matrix_code != 'M_USER_IN_USE':
                raise
        result = self.call('POST', '/_matrix/client/v3/login', body=login)
        if not result.get('user_id') or not result.get('access_token'):
            raise ServiceError('Matrix login credentials missing', 503)
        return {'username': username, 'password': password, 'matrix_id': result['user_id'],
                'access_token': result['access_token']}

    def room(self, tenant, conversation_id, account, title):
        token = account['access_token']
        digest = hashlib.sha256(f'{tenant}:{conversation_id}'.encode()).hexdigest()
        local_alias = 'tt_' + digest
        alias = '#' + local_alias + ':' + account['matrix_id'].split(':', 1)[1]
        owner = {'tenant_id': tenant, 'conversation_id': conversation_id}
        resolve = '/_matrix/client/v3/directory/room/' + quoted(alias)
        try:
            found = self.call('GET', resolve, token)
        except ServiceError:
            try:
                found = self.call('POST', '/_matrix/client/v3/createRoom', token, {
                    'room_alias_name': local_alias, 'name': title[:255] or 'Internal chat',
                    'visibility': 'private', 'preset': 'private_chat',
                    'initial_state': [{'type': 'chat.ttt.owner', 'state_key': '', 'content': owner},
                                      {'type': 'm.room.history_visibility', 'state_key': '',
                                       'content': {'history_visibility': 'joined'}}]})
            except ServiceError:
                found = self.call('GET', resolve, token)
        room_id = found.get('room_id')
        if not isinstance(room_id, str):
            raise ServiceError('Matrix room id missing', 503)
        saved = self.call('GET', '/_matrix/client/v3/rooms/' + quoted(room_id) + '/state/chat.ttt.owner/', token)
        if saved != owner:
            raise ServiceError('Matrix alias ownership mismatch', 503)
        return room_id

    def send(self, room, token, transaction, content):
        result = self.call('PUT', '/_matrix/client/v3/rooms/' + quoted(room) + '/send/m.room.message/' + quoted(transaction), token, content)
        if not result.get('event_id'):
            raise ServiceError('Matrix event id missing', 503)
        return result['event_id']

    def redact(self, room, event, token, transaction):
        result = self.call('PUT', '/_matrix/client/v3/rooms/' + quoted(room) + '/redact/' + quoted(event) + '/' + quoted(transaction), token, {'reason': 'Source message revoked'})
        if not result.get('event_id'):
            raise ServiceError('Matrix redaction id missing', 503)

    def upload(self, token, raw, mime):
        result = self.call('POST', '/_matrix/media/v3/upload', token, raw=raw, mime=mime)
        uri = result.get('content_uri', '')
        if not uri.startswith('mxc://'):
            raise ServiceError('Matrix media URI missing', 503)
        return uri

    def download(self, token, uri):
        parts = uri.removeprefix('mxc://').split('/', 1)
        if len(parts) != 2 or not all(parts):
            raise ServiceError('Invalid Matrix media URI', 503)
        return self.call('GET', '/_matrix/client/v1/media/download/' + quoted(parts[0]) + '/' + quoted(parts[1]), token, download=True)
