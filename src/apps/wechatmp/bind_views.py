"""网页授权绑定 openid（计划 T4，核心流；H3/H4/S2 订正都在这一条链上）。

## 流程（五步，绕开 m/ 打手门）

1. 公众号菜单「绑定账号」(view) → `GET /api/wechatmp/bind-entry`：
   生成一次性 `state` 存 session → 302 跳微信 OAuth（`snsapi_base`，`state` 随行）；
2. 微信带 `code+state` 回 `GET /api/wechatmp/bind-callback`：
   校验 state 与 session 一致（**S2 防 CSRF**：没有这步，攻击者可以把**自己的** code
   换来的 openid 塞进受害者的 session，让受害者把**攻击者的**微信号绑到自己账号上）
   ⇒ code 换 openid ⇒ **openid 存 session** ⇒ 302 跳 `/wechatmp/bind-confirm`；
3. `GET/POST /wechatmp/bind-confirm`：**独立服务端渲染页**（⛔ 不在 m/ SPA 内 ——
   m/router/guards.ts 有「只服务打手」身份门，客服会被弹回桌面；本页自成一路，
   session 未认证 ⇒ 页面自带登录表单，客服与打手都能登）；
4. 登录后绑定落库（重绑 = 换绑覆盖 + 操作日志）；同页有解绑按钮（删行 + 日志）。

## H4 判据钉的落点

OAuth 存的 `wechatmp_oauth_openid` 写在**登录之前**。Django 5.2 的 `login()` 会
`cycle_key()`，但**保留**匿名会话数据（django/contrib/auth/__init__.py 的 `login`
docstring："Note that data set during the anonymous session is retained when the
user logs in."）⇒ 登录后 openid 仍在 session，第 3 步拿得到它。
`tests/test_wechatmp_bind.py` 有这条钉（session 写 openid → `authenticate`+`login`
→ openid 仍在）。
"""
from __future__ import annotations

import secrets
from urllib.parse import quote

from django.contrib.auth import authenticate, login
from django.db import connection, transaction
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.core.login_rate_limit import (
    RATE_LIMITED_MSG,
    clear_login_failures,
    login_blocked,
    login_source_ip,
    record_login_failure,
)
from apps.core.plane import on_public_plane, plane_not_found_response
from apps.wechatmp import services
from apps.wechatmp.models import WechatMpBinding
from apps.wechatmp.views import public_base_url

#: OAuth 一次性 state 的 session 键。
SESSION_STATE_KEY = "wechatmp_oauth_state"
#: code 换来的 openid 的 session 键（H4：它要活着穿过 `login()`）。
SESSION_OPENID_KEY = "wechatmp_oauth_openid"
#: openid 对应的公众号 AppID 的 session 键。
SESSION_APPID_KEY = "wechatmp_oauth_appid"

#: 微信网页授权入口（公开文档常量；`snsapi_base` = 静默授权，只要 openid）。
OAUTH_AUTHORIZE_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"

#: 确认页路径（与 `page_urls.py` 逐字一致）。
BIND_CONFIRM_PATH = "/wechatmp/bind-confirm"


class _OAuthQuerySerializer(serializers.Serializer):
    """微信回跳时 URL 上的两个参数。"""

    code = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    state = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")


class _LoginFormSerializer(serializers.Serializer):
    """确认页登录表单的入参（错误文案**带字段名**，AGENTS.md §5-G 口诀）。"""

    username = serializers.CharField(max_length=150, allow_blank=False)
    password = serializers.CharField(max_length=128, allow_blank=False)


def _page(request, context: dict, status: int = 200) -> HttpResponse:
    """渲染确认页（`stage` 决定正文是哪一块）。

    ⚠️ `render()` 直接收 DRF 的 `Request` 没问题：它按 `__getattr__` 代理到底层
    `HttpRequest`（`request.user` / `request.META` 都取得到，上下文处理器照跑）。
    """
    return render(request, "wechatmp/bind_confirm.html", context, status=status)


def _error_page(request, message: str, status: int = 403) -> HttpResponse:
    return _page(request, {"stage": "error", "error_message": message}, status=status)


def _bind_openid(*, user, appid: str, openid: str) -> tuple[WechatMpBinding, bool]:
    """绑定落库（换绑 = 覆盖 + 日志）。返回 `(binding, created)`。

    ⚠️ `(appid, openid)` 唯一：同一个微信号只能挂一个账号 —— 换绑到新账号时，先把
    别人名下那行删掉（= 微信号搬家），再 upsert 本账号的行。两步在同一个事务里。
    ⚠️ `subscribed` 沿模型默认 True（菜单入口只有关注者可见；见模型列注释的推断标注）。
    """
    with transaction.atomic():
        WechatMpBinding.objects.filter(appid=appid, openid=openid).exclude(user_id=user.pk).delete()
        binding, created = WechatMpBinding.objects.update_or_create(
            user=user, defaults={"appid": appid, "openid": openid, "subscribed": True}
        )
        record(
            log_type=ACTION_LOG_TYPES["system.wechatmp_binding_create"],
            action="system.wechatmp_binding_create",
            actor=user.get_username(),
            actor_id=user.pk,
            target_kind="user",
            target_id=user.pk,
            target_name=user.get_username(),
            reason=openid[:8],
        )
    return binding, created


def _unbind_openid(*, user) -> int:
    """解绑 = 删本账号的绑定行 + 日志。返回删了几行。"""
    with transaction.atomic():
        deleted, _ = WechatMpBinding.objects.filter(user=user).delete()
        record(
            log_type=ACTION_LOG_TYPES["system.wechatmp_binding_delete"],
            action="system.wechatmp_binding_delete",
            actor=user.get_username(),
            actor_id=user.pk,
            target_kind="user",
            target_id=user.pk,
            target_name=user.get_username(),
            reason="用户在绑定确认页解绑",
        )
    return deleted


class WechatMpBindEntryView(APIView):
    """`GET /api/wechatmp/bind-entry` —— 生成一次性 state，302 跳微信 OAuth。"""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    wechatmp_raw_contract = True

    def get(self, request):
        if on_public_plane():
            return plane_not_found_response()
        config = services.get_ready_config()
        if config is None:
            return _error_page(request, "公众号还没配置好，请联系管理员先完成「微信公众号设置」")
        state = secrets.token_urlsafe(16)
        request.session[SESSION_STATE_KEY] = state
        redirect_uri = f"{public_base_url(request)}/api/wechatmp/bind-callback"
        # ⚠️ `redirect_uri` 必须**整体** percent-encode（微信按整串回跳）；`state` 同样编码。
        oauth_url = (
            f"{OAUTH_AUTHORIZE_URL}?appid={quote(config.appid, safe='')}"
            f"&redirect_uri={quote(redirect_uri, safe='')}"
            f"&response_type=code&scope=snsapi_base&state={quote(state, safe='')}"
            "#wechat_redirect"
        )
        return HttpResponseRedirect(oauth_url)


class WechatMpBindCallbackView(APIView):
    """`GET /api/wechatmp/bind-callback` —— 校验 state ⇒ code 换 openid ⇒ 存 session。"""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    wechatmp_raw_contract = True

    def get(self, request):
        if on_public_plane():
            return plane_not_found_response()
        config = services.get_ready_config()
        if config is None:
            return _error_page(request, "公众号还没配置好，请联系管理员先完成「微信公众号设置」")
        serializer = _OAuthQuerySerializer(data=request.query_params)
        if not serializer.is_valid():
            return _error_page(request, "授权参数不完整，请从公众号菜单重新进入")
        query = serializer.validated_data
        # ⭐ S2：state 必须与本会话此前生成的一致；**一次性**（取出即删，重放同一链接无效）。
        session_state = request.session.get(SESSION_STATE_KEY)
        request.session.pop(SESSION_STATE_KEY, None)
        if not session_state or not query["state"] or not secrets.compare_digest(
            session_state, query["state"]
        ):
            return _error_page(request, "授权状态校验失败，请从公众号菜单重新进入")
        if not query["code"]:
            return _error_page(request, "微信没有带回授权码，请从公众号菜单重新进入")
        body, err = services.exchange_openid(code=query["code"])
        if err:
            return _error_page(request, f"微信授权失败：{err}")
        # ⭐ H4：openid 在**登录之前**写进 session —— Django 的 login() 保留匿名会话数据，
        # 它会活着穿过登录（专钉见 tests/test_wechatmp_bind.py 的 session 用例）。
        request.session[SESSION_OPENID_KEY] = str(body.get("openid") or "")
        request.session[SESSION_APPID_KEY] = config.appid
        return HttpResponseRedirect(BIND_CONFIRM_PATH)


@method_decorator(csrf_protect, name="dispatch")
@method_decorator(ensure_csrf_cookie, name="dispatch")
class WechatMpBindConfirmView(APIView):
    """`GET/POST /wechatmp/bind-confirm` —— 独立服务端渲染的绑定确认页（H3：不在 m/ SPA 内）。

    **认证形态**：默认会话认证 + `AllowAny` —— 匿名看到登录表单，已登录看到绑定卡。
    **CSRF**：页面带 `{% csrf_token %}` 表单；`csrf_protect` 补上匿名 POST 那半边
    （DRF 的 `as_view()` 全员 csrf_exempt、`SessionAuthentication.enforce_csrf` 只管
    已认证 —— 同 `LoginView` 的 D15 口径）。
    """

    permission_classes = [AllowAny]
    #: `tests/test_no_route_500.py` 的框架事实判据：本页回**服务端渲染 HTML**（浏览器
    #: 原生形状，不是本仓信封）—— 新豁免类 `WECHATMP_SHAPED_ROUTES` 按它对拍。
    wechatmp_raw_contract = True

    # ── 页面状态机 ────────────────────────────────────────────────────────

    def _stage_context(self, request) -> dict:
        openid = request.session.get(SESSION_OPENID_KEY) or ""
        if not request.user.is_authenticated:
            return {"stage": "login"}
        if not openid:
            return {"stage": "no_oauth"}
        binding = WechatMpBinding.objects.filter(user=request.user).first()
        if binding is None:
            return {"stage": "confirm", "openid": openid}
        return {
            "stage": "bound",
            "openid": binding.openid,
            # ⚠️ 视图层预格式化（本仓 TEMPLATES 没挂 `tz` 上下文处理器，
            # 模板里的 `localtime` 过滤器不存在 —— 首跑实测）。
            "bound_at": timezone.localtime(binding.bound_at).strftime("%Y-%m-%d %H:%M")
            if binding.bound_at is not None
            else "",
            "rebind": binding.openid != openid,
        }

    def get(self, request):
        if on_public_plane():
            return plane_not_found_response()
        return _page(request, self._stage_context(request))

    def post(self, request):
        if on_public_plane():
            return plane_not_found_response()
        action = str(request.data.get("action") or "")
        if action == "login":
            return self._post_login(request)
        if action == "bind":
            return self._post_bind(request)
        if action == "unbind":
            return self._post_unbind(request)
        return _error_page(request, "未知操作，请从公众号菜单重新进入")

    # ── 三个动作 ──────────────────────────────────────────────────────────

    def _post_login(self, request):
        # ── 登录温和限流（D-0332）：闸在表单校验与 `authenticate` 之前——被挡的请求
        # 不查库、不算口令哈希。阈值与口径唯一出处：`apps/core/login_rate_limit.py`。──
        ip = login_source_ip(request)
        username = str(request.data.get("username") or "")
        if login_blocked(ip=ip, username=username):
            # 与本页既有登录失败**同一形状**（200 + stage=login + error_message），
            # 只换三口共用的固定文案；⛔ 不从限流文案里泄露「账号存在」。
            return _page(request, {"stage": "login", "error_message": RATE_LIMITED_MSG})
        serializer = _LoginFormSerializer(data=request.data)
        if not serializer.is_valid():
            return _page(request, {"stage": "login", "error_message": "请填写账号和密码"})
        attrs = serializer.validated_data
        # `authenticate` 一次覆盖三种失败（不存在 / 密码错 / is_active=False），
        # 与 LoginView 同口径：不泄露到底错在哪。
        user = authenticate(request, username=attrs["username"], password=attrs["password"])
        if user is None:
            # 凭据失败：IP 维与账号维各记一笔（触线即设锁标记；D-0332）。
            record_login_failure(ip=ip, username=username)
            return _page(request, {"stage": "login", "error_message": "账号或密码不正确"})
        login(request, user)
        # 登录成功 ⇒ 两维失败计数清零（D-0332；锁标记不清，理由见限流模块）。
        clear_login_failures(ip=ip, username=username)
        # ⚠️ identity_plane 同样必须在 login() **之后**写（与 LoginView 同一条要求；
        # `login()` 对匿名会话保留数据，但对**换绑到另一个已认证用户**的会话会整段
        # flush ⇒ 任何"必须活过登录"的键都写在它之后，两边口径一致）。
        request.session["identity_plane"] = connection.schema_name
        # 302 回 GET：防表单重复提交，也让下面的渲染走同一条状态机。
        return HttpResponseRedirect(BIND_CONFIRM_PATH)

    def _post_bind(self, request):
        if not request.user.is_authenticated:
            return _error_page(request, "请先登录再绑定")
        openid = request.session.get(SESSION_OPENID_KEY) or ""
        appid = request.session.get(SESSION_APPID_KEY) or ""
        if not openid or not appid:
            return _error_page(request, "授权信息已过期，请从公众号菜单重新进入")
        _bind_openid(user=request.user, appid=appid, openid=openid)
        return HttpResponseRedirect(BIND_CONFIRM_PATH)

    def _post_unbind(self, request):
        if not request.user.is_authenticated:
            return _error_page(request, "请先登录")
        _unbind_openid(user=request.user)
        return HttpResponseRedirect(BIND_CONFIRM_PATH)
