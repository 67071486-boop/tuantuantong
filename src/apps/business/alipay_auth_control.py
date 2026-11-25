"""租户侧控制服务客户端。"""
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlsplit

import httpx
from django.conf import settings


class ControlUnavailable(Exception):
    """控制服务未配置或当前不可用。"""


class ControlCaptchaFailed(ControlUnavailable):
    """验证码识别失败，可稍后重试。"""


class ControlOutcomeUnknown(ControlUnavailable):
    """上游写操作结果未知，需平台核对。"""


class ControlIdentityIncomplete(ControlUnavailable):
    """付款身份信息不完整。"""


class ControlIdentityUnverified(ControlUnavailable):
    """无法证明付款身份匹配。"""


@dataclass(frozen=True)
class ControlResult:
    status: str
    authorization_url: str | None = None
    configured: bool = False
    next_action: str | None = None


class AuthorizationControlClient:
    def __init__(self, credential: str, *, base_url: str = "", client_id: str = ""):
        self._credential = credential
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id

    def _post(self, path: str, payload: dict) -> ControlResult:
        parsed = urlsplit(self._base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query
                or parsed.fragment or parsed.username or parsed.password
                or not self._credential or not self._client_id):
            raise ControlUnavailable()
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timestamp, nonce = str(int(time.time())), secrets.token_hex(16)
        signed = "\n".join(("POST", path, timestamp, nonce, hashlib.sha256(body).hexdigest()))
        signature = hmac.new(self._credential.encode(), signed.encode(), hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Settlement-Client": self._client_id,
                   "X-Settlement-Timestamp": timestamp, "X-Settlement-Nonce": nonce,
                   "X-Settlement-Signature": signature}
        try:
            response = self._send(path, body, headers)
            if response.status_code == 503:
                data = response.json()
                if isinstance(data, dict) and data.get('code') == 'CAPTCHA_FAILED':
                    raise ControlCaptchaFailed()
            if response.status_code == 409:
                data = response.json()
                if isinstance(data, dict) and data.get("code") == "OUTCOME_UNKNOWN":
                    raise ControlOutcomeUnknown()
                if isinstance(data, dict) and data.get("code") == "IDENTITY_INCOMPLETE":
                    raise ControlIdentityIncomplete()
                if isinstance(data, dict) and data.get("code") == "IDENTITY_UNVERIFIED":
                    raise ControlIdentityUnverified()
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("status"), str):
                raise ValueError("invalid response")
            if path.endswith('/revoke') and data['status'] != 'revoked':
                raise ValueError("invalid cancellation outcome")
            if path.endswith('/identity-refresh') and data['status'] not in {'signed', 'authorized'}:
                raise ValueError("invalid identity outcome")
            if path.endswith('/revoke') and (data.get('configured') is not False or data.get('next_action') is not None):
                raise ValueError("invalid cancellation outcome")
            if path.endswith('/identity-refresh'):
                valid = (data['status'] == 'signed' and data.get('configured') is False and data.get('next_action') == 'identity') or (data['status'] == 'authorized' and data.get('configured') is True and data.get('next_action') is None)
                if not valid:
                    raise ValueError("invalid identity outcome")
            url = data.get("authorization_url")
            if url is not None and not isinstance(url, str):
                raise ValueError("invalid authorization URL")
            next_action = data.get("next_action")
            if next_action == "identity" and data["status"] != "signed":
                if path.endswith(('/revoke', '/identity-refresh')):
                    raise ValueError("invalid identity outcome")
                raise ControlIdentityIncomplete()
            if data["status"] == "authorized" and next_action is not None:
                raise ValueError("invalid authorized outcome")
            if (data["status"], next_action) not in {("pending", "agreement"), ("signed", "identity")}:
                next_action = None
            return ControlResult(data["status"], url, data.get("configured") is True, next_action)
        except (httpx.HTTPError, ValueError):
            if path.endswith(('/revoke', '/identity-refresh')):
                raise ControlOutcomeUnknown() from None
            raise ControlUnavailable() from None

    def _send(self, path, body, headers):
        return httpx.post(self._base_url + path, content=body, headers=headers,
                          timeout=10, follow_redirects=False)

    def create(self, request_id: str, account: str, real_name: str) -> ControlResult:
        return self._post("/authorizations", {"request_id": request_id, "account": account, "real_name": real_name})

    def authorize(self, request_id: str) -> ControlResult:
        return self._post(f"/authorizations/{request_id}/authorize", {"request_id": request_id})

    def refresh(self, request_id: str) -> ControlResult:
        return self._post(f"/authorizations/{request_id}/refresh", {"request_id": request_id})

    def identity(self, request_id: str) -> ControlResult:
        return self._post(f"/authorizations/{request_id}/identity", {"request_id": request_id})

    def identity_refresh(self, request_id: str) -> ControlResult:
        return self._post(f"/authorizations/{request_id}/identity-refresh", {"request_id": request_id})

    def revoke(self, request_id: str) -> ControlResult:
        return self._post(f"/authorizations/{request_id}/revoke", {"request_id": request_id, "confirm": True})


class LocalAuthorizationControlClient(AuthorizationControlClient):
    def __init__(self, credential):
        super().__init__(credential, base_url="https://local.invalid", client_id="local")

    def _send(self, path, body, headers):
        from django.test import RequestFactory
        from apps.settlement_control import views

        if path == "/authorizations":
            view, args = views.create, ()
        else:
            parts = path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "authorizations" or parts[2] not in {"authorize", "refresh", "identity", "identity-refresh", "revoke"}:
                raise ControlUnavailable()
            import uuid
            try:
                request_id = uuid.UUID(parts[1])
            except ValueError:
                raise ControlUnavailable() from None
            view, args = getattr(views, parts[2].replace('-', '_')), (request_id,)
        extra = {"HTTP_" + key.upper().replace("-", "_"): value for key, value in headers.items() if key.lower() != "content-type"}
        request = RequestFactory().generic("POST", path, body, content_type="application/json", **extra)
        response = view(request, *args)
        return httpx.Response(response.status_code, content=response.content,
                              request=httpx.Request("POST", self._base_url + path))


def get_control_client() -> AuthorizationControlClient:
    if getattr(settings, "ALIPAY_AUTH_LOCAL_ENABLED", False):
        from cryptography.fernet import Fernet
        from apps.settlement_control.locking import trusted_executable

        credential = getattr(settings, "ALIPAY_AUTH_CONTROL_CREDENTIAL", "")
        state_key = getattr(settings, "SETTLEMENT_FERNET_KEY", "")
        data_key = getattr(settings, "ALIPAY_AUTH_DATA_KEY", "")
        executable = getattr(settings, "SETTLEMENT_TESSERACT", "")
        lock = getattr(settings, "SETTLEMENT_OPERATION_LOCK_FILE", "")
        name = getattr(settings, "SETTLEMENT_CLIENT_NAMES", {}).get("local", "")
        local_keys = getattr(settings, "SETTLEMENT_CLIENT_KEYS", {})
        required = (getattr(settings, "SETTLEMENT_USERNAME", ""), getattr(settings, "SETTLEMENT_PASSWORD", ""),
                    getattr(settings, "SETTLEMENT_MERCHANT", ""), name)
        if (not all(isinstance(value, str) and value.strip() for value in required)
                or len(name) > 100 or not isinstance(credential, str) or len(credential) < 32
                or local_keys != {"local": credential}
                or not isinstance(state_key, str) or state_key in (data_key, credential)
                or not isinstance(executable, str) or not trusted_executable(executable)
                or not isinstance(lock, str) or not os.path.isabs(lock)):
            raise ControlUnavailable()
        try:
            Fernet(state_key.encode("ascii"))
        except (TypeError, ValueError, UnicodeError):
            raise ControlUnavailable() from None
        return LocalAuthorizationControlClient(credential)
    return AuthorizationControlClient(
        credential=getattr(settings, "ALIPAY_AUTH_CONTROL_CREDENTIAL", ""),
        base_url=getattr(settings, "ALIPAY_AUTH_CONTROL_URL", ""),
        client_id=getattr(settings, "ALIPAY_AUTH_CONTROL_CLIENT_ID", ""),
    )
