"""微信小程序身份换取（`jscode2session`，打手小程序支付）。为什么单独一个模块：`code2session` 是**微信**的接口、不是 Jeepay 的，塞进 `jeepay.py` 会让「支付网关」同时依赖小程序的 AppID/AppSecret；放这里，调用方（`app_wallet_views`）只看到两个入口：`ensure_configured()` 与 `exchange_code()`。
三条硬判据：① **openid 按 AppID 隔离**——小程序 openid 只能由这个 AppID 下的 `code2session` 拿到，公众号那串不能复用（业务方实测报「无效的 openid」）⇒ 只读 `settings.WX_MINI_APP_ID` / `WX_MINI_APP_SECRET`，⛔ 不碰公众号那一组；② **未配置 ⇒ 业务错误，⛔ 不 500**——AppID / AppSecret 任一为空抛 `WxMiniError`（文案含「小程序支付未配置」），由视图翻成业务码；③ **上游失败只带 errcode，不回显 errmsg**——errcode 足够排障，errmsg 是微信原文，⛔ 不往客户端 / 日志里搬（日志只记 errcode）。
"""
import logging

import httpx
from django.conf import settings

from apps.core.error_codes import ErrorCode

logger = logging.getLogger(__name__)

#: 微信「登录凭证校验」端点（GET，`grant_type=authorization_code`）。
JSCODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"

#: 未配置时给客户端的那句话（视图断言与排障都认它）。
MINI_NOT_CONFIGURED_MSG = "小程序支付未配置"


class WxMiniError(Exception):
    """小程序登录的**预期**失败（未配置 / 微信拒绝 / 网络）。调用方翻成业务码，⛔ 不 500。"""

    def __init__(self, message, code=ErrorCode.BUSINESS):
        super().__init__(message)
        self.code = code


def app_config() -> tuple[str, str]:
    """读并校验 `WX_MINI_APP_ID` / `WX_MINI_APP_SECRET`，缺项抛 `WxMiniError`。"""
    app_id = (getattr(settings, "WX_MINI_APP_ID", "") or "").strip()
    secret = (getattr(settings, "WX_MINI_APP_SECRET", "") or "").strip()
    missing = [name for name, value in (("WX_MINI_APP_ID", app_id), ("WX_MINI_APP_SECRET", secret)) if not value]
    if missing:
        raise WxMiniError(
            MINI_NOT_CONFIGURED_MSG + "，缺：" + "、".join(missing) + "（见 settings.WX_MINI_*）",
            ErrorCode.BUSINESS,
        )
    return app_id, secret


def ensure_configured() -> str:
    """配置自检并返回小程序 AppID（不做网络请求；给 `way=WX_LITE` 在改状态前用）。"""
    return app_config()[0]


def exchange_code(code: str) -> dict:
    """用 `wx.login()` 的 `code` 换 openid。返回 `{"openid", "app_id"}`。⚠️ **不返回 session_key**：本仓不拿它解密任何东西（openid 只用于付款）。
    """
    app_id, secret = app_config()
    try:
        response = httpx.get(
            JSCODE2SESSION_URL,
            params={
                "appid": app_id,
                "secret": secret,
                "js_code": code,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
    except httpx.HTTPError:
        # ⛔ 不把异常消息拼进对外文案（可能带 URL 里的 secret）。
        raise WxMiniError("微信登录失败，请稍后重试", ErrorCode.BUSINESS) from None

    try:
        result = response.json()
    except ValueError:
        raise WxMiniError(
            f"微信登录失败（HTTP {response.status_code}），请稍后重试", ErrorCode.BUSINESS
        ) from None
    if not isinstance(result, dict):
        raise WxMiniError("微信登录失败（返回结构不对），请稍后重试", ErrorCode.BUSINESS)

    errcode = result.get("errcode")
    # ⚠️ 成功响应**没有** errcode 字段；有且非 0 才是失败（别用真值判断：0 是成功）。
    if errcode is not None and str(errcode) != "0":
        logger.warning("小程序 code2session 失败：errcode=%s", errcode)
        raise WxMiniError(
            f"微信登录失败（errcode：{errcode}），请重新进入小程序", ErrorCode.BUSINESS
        )

    openid = str(result.get("openid") or "").strip()
    if not openid:
        raise WxMiniError("微信登录失败（未返回 openid），请重新进入小程序", ErrorCode.BUSINESS)
    return {"openid": openid, "app_id": app_id}
