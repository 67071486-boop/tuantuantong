import hashlib
import hmac
from urllib.parse import quote

import httpx


class PilotError(Exception):
    def __init__(self, message, status=400, matrix_code=None):
        super().__init__(message)
        self.status = status
        self.matrix_code = matrix_code


def quoted(value):
    return quote(str(value), safe='')


class Matrix:
    def __init__(self, url, client=None):
        self.url = url.rstrip('/')
        self.client = client or httpx.Client(timeout=30, trust_env=False)

    def call(self, method, path, token=None, body=None, params=None, raw=None, mime=None):
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        if mime:
            headers['Content-Type'] = mime
        try:
            response = self.client.request(method, self.url + path, headers=headers,
                                           json=body, params=params, content=raw)
        except httpx.HTTPError:
            raise PilotError('聊天服务器暂时不可用', 502) from None
        if not response.is_success:
            try:
                error = response.json()
            except ValueError:
                error = {}
            code = error.get('errcode') if isinstance(error, dict) else None
            raise PilotError(f'聊天服务器拒绝请求（{response.status_code}）', 502, code)
        if mime == 'download':
            return response.content, response.headers.get('content-type', '')
        try:
            result = response.json()
        except ValueError:
            raise PilotError('聊天服务器返回格式错误', 502) from None
        if not isinstance(result, dict) or 'errcode' in result:
            raise PilotError('聊天服务器返回失败结果', 502, result.get('errcode') if isinstance(result, dict) else None)
        return result

    def provision(self, user, secret):
        path = '/_synapse/admin/v1/register'
        nonce = self.call('GET', path)['nonce']
        mac = hmac.new(secret.encode(), digestmod=hashlib.sha1)
        mac.update(('\x00'.join([nonce, user['username'], user['password'], 'notadmin'])).encode())
        try:
            self.call('POST', path, body={'nonce': nonce, 'username': user['username'],
                      'password': user['password'], 'admin': False, 'mac': mac.hexdigest()})
        except PilotError as error:
            if error.matrix_code != 'M_USER_IN_USE':
                raise
        result = self.call('POST', '/_matrix/client/v3/login', body={
            'type': 'm.login.password', 'identifier': {'type': 'm.id.user', 'user': user['username']},
            'password': user['password'], 'device_id': 'TT_PILOT_ADAPTER'})
        return result['user_id'], result['access_token']

    def room(self, token, path, method='GET', body=None, params=None):
        return self.call(method, '/_matrix/client/v3/' + path, token, body, params)
