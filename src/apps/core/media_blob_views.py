"""**仅本机模式**存在的对象读写端点：本地存储实现的 presign 目标。生产（`MEDIA_STORAGE_BACKEND=cos`）下这个 URL **根本不登记**（见 `apps/core/urls.py`）⇒ 生产上不存在这条写入面——真正的上传是浏览器拿 COS **预签名 PUT** 直传、不经过 Django。租户前缀闸（AGENTS.md §4）：租户上下文用 `django.db.connection.schema_name`、**不用**请求参数（仓库里没有 brief 初稿的 `tenant_schema_name(request)` 占位名，本仓租户上下文一律来自认证态 / django-tenants 路由结果，同型取法见 `apps/core/auth_views.py` 与 `apps/core/plane.py`）；⛔ 绝不接受 `?tenant=` / 请求体里的租户参数；⛔ 判据是 `storage.key_belongs_to_tenant`（**归一化后**再判首段）、**不是**字符串 `key.startswith(f"{tenant}/")`——评审实测后者可被 `..` 绕过（见该函数 docstring），读写两侧**共用同一条**判据（`_belongs_to_current_tenant`）。⚠️ 订单截图的**共享前缀**（`shared-orders/…`）是这道闸**唯一的例外**、且绑在「**本租户确实持有这张单**」上（传 `shared_holder`），⛔ 不是「凡共享前缀一律放行」；用户媒体（头像 / 收款码）不给判据 ⇒ 对共享前缀仍一律拒。⚠️ **`GET` 也过这道闸**（本机所有租户共用同一个 `MEDIA_ROOT`，本机 URL **可猜**——没有签名没有时效；只挡写不挡读 ⇒ 租户 A 猜到 B 的对象名就能读走 B 的图；生产把「可读性」交给短时效签名 URL，本机没有签名，用前缀闸兜住同一件事，本机比生产**更严**、方向 fail-closed）；反方向的跨租户否定用例见 `tests/test_media_storage.py`。
⚠️ **「这个对象名在当前文件系统里写不了」一律翻成 403，⛔ 不许漏成 500**（AGENTS.md §5-E：领域层的**预期**失败必须翻译成业务响应，未预期的错误才响亮 500）。两道防线都会抛 `ValueError`：`key_belongs_to_tenant` 返回 False 走 403；`LocalStorage._path` 的 `..` / 越出 root 判据抛 `ValueError` ⇒ `except` 兜成 403。⚠️ **「预期」的边界如实写清**（原文「非法对象名一律 403、不漏 500」是**过度承诺**）：除 `ValueError` 外，`IsADirectoryError`（key 指着**已存在的目录**）与 `NotADirectoryError`（key 的某段前缀是一个**文件**）也翻成 403——都是路径形状问题、换个 key 就能成功；⛔ 但**不捕整个 `OSError`**：磁盘满（`ENOSPC`）、权限被拒（`EACCES`）这类基础设施故障仍应响亮 500；403 响应是**空体** ⇒ 不泄漏内部路径。
大小上限：**先看 `Content-Length`、再碰 `request.body`**——`request.body` 会把整个请求读进内存，超限直接 413、**不读体**；读完再按实际长度兜一次（ASGI 形态的第三道）；打包下载那条真正的大文件路径走 `open_stream`、与这里无关。⚠️ **`Content-Length` 缺失 / ≤ 0 一律拒收 413**：WSGI（生产形态 gunicorn）下没有声明值时 `WSGIRequest` 的 `LimitedStream` limit=0 ⇒ 体被**读成空**、`RequestDataTooBig` 永远不触发——修复前这种请求会 `put_bytes(key, b"")` 并回 **200**、磁盘上真的落一个 **0 字节对象**；空图片永远不合法 ⇒ 明确拒收、码取 **413**（与「超限」同一个码，本机路由既有口径；⛔ 不新造状态码）。⚠️ **上限按 key 的种类取**（业务方裁定）：头像 / 微信收款码 **10 MB**、订单截图仍是 **18 MiB**；判据 = `storage.key_media_kind(key)`（按 `/` 切段后的**第 2 段**：`users` ⇒ 用户媒体、其余 ⇒ 订单截图；读码依据与「为什么认不出时按更宽的订单上限」写在那个函数的 docstring 里）——⛔ 不要在这里重写一份子串判法，判错方向会把 18 MiB 的订单截图误拒（回归）。⚠️ 这条路由**不是**权威闸：它只有 key、没有文件大小以外的任何上下文，且（生产 `cos` 形态下）这条 URL **根本不登记**——真正的权威判据在认领端点（`profile_views.MyProfileMediaClaimView` 按 10 MB 重判、`screenshot_views` 按 18 MiB 重判）。
"""

from django.db import connection
from django.http import FileResponse, HttpResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core.storage import (
    MAX_IMAGE_BYTES,
    MAX_USER_MEDIA_BYTES,
    get_storage,
    is_approval_evidence_key,
    key_belongs_to_tenant,
    key_media_kind,
)


#: 「对象名指向的不是一个可写文件」这一类**预期**失败（模块 docstring 有实测复现形状）：`ValueError`（`..` / 越出 root / 空 key）、`IsADirectoryError`（key 指着已存在的目录）、`NotADirectoryError`（key 的某段前缀是个文件）。
#: ⚠️ 刻意**不**写 `OSError`：那会把磁盘满（`ENOSPC`）、权限被拒（`EACCES`）这类**基础设施**故障也吞成 403，让监控失明（AGENTS.md §5-E）。
_OBJECT_PATH_ERRORS = (ValueError, IsADirectoryError, NotADirectoryError)


def _max_bytes_for_key(key: str) -> int:
    """本机直传的**按 key 种类**上限（`:mod:`apps.core.storage` 的 `key_media_kind` 分类）。
    ⚠️ 两个上限都从**本模块的全局名**取（⛔ 不写死数字）：
    测试用`mock.patch("apps.core.media_blob_views.MAX_IMAGE_BYTES", …)` 把闸缩小到几个字节来判「超限不落盘」（E-0029：桩必须打在**被查找的那个模块**上）。"""
    if key_media_kind(key) == "user":
        return MAX_USER_MEDIA_BYTES
    return MAX_IMAGE_BYTES


def _declared_content_length(request) -> int:
    """`Content-Length` 头解析成 int；缺失或畸形按 `0` 处理。⚠️ 调用方对 `0` 的语义是「**没有可用的声明值 ⇒ 明确拒收 413**」（复评订正）：以前这里是「交给读入后的第二道判据」，而 WSGI 下无声明值时体被读成空 ⇒ 那道判据**永远不触发**，只会静默落一个 0 字节对象并回 200（见模块 docstring 的「大小上限」）。
    ⚠️ 与 `middleware._declared_content_length` **有意不同**：那道墙对 `0` 是「放行」（不拦分块）。⚠️ **不能写 `request.content_length`**：Django 5.2 的 `HttpRequest` / `WSGIRequest`**没有这个属性**（`django/http/request.py` 里搜不到；
    Django 只在自己的 `POST` 解析里直接读 `META["CONTENT_LENGTH"]`，见 `request.py:387`）。DRF 的 `Request.__getattr__`把未知属性转发给底层 request（`rest_framework/request.py:419-428`），底层也没有 ⇒运行期 `AttributeError` ⇒ 端点直接 500。brief 初稿/修订稿里那一行**实测跑不通**。"""
    try:
        return int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        return 0


class MediaBlobView(APIView):
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _belongs_to_current_tenant(key: str) -> bool:
        """读写两侧**同一条**判据（归一化后判首段，见 `key_belongs_to_tenant` 的 docstring）。⚠️ P4：
        订单截图落在**共享前缀** `shared-orders/<链上最初单号>/…` 下，而本机 /CI 的浏览器直传（PUT）与显示（GET）**都**走这个端点 ⇒ 这里也**必须**把「本租户确实持有这张单」的判据交给护栏，否则本机的订单截图**传不上去也显示不出来**（403）。⛔ 不传判据时护栏对共享前缀一律拒（fail-closed），那不是保守而是功能坏掉。
        ⚠️ 业务模块的 import **放在函数体内**（延迟）：这里要的判据必须落到**业务模型**（`OrderScreenshot`）上，而 `apps.core` **不在模块顶层** import `apps.business` 的模型、也不引「基座层依赖租户层模型」那条反向依赖（`apps.business` 是 `TENANT_APPS`-only 的租户层）⇒ 只能延迟到真正要用的时候再 import。⚠️ 这不是「两个 app 之间零 import」：`apps/core/authentication.py` 在模块级 import 过`apps.business.peer_sync` 的纯函数（不引模型）——那句话今天**已被证伪**，别写回去。判据本身对平台面 fail-closed（见那边 docstring）。"""
        from apps.business.media_access import order_screenshot_key_is_held

        if is_approval_evidence_key(key):
            return False
        return key_belongs_to_tenant(
            key, connection.schema_name, shared_holder=order_screenshot_key_is_held
        )

    def put(self, request, key):
        # ⚠️ **先看 Content-Length，再碰 `request.body`**（见模块 docstring）。
        # ⚠️ 上限**按 key 的种类**取（用户媒体 10 MB / 订单截图 18 MiB，见 `_max_bytes_for_key`）。
        limit = _max_bytes_for_key(key)
        declared = _declared_content_length(request)
        if declared <= 0:
            # 没有可用的声明值（缺失 / 畸形 / 0）⇒ **明确拒收**：WSGI 下这种请求的体会被读成空（见模块 docstring），修复前会落一个 0 字节对象并回 200；空图片永远不合法。码取 413——与「超限」同码（本机路由既有口径），⛔ 不新造状态码。⚠️ 这一支**先于**下面的限额判据，且**先于**读体。
            return HttpResponse(status=413)
        if declared > limit:
            return HttpResponse(status=413)
        body = request.body
        # ⚠️ 第三道（读完按**实际**长度兜）：真实流量里只有 ASGI 形态才可能触发——那里 `_stream` 是**可 seek** 的临时文件、体长不受 `Content-Length` 约束（`django/http/request.py` 的 `body` 属性自己写着「Content-Length can be absent or **understated**」）；WSGI 下 `_stream` 是 `LimitedStream` ⇒ 体长恒 ≤ 声明值。
        # 判据见 `tests/test_media_storage.py` 的 `test_put_understated_content_length_with_a_real_oversize_body_is_413`（用「声明值少报」的形状钉住它）。
        if len(body) > limit:
            return HttpResponse(status=413)
        # ⚠️ 本机实现也走"前缀必须是本租户"的同一道闸（与生产同一条判据）。
        # ⛔ 判据用 `key_belongs_to_tenant`（**归一化后**再判），⛔ 不是字符串 startswith。
        if not self._belongs_to_current_tenant(key):
            return HttpResponse(status=403)
        try:
            get_storage().put_bytes(key, body, content_type=request.content_type or "")
        except _OBJECT_PATH_ERRORS:
            # 领域层的**预期**失败（对象名指向的不是可写文件）必须翻成业务响应，
            # ⛔ 不许漏成 500（AGENTS.md §5-E）。空体 403 ⇒ 不泄漏内部路径。
            return HttpResponse(status=403)
        return HttpResponse(status=200)

    def get(self, request, key):
        # ⛔ 读侧与写侧同一条判据（评审实测：只把字符串 startswith 用在这里会被 `..` 绕过）
        if not self._belongs_to_current_tenant(key):
            return HttpResponse(status=403)
        st = get_storage()
        try:
            meta = st.head(key)
            if meta is None:
                return HttpResponse(status=404)
            _size, content_type = meta
            # 显式给 Content-Type：本机 `head` 从扩展名推得到（image/jpeg 等），
            # 浏览器 `<img src>` 才不会把图当附件下（默认落 application/octet-stream）。
            return FileResponse(
                st.open_stream(key), content_type=content_type or "application/octet-stream"
            )
        except _OBJECT_PATH_ERRORS:
            # 同写侧：对象名指向的不是可读文件是**预期**失败 ⇒ 403，不是 500。
            return HttpResponse(status=403)
