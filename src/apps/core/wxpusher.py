"""WxPusher 标准推送接口适配器。"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Self
from urllib.parse import parse_qs, urlsplit

import httpx

from apps.core.wxpusher_app_links import is_daidaitong_app_link

API_BASE = "https://wxpusher.zjiecode.com/api"
_SUCCESS = 1000
_SUBSCRIPTION_LINK = re.compile(r"https://wxpusher\.zjiecode\.com/api/s/[0-9A-Za-z_-]{1,192}(?:\?jump=auto)?")
_QR_URL = re.compile(r"https://wxpusher\.zjiecode\.com/api/qrcode/[0-9A-Za-z_-]+\.jpg")


def is_wxpusher_qr(url: str) -> bool:
    return bool(_QR_URL.fullmatch(url))


def is_wxpusher_subscription_link(url: str) -> bool:
    return bool(_SUBSCRIPTION_LINK.fullmatch(url))


def public_follow_url(value: str, *, app_id: str | None = None) -> str | None:
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if (
        url.scheme != "https"
        or url.netloc != "wxpusher.zjiecode.com"
        or url.path != "/app/"
        or url.query
    ):
        return None
    path, _, query = url.fragment.partition("?")
    args = parse_qs(query, keep_blank_values=True)
    if path != "/follow" or args.keys() != {"type", "id"} or args["type"] != ["1"]:
        return None
    app_ids = args["id"]
    if len(app_ids) == 1 and re.fullmatch(r"[1-9][0-9]*", app_ids[0]):
        if app_id is None or app_ids[0] == app_id:
            return f"https://wxpusher.zjiecode.com/app/#/follow?type=1&id={app_ids[0]}"
    return None


class WxPusherError(Exception):
    """请求失败或响应不符合接口契约。"""


@dataclass(frozen=True)
class WxPusherQRCode:
    code: str
    url: str
    link: str = ""


def _is_https_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        return (
            parsed.scheme == "https" and bool(host) and "%" not in host
            and parsed.username is None and parsed.password is None
            and parsed.port != 0
            and "\\" not in url
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
        )
    except ValueError:
        return False


class WxPusherClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None, timeout: float = 10.0):
        self._transport = transport if transport is not None else httpx.HTTPTransport(trust_env=False)
        self._timeout = httpx.Timeout(timeout)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._transport.close()

    def _request(self, method: str, path: str, **kwargs):
        # 订阅查询的 URL 带密钥，直接走 transport 以避开 Client 的整条 URL 日志。
        request = httpx.Request(
            method, f"{API_BASE}{path}", extensions={"timeout": self._timeout.as_dict()}, **kwargs,
        )
        try:
            response = self._transport.handle_request(request)
            try:
                if not response.is_success:
                    raise WxPusherError(f"WxPusher 返回 HTTP {response.status_code}")
                response.read()
                payload = response.json()
            finally:
                response.close()
        except httpx.TimeoutException:
            raise WxPusherError("WxPusher 请求超时，处理结果待确认") from None
        except httpx.HTTPError:
            raise WxPusherError("WxPusher 网络请求失败") from None
        except ValueError:
            raise WxPusherError("WxPusher 返回了无效的 JSON") from None

        if not isinstance(payload, dict) or payload.get("code") != _SUCCESS:
            raise WxPusherError("WxPusher 未确认受理请求")
        return payload.get("data")

    def _send_request(
        self, *, app_token: str, uids: Sequence[str], content: str,
        summary: str | None, url: str | None, html: bool,
    ):
        if not app_token.strip() or not uids or any(not uid.strip() for uid in uids):
            raise WxPusherError("发送密钥和收件人 UID 不能为空")
        payload = {"appToken": app_token, "uids": list(uids), "content": content, "contentType": 2 if html else 1}
        if summary is not None:
            payload["summary"] = summary
        if url is not None:
            if not (_is_https_url(url) or is_daidaitong_app_link(url)):
                raise WxPusherError("回跳链接必须是完整的 HTTPS 地址或代代通链接")
            payload["url"] = url
        return self._request("POST", "/send/message", json=payload)

    @staticmethod
    def _accepted_content_id(target):
        content_id = target.get("messageContentId")
        if target.get("code") == _SUCCESS and type(content_id) is int and content_id > 0:
            return content_id
        return None

    def send(
        self, *, app_token: str, uid: str, content: str, summary: str | None = None,
        url: str | None = None, html: bool = False,
    ) -> int:
        """返回已受理的消息内容编号，送达情况需在接收设备查看。"""
        data = self._send_request(app_token=app_token, uids=[uid], content=content, summary=summary, url=url, html=html)
        if isinstance(data, list) and len(data) == 1:
            target = data[0]
            if isinstance(target, dict) and target.get("uid") == uid:
                content_id = self._accepted_content_id(target)
                if content_id is not None:
                    return content_id
        raise WxPusherError("WxPusher 未确认受理指定收件人的消息")

    def send_many(
        self, *, app_token: str, uids: Sequence[str], content: str, summary: str | None = None,
        url: str | None = None, html: bool = False,
    ) -> dict[str, int]:
        """只返回 WxPusher 明确受理且回执无重复的收件人。"""
        requested = set(uids)
        if len(requested) != len(uids) or len(requested) > 2000:
            raise WxPusherError("收件人 UID 不能重复且每批最多 2000 个")
        data = self._send_request(
            app_token=app_token, uids=uids, content=content, summary=summary, url=url, html=html,
        )
        if not isinstance(data, list):
            raise WxPusherError("WxPusher 未返回有效的收件人回执")
        seen = set()
        accepted = {}
        for target in data:
            if not isinstance(target, dict):
                continue
            uid = target.get("uid")
            if not isinstance(uid, str) or uid not in requested:
                continue
            if uid in seen:
                accepted.pop(uid, None)
                continue
            seen.add(uid)
            content_id = self._accepted_content_id(target)
            if content_id is not None:
                accepted[uid] = content_id
        return accepted

    def create_qrcode(self, *, app_token: str, extra: str, valid_time: int = 300) -> WxPusherQRCode:
        """extra 由调用方生成并关联待确认的本地会话。"""
        if not app_token.strip() or not extra.strip() or len(extra) > 64:
            raise WxPusherError("创建二维码需要发送密钥和不超过 64 字符的随机参数")
        if type(valid_time) is not int or not 1 <= valid_time <= 30 * 24 * 60 * 60:
            raise WxPusherError("二维码有效期必须在 1 秒到 30 天之间")
        data = self._request("POST", "/fun/create/qrcode", json={
            "appToken": app_token, "extra": extra, "validTime": valid_time,
        })
        if isinstance(data, dict):
            code, url = data.get("code"), data.get("url")
            if isinstance(code, str) and code.strip() and isinstance(url, str) and _is_https_url(url):
                link = data.get("link")
                if not isinstance(link, str) or not is_wxpusher_subscription_link(link):
                    link = ""
                return WxPusherQRCode(code=code, url=url, link=link)
        raise WxPusherError("WxPusher 未返回有效的二维码")

    def verify_scanned_uid(self, *, app_token: str, code: str, app_id: str = "") -> str | None:
        """复核扫码者订阅。调用方须从当前会话的待绑定记录取同一应用签发的 code。"""
        if not app_token.strip() or not code.strip():
            raise WxPusherError("查验扫码需要发送密钥和二维码编号")
        uid = self._request("GET", "/fun/scan-qrcode-uid", params={"code": code})
        if not isinstance(uid, str) or not uid.strip():
            raise WxPusherError("WxPusher 未返回有效的扫码 UID")
        return uid if self.is_subscribed(app_token=app_token, uid=uid, app_id=app_id) else None

    def is_subscribed(self, *, app_token: str, uid: str, app_id: str = "") -> bool:
        subscription = self._request("GET", "/fun/wxuser/v2", params={
            "appToken": app_token, "uid": uid, "type": 0, "isBlock": False, "page": 1, "pageSize": 100,
        })
        records = subscription.get("records") if isinstance(subscription, dict) else None
        if not isinstance(records, list):
            raise WxPusherError("WxPusher 未返回有效的订阅列表")
        for record in records:
            if (
                isinstance(record, dict) and record.get("uid") == uid
                and type(record.get("type")) is int and record["type"] == 0
                and record.get("reject") is False
                and (not app_id or str(record.get("appOrTopicId")) == app_id)
            ):
                return True
        return False
