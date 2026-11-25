"""打手 App 平面：**在线客服身份 + 承载页地址**（`GET /api/app/chat/identity/`，带尾斜杠，与 `/api/app/auth/me/` 同形；spec `docs/specs/2026-10-21-taker-app-customer-service.md` §2 / §4，验收判据 1–5）。认证 = 打手 **App 自己的登录令牌**（`Authorization: Bearer <token>`，`AppTokenAuthentication`，走全局默认）+ `IsAuthenticated`（⛔ 不是空列表：空列表会让匿名请求**继续以匿名身份执行**）；成功回 `{"code":0,"msg":"","data":{"enabled":true,"url":"…"}}`，未配置回 `200 + enabled=false, url=""`（⛔ 不许 500、⛔ 不许 404）。⭐ 落在 `/api/app/` 而不是 `/api/taker/v1/`：那是**第三方应用**凭据平面（OAuth `client_credentials`），打手 App 用的是**用户登录令牌**（`AppToken`），两者互不通用（拿错即对端 401「应用凭据无效」）。
⛔ URL 由**后端**拼、客户端只负责原样打开：`identifier_hash` = `HMAC-SHA256(identifier, 收件箱 hmac 密钥)`（Chatwoot `setUser` 协议），密钥**绝不能进客户端**；客户端也不做 URL 编码（UTS 没有 `encodeURIComponent`）⇒ 四个值一律经 `urllib.parse.urlencode`，本模块是这件事的唯一出口。`identifier` = `taker-<租户 schema>-<用户主键>`——带租户成分是**防合并**的（多租户共用 Chatwoot 实例时同主键用户不会串成同一联系人）。未配置判据：`CHATWOOT_BASE_URL` / `CHATWOOT_WEBSITE_TOKEN` / `CHATWOOT_HMAC_TOKEN` / `CHATWOOT_PAGE_PATH` 四个键**任一为空**（`strip()` 后）⇒ 回「未开通」；⚠️ `CHATWOOT_WEBSITE_TOKEN` **不进 URL**（承载页 `deploy/chatwoot/app-chat.html` 自己带着它，公开值；本端点只把它当"配好了没有"的闸）。
"""
from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

from django.conf import settings
from django.db import connection
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.app_auth_views import AppPlaneMixin
from apps.accounts.display import user_display_name
from apps.core.envelope import api_response

#: 承载页读的 `c`（来源）参数。打手端固定 `app`（spec §2 的 URL 契约）。
#: ⚠️ 它是**来源标签**，不参与鉴权；⛔ 别拿它当租户 / 角色判据。
CHAT_SOURCE = "app"

#: `identifier` 的前缀：`taker-<租户 schema>-<用户主键>`。
IDENTIFIER_PREFIX = "taker-"


def _chat_config():
    """读四个 env；**任一为空** ⇒ `None`（= 整条链路未开通）。返回 `(base_url, page_path, hmac_token)`（已 `strip()`）。
    ⚠️ `website_token` 只在这里参与"配没配齐"的判断，**不进 URL**（承载页自己带着它）。⚠️ 本函数**每次请求**都从 `django.conf.settings` 现取（⛔ 不在 import 时缓存）：测试里 `override_settings` 能打中、部署时改 `.env` 重启即生效。
    """
    base_url = (settings.CHATWOOT_BASE_URL or "").strip()
    website_token = (settings.CHATWOOT_WEBSITE_TOKEN or "").strip()
    hmac_token = (settings.CHATWOOT_HMAC_TOKEN or "").strip()
    page_path = (settings.CHATWOOT_PAGE_PATH or "").strip()
    if not (base_url and website_token and hmac_token and page_path):
        return None
    return base_url, page_path, hmac_token


def identifier_for(user) -> str:
    """`taker-<租户 schema>-<用户主键>`（见模块 docstring 的「防合并」）。"""
    return f"{IDENTIFIER_PREFIX}{connection.schema_name}-{user.pk}"


def chat_identity_url(user) -> str:
    """拼出打手端承载页地址；未配置齐全 ⇒ **空串**（调用方据此回 `enabled=false`）。
    ⚠️ 四个查询参数的顺序固定 `id` / `n` / `h` / `c`（spec §2 的示例逐字）；值全部经 `urlencode` 编码（昵称里可能有空格 / 中文 / `&`）。
    """
    config = _chat_config()
    if config is None:
        return ""
    base_url, page_path, hmac_token = config

    identifier = identifier_for(user)
    # spec §2 的公式逐字：HMAC-SHA256(identifier, 收件箱 hmac 密钥)，小写十六进制。
    identifier_hash = hmac.new(
        hmac_token.encode(), identifier.encode(), hashlib.sha256
    ).hexdigest()
    query = urlencode(
        {
            "id": identifier,
            "n": user_display_name(user),
            "h": identifier_hash,
            "c": CHAT_SOURCE,
        }
    )
    # 两端都归一化一次：`CHATWOOT_BASE_URL` 常被写成带尾斜杠的形态，而 `PAGE_PATH`
    # 可能写成不带前导斜杠的形态 ⇒ 直接相加会得到 `//app-chat.html` 或 `app-chat.html`。
    return f"{base_url.rstrip('/')}/{page_path.lstrip('/')}?{query}"


class AppChatIdentityView(AppPlaneMixin, APIView):
    """`GET /api/app/chat/identity/` —— 返回"客服开没开 + 打开哪个地址"（spec §2）。
    装配：`permission_classes = [IsAuthenticated]` **显式**要认证（⛔ 不写空列表——DRF 里那等于"匿名也放行"，本端点会拿匿名用户去算 identifier）；认证类走全局默认（`Session` + `AppToken` Bearer）；`AppPlaneMixin`：`public` 平面一律 404（本视图读租户库的 settings / 用户）。未配置不是错误：spec §4-3 要求「env 缺任一必填 ⇒ **200** + `enabled=false`」——**产品态的降级**（App 显示"客服未开通"），不是服务端故障 ⇒ ⛔ 不许抛 500、⛔ 不许回 404。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        url = chat_identity_url(request.user)
        return api_response({"enabled": bool(url), "url": url})
