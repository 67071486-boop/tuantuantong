"""wechatpy 的薄封装（**唯一 import 点**，D-0379 计划 §1.3）。

- token 存储：`DBSessionStorage` 把 wechatpy 的 SessionStorage 落到 `WecomAppConfig`
  的 token 列。⛔ 必须 DB 存（生产无 Redis；三台机 × gunicorn 多进程，内存存储会互踢
  刷新、反复烧 token 配额——照 `WechatMpConfig` 反例 ③ 的同一教训）。
- token 正常到期由 wechatpy 负责刷新；需要有限次重试的调用禁用 SDK 自动重试，
  改用 `retry_rejected_token`。
- 加解密：`wechatpy.work.crypto.WeChatCrypto`（源码已核：`_decrypt_message` 先拿密文
  重算 sha1 验签、不符抛 `InvalidSignatureException`，然后才 AES；明文尾 corp_id 不匹配
  抛 `InvalidCorpIdException` ⇒ corp_id 配错的包天然被拒）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone

from apps.wecom.models import WecomAppConfig
from wechatpy.exceptions import WeChatClientException
from wechatpy.session import SessionStorage
from wechatpy.work import WeChatClient
from wechatpy.work.crypto import WeChatCrypto


def retry_rejected_token(client, call):
    """企微明确拒绝凭证后，刷新并重试一次。"""
    try:
        return call()
    except WeChatClientException as exc:
        if exc.errcode not in (40001, 40014, 42001):
            raise
    client.fetch_access_token()
    return call()


class DBSessionStorage(SessionStorage):
    """把两类 token（应用 / 客户联系）落到配置行的对应列。

    列按 `kind` 直落（wechatpy 的 key 混了 corp_id 与 secret 前缀，不按它寻列）；
    `get` / `set` 再按 key 后缀分流「token 本体 vs 过期时刻」，两法对称。
    过期时刻对外是 **epoch 秒浮点**（wechatpy 做 `expires_at - time.time()` 浮点运算；
    回 token 串或 datetime 都是同型 TypeError）。
    """

    def __init__(self, config: WecomAppConfig, *, kind: str) -> None:
        self._config = config
        self._kind = kind

    def _token_field(self) -> str:
        return "access_token" if self._kind == "main" else "contacts_token"

    def _expires_field(self) -> str:
        return "access_token_expires_at" if self._kind == "main" else "contacts_token_expires_at"

    def _persist(self):
        fields = [self._token_field(), self._expires_field()]
        changed = WecomAppConfig.objects.filter(
            pk=self._config.pk, identity_revision=self._config.identity_revision,
            corp_id=self._config.corp_id, agent_id=self._config.agent_id,
            secret=self._config.secret, contacts_secret=self._config.contacts_secret,
        ).update(**{name: getattr(self._config, name) for name in fields})
        if not changed:
            raise RuntimeError("企微配置已改变，请重新获取客户端")

    def get(self, key, default=None):
        if key.endswith("_expires_at"):
            expires = getattr(self._config, self._expires_field())
            return default if expires is None else expires.timestamp()
        return getattr(self._config, self._token_field()) or default

    def set(self, key, value, ttl=None):
        if value is None:
            return
        # wechatpy 的两次 set：token 本体（key = `…_access_token`，ttl = 秒数）+
        # `expires_at` setter（key = `…_expires_at`，value = **epoch 秒**）。按 key 分流。
        if key.endswith("_expires_at"):
            setattr(
                self._config,
                self._expires_field(),
                datetime.fromtimestamp(value, tz=dt_timezone.utc)
                if isinstance(value, (int, float))
                else value,
            )
        else:
            setattr(self._config, self._token_field(), value)
            setattr(
                self._config,
                self._expires_field(),
                timezone.now() + timedelta(seconds=ttl) if ttl else None,
            )
        self._persist()

    def delete(self, key):
        setattr(self._config, self._token_field(), "")
        setattr(self._config, self._expires_field(), None)
        self._persist()


def get_client(config: WecomAppConfig, *, timeout=None, auto_retry=True) -> WeChatClient:
    """应用消息 / 通讯录面的客户端（用应用 secret；token 缓存在 `access_token` 两列）。"""
    return WeChatClient(
        config.corp_id, config.secret, session=DBSessionStorage(config, kind="main"),
        timeout=timeout, auto_retry=auto_retry,
    )


def get_contacts_client(config: WecomAppConfig) -> WeChatClient:
    """客户联系面的客户端（token 分列存）；contacts_secret 选填：留空 = 用应用 secret
    （官方现行口径：应用在企微后台「客户联系 → 可调用接口的应用」授权后即可这样调）。
    """
    return WeChatClient(
        config.corp_id,
        config.contacts_secret or config.secret,
        session=DBSessionStorage(config, kind="contacts"),
    )


def build_crypto(config: WecomAppConfig) -> WeChatCrypto:
    """回调加解密器。⛔ 调用方先验 `encoding_aes_key` 是 43 位（见 `views._usable_crypto`）：
    wechatpy 对坏 key 抛的是裸 `AssertionError` / `binascii.Error`，不接住就是 500。
    """
    return WeChatCrypto(config.callback_token, config.encoding_aes_key, config.corp_id)
