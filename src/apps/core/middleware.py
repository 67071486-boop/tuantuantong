"""会话守卫：把"这次凭据属于哪个平面"变成每个请求都强制的不变式。命中即**短路**：绝不调用 get_response。
理由见 spec §4.3——`AuthenticationMiddleware` 把身份包成 `SimpleLazyObject`（django/contrib/auth/middleware.py:40），而不是当场求值。
只要有**任何**先跑的代码读过一次 `request.user`，值就落进两份缓存：lazy 对象的 `_wrapped`（django/utils/functional.py:403-404）与 `request._cached_user`（django/contrib/auth/middleware.py:19-21）。此时 `session.flush()` 已经拦不住它——读 `request.user` 不会再碰会话，视图会以**对方的身份**跑完（实测 HTTP 200 + 对方数据）。所以只能在**进入视图之前**把请求整条掐掉。"""
# `_LEGACY_BODY_LIMIT` 就是 Django **出厂默认**的请求体墙——直接以 `... as` 形式从框架的出厂设置引入（`django/conf/global_settings.py`，≈2.5 MB）：⛔ 不手抄数字（手抄会跟丢框架的改动），⛔ 也**不读 `settings`**（本仓的 `settings.DATA_UPLOAD_MAX_MEMORY_SIZE` 已被抬到 20 MiB，读它取到的是**新姿态**、不是这里要的「旧姿态」）。
import re

from django.conf.global_settings import DATA_UPLOAD_MAX_MEMORY_SIZE as _LEGACY_BODY_LIMIT
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.http import HttpResponse

from apps.core.app_version import android_update_response, android_version_code, requires_android_update
from apps.core.envelope import error_response, rendered_response
from apps.core.plane import on_public_plane
from apps.core.storage import MAX_APPROVAL_EVIDENCE_BYTES, MAX_IMAGE_BYTES
from chat_service.image_limits import MAX_CHAT_MESSAGE_BODY, MAX_CHAT_PEER_BODY

REJECT_MESSAGE = "会话与当前租户不匹配"

#: 放行前缀：**本机模式**的直传路由（`apps/core/urls.py` 的 `path("api/media/blob/<path:key>")`，仅 `MEDIA_STORAGE_BACKEND=local` 时登记）。⚠️ 生产（`cos`）下那条 URL **根本不登记** ⇒ 这里的前缀放行在生产是**无害的空操作**（请求会走到 404，连体都不会读）。
_DIRECT_UPLOAD_PATH_PREFIX = "/api/media/blob/"
_APPROVAL_UPLOAD_PREFIX = "/api/business/approvals/media/upload/"
_SCREENSHOT_BROWSER_UPLOAD_PREFIX = "/api/business/screenshot-browser/blob/"
_CHAT_MESSAGE_UPLOAD_PATH = re.compile(
    r"/api/internal-chat/(?:(?:visitor/(?:guest|desk)/conversations|conversations|"
    r"(?:app/)?(?:order-groups|notification-conversations))/[0-9]{1,19}|"
    r"orders/[^/\x00-\x1f\x7f]+)/messages/"
)
_CHAT_PEER_PATH = "/api/internal-chat/service/peer-chat/"
_PEER_INBOUND_PATH = "/api/business/peer-sync/inbound/events/"


def _chat_upload_limit(request):
    if request.method != "POST" or request.content_type != "application/json":
        return None
    path = request.path_info
    if path == _CHAT_PEER_PATH:
        signature = request.headers.get("X-TTT-Chat-Signature", "")
        if re.fullmatch(r"[0-9a-f]{64}", signature):
            return MAX_CHAT_PEER_BODY
    if path == _PEER_INBOUND_PATH and request.headers.get("Authorization", "").startswith("Bearer "):
        return MAX_CHAT_PEER_BODY
    if _CHAT_MESSAGE_UPLOAD_PATH.fullmatch(path):
        return MAX_CHAT_MESSAGE_BODY


def _declared_content_length(request) -> int:
    """`Content-Length` 头解析成 int；**缺失或畸形一律按 `0`（= 不拦）**。⚠️ 语义与 `media_blob_views._declared_content_length` **有意不同**：
    那边 `0` 的意思是「声明值不可用 ⇒ **明确拒收 413**」（空图片永远不合法），这里 `0` 的意思是「**没有声明值 ⇒ 放行**」——这里刻意不对分块传输自作主张（残余写进 `config/settings.py` ④与 `E-0095`）
    。⚠️ 不写 `request.content_length`：Django 的 `HttpRequest` / `WSGIRequest` **没有**这个属性（实测记录在 `media_blob_views._declared_content_length` 的 docstring 里）。"""
    try:
        return int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        return 0


class NonDirectUploadBodyLimitMiddleware:
    """把**非直传路径**的请求体墙恢复到 Django 出厂默认（2.5 MB）⇒ 超出回 **400**。**为什么需要**（E-0095）：`DATA_UPLOAD_MAX_MEMORY_SIZE` 是**全局**设置、没有 per-view 形式；为放行本机直传（`PUT /api/media/blob/<key>` 要读 `request.body`）把它抬到 20 MiB 时，**未认证**的 `POST /api/auth/login` 等**每个**读体端点也被一起抬高了 ⇒ 生产（nginx 本来就是 `client_max_body_size 20m`）同样从「廉价 400」变成「整份进内存 +`json.loads`」，而本仓没有节流。这道中间件只把**直传那一条前缀**留在 20 MiB 上。**位置**：紧跟在 `TenantMainMiddleware` **之后**、其余中间件之前（`config/settings.py` 的`MIDDLEWARE`）。理由：① 它只读 `request.META["CONTENT_LENGTH"]` 与 `request.path_info` ⇒ 放在**最前面**也读得到 header，但排在租户解析**之后**有一个具体好处：**未知域名**仍由租户层给原有的 404（复评实测：
    3 MiB + 不存在的域名 ⇒ 404），不会被这里抢先改成 400（改动面更小）。⚠️ **停用租户不是这样**（复评实测）：`TenantSessionGuardMiddleware` 的 401 排在本墙**之后**⇒ **小体**请求才轮得到那个 401，**大体**请求会先被本墙拦成 **400 空体**——两者都是拒绝，只是形状不同。⛔ 不要为此把本墙挪到守卫**之后**：那会退掉下面 ③「不落 session / CSRF /认证 / DB 工作」这条理由；② 它**不碰** `request.tenant` / `connection.schema_name` ⇒ 对租户解析没有任何影响；③ 排在 Session / CSRF / Authentication **之前** ⇒ 超大体的请求不落任何 session、CSRF、认证与 DB 工作（这正是「恢复到旧姿态」：旧行为下这些工作会做完，只在视图读体时炸 400）；④ ⛔ 不能再往后：`CommonMiddleware` 与视图都可能读体。**残余**：分块传输（无 `Content-Length`）**不拦**。⚠️ **真实机制**（复评实测 +读框架源码）：
    **WSGI（本仓生产形态，gunicorn）**下无 `Content-Length` ⇒ `WSGIRequest` 的`LimitedStream` limit=0（`django/core/handlers/wsgi.py`）⇒ 体被**读成空**、`RequestDataTooBig` **永远不会触发**（`django/http/request.py` 的「按真实长度再判」只在**ASGI** 的可 seek 流上成立）⇒ 残余形状是「**静默空体**」，**不是**「仍只受 20 MiB 墙约束」。见 `config/settings.py` ④ 的如实登记。⚠️ **路径形状变体**：本墙**先于路由** ⇒ 未命中豁免前缀的畸形路径（`/api/media/blob` 少尾斜杠、`/API/MEDIA/BLOB/…` 大小写不同、`//api/media/blob/…`）在**大体**下会由 404 变 **400**；命中前缀的 `/api/media/blob/` 仍是 404→404。这是 fail-closed、不是洞。判据：`tests/test_media_storage.py::BodySizeWallTests`（五条：非直传 3 MiB ⇒ **400**；同一请求**只去掉本中间件** ⇒ 变成 **401**（体真的进了登录视图）；本机直传 3 MiB ⇒ **200**且真的落盘；小的正常请求不受影响；**带 `SCRIPT_NAME` 的直传** ⇒ 仍 **200**（钉住`request.path_info` 这条判据）。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # ⚠️ 用 `request.path_info`（= 去掉 `SCRIPT_NAME` 后的部分），⛔ **不用 `request.path`**：后者是 `SCRIPT_NAME + path_info`（`django/core/handlers/wsgi.py`）⇒ 部署在子路径（`SCRIPT_NAME=/tt`）时 `/tt/api/media/blob/…` 不再命中豁免前缀、本机大体直传会被 400。今天 `SCRIPT_NAME` 为空、无实害，但这是正确的判据。
        if request.path_info.startswith(_DIRECT_UPLOAD_PATH_PREFIX):
            return self.get_response(request)
        declared = _declared_content_length(request)
        if request.method == "PUT" and request.path_info.startswith(_APPROVAL_UPLOAD_PREFIX):
            ticket = request.path_info.removeprefix(_APPROVAL_UPLOAD_PREFIX).removesuffix("/")
            if ticket and "/" not in ticket and request.path_info.endswith("/"):
                if declared > MAX_APPROVAL_EVIDENCE_BYTES:
                    return HttpResponse(status=413)
                return self.get_response(request)
        if request.method == "PUT" and request.path_info.startswith(_SCREENSHOT_BROWSER_UPLOAD_PREFIX):
            screenshot_id = request.path_info.removeprefix(_SCREENSHOT_BROWSER_UPLOAD_PREFIX)
            if screenshot_id.isascii() and screenshot_id.isdecimal():
                if declared > MAX_IMAGE_BYTES:
                    return HttpResponse(status=413)
                return self.get_response(request)
        if (chat_limit := _chat_upload_limit(request)) is not None:
            if declared > chat_limit:
                return HttpResponse(status=413)
            return self.get_response(request)
        if declared > _LEGACY_BODY_LIMIT:
            # 空体 400：与 Django 因 `RequestDataTooBig` 给出的 400 同姿态（不渲染信封，
            # ⛔ 这里也不该读体 / 进 DRF）。
            return HttpResponse(status=400)
        return self.get_response(request)


class AndroidAppVersionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.method != "OPTIONS"
            and request.path_info.startswith("/api/app/")
            and requires_android_update(android_version_code(request))
            and not on_public_plane()
        ):
            return rendered_response(android_update_response())
        return self.get_response(request)


class TenantSessionGuardMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = getattr(request, "tenant", None)
        if tenant is not None and not tenant.is_active:
            return self._reject(request)
        session = getattr(request, "session", None)
        if session is not None:
            plane = session.get("identity_plane")     # 缺失 => None => 拒绝
            if "_auth_user_id" in session and plane != connection.schema_name:
                return self._reject(request)
        return self.get_response(request)

    @staticmethod
    def _reject(request):
        session = getattr(request, "session", None)
        if session is not None:
            session.flush()
        request.user = AnonymousUser()
        # 两处都要清：`AuthenticationMiddleware` 存的是 `SimpleLazyObject(lambda: get_user(request))`，而模块级 `get_user` 会把结果缓存进 `request._cached_user`——只换 `request.user` 不够，任何后来直接调 `get_user(request)` 的代码（或重新包一层 lazy 的代码）都会从这份缓存里把**对方**捡回来。
        request._cached_user = AnonymousUser()
        # 中间件短路返回的响应**不走** Django 的 template-response 阶段（`BaseHandler._get_response` 只替视图响应调 `render()`）、也不经过 DRF 的 `finalize_response`（渲染三件套是 DRF 在那里补的）。交给 `envelope.rendered_response`（与 `CSRF_FAILURE_VIEW` 共用同一份）：不渲染的话 `error_response` 的 `Response` 停在未渲染态，而外层 `CommonMiddleware.process_response` 每个响应都要读 `len(response.content)` ⇒ 一个本意 401 的短路会变成 500（实测过）；各写一份则等于又开一条绕过唯一信封来源的路。
        return rendered_response(error_response(401, REJECT_MESSAGE, 401))
