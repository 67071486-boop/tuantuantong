"""网页端 Chatwoot 小部件的**身份字段**（`/api/auth/{me,login}` 的 `user` 载荷；计划 `docs/plans/2026-10-22-web-chatwoot-widget.md` §2.3）。契约：`CHATWOOT_WEB_BASE_URL` / `CHATWOOT_WEB_WEBSITE_TOKEN` / `CHATWOOT_WEB_HMAC_TOKEN` 三个 env 都非空时，`user` 里才多出四个键（同生同灭）：`chat_identifier`（`web-<当前 schema>-<用户主键>`，公开——客户端要拿去 `setUser`）、`chat_identifier_hash`（`HMAC-SHA256(chat_identifier, CHATWOOT_WEB_HMAC_TOKEN)` 小写 hex，公开——协议就是要它）、`chat_base_url` / `chat_website_token`（公开，`chatwootSDK.run` 的两个参数）。⛔ 不许退化成匿名：不带 `identifier_hash` 的 `setUser` 在 Chatwoot 关掉 `hmac_mandatory` 时会被当匿名认、登录用户能在 devtools 里冒充别人；前端按「缺字段 ⇒ 不挂气泡」fail-closed。
带租户成分的原因：Chatwoot 按 `identifier` 合并联系人 ⇒ 跨租户的**裸主键**会把两个租户的同 id 用户并成一个人；`web-` 前缀还与 App 线的 `taker-` 分开（网页用户与 App 用户是否合并是产品决定，本批**不合并**）。`base_url` / `website_token` 也由后端下发（⛔ 前端不另建构建期配置）：它们是**公开值**，但**只该有一份真值源**——前端再要一份 `VITE_CHATWOOT_WEB_*` 的话，换收件箱要同时改后端 `.env` 与发版机前端配置，漏一处就是「气泡还连着旧收件箱」且两边都不报错；**hmac 密钥仍然只住服务端**（下发的只是用它算出的 hash）。与 `apps/accounts/app_chat_views.py` 不共用：那是 **App 平面**（Bearer 令牌 + 承载页 URL，读 `CHATWOOT_*`），本模块是**网页会话**、读 `CHATWOOT_WEB_*`，两套收件箱 / 凭据不同，避免改一处串掉另一边。三处容易写歪：① **每次现取 settings**（⛔ 不在 import 时缓存，`override_settings` 才打得中）；② **用 `connection.schema_name`**（identifier 要如实反映请求落在哪个平面，判据与 `_user_payload` 的 `identity_plane` 同源）；③ **函数返回补丁字典**（空字典 = 未配置），调用方 `payload.update(...)` 一处接线，⛔ 不要在视图里各写一份 if/else（登录与 `/me` 会漂）。
"""
from __future__ import annotations

import hashlib
import hmac

from django.conf import settings
from django.db import connection

#: `chat_identifier` 的前缀（与 App 线的 `taker-` 分开，见模块 docstring）。
IDENTIFIER_PREFIX = "web-"


def web_chat_identity(user) -> dict[str, str]:
    """当前用户的网页客服身份；**未配置齐全 ⇒ 空字典**（调用方据此一个键都不下发）。
    ⚠️ 返回值里的 hash 是**给客户端**的（Chatwoot 的 `setUser` 协议本来就要它）；密钥本身**不**出现在返回值 / 响应 / 日志里。
    """
    base_url = (settings.CHATWOOT_WEB_BASE_URL or "").strip()
    website_token = (settings.CHATWOOT_WEB_WEBSITE_TOKEN or "").strip()
    hmac_token = (settings.CHATWOOT_WEB_HMAC_TOKEN or "").strip()
    if not (base_url and website_token and hmac_token):
        return {}

    identifier = f"{IDENTIFIER_PREFIX}{connection.schema_name}-{user.pk}"
    # 公式与 Chatwoot 官方 `setUser(identifier, {identifier_hash})` 一致：小写十六进制。
    identifier_hash = hmac.new(
        hmac_token.encode(), identifier.encode(), hashlib.sha256
    ).hexdigest()
    return {
        "chat_identifier": identifier,
        "chat_identifier_hash": identifier_hash,
        # 公开值：前端拿它们拼 SDK 地址 / 调 `chatwootSDK.run`。单一真值源，见模块 docstring。
        "chat_base_url": base_url,
        "chat_website_token": website_token,
    }
