"""媒体文件存储（图片存储线，spec §6）。⛔ 只存**对象名**（key），不存 URL —— URL 是签出来的，落库等于把会过期的链接写死。⚠️ 关于内存：**读路径不整份驻留**——打包下载走 `open_stream`、**逐张**读（`screenshot_zip._read_all`），**订单截图单张的上界 `MAX_IMAGE_BYTES` = 18 MiB 是已登记的例外**（`screenshot_zip.py` 的模块 docstring 同样写明）。⚠️ 头像 / 收款码另有一条**更小**的上限 `MAX_USER_MEDIA_BYTES` = 10 MB（业务方裁定），它不参与打包下载那条读路径。⚠️ 起**反馈截图走同一条上限**（同一个对象名前缀，见 `USER_KEY_KINDS` 的说明）。
⚠️ **写路径不在这条约束内**：`put_bytes(data: bytes)` 本来就是**整份在内存**（本机 / COS 两个实现都如此）⇒ 别把这句读成「两个实现都不许把整份文件读进内存」。## 两个实现- `LocalStorage`（本机 + CI，**默认**）：写本地目录；`presign_*` 换成本后端自己的`/api/media/blob/<key>` 端点（`apps/core/media_blob_views.py`，**仅 local 模式登记**）。`head()` 的**内容类型从扩展名推**（`mimetypes`）——本地文件系统不存 Content-Type，而 `.jpg/.jpeg → image/jpeg`、`.png → image/png` 与上传时的取值一致；
⛔ 不写旁挂元数据文件（少一类残留物，也不会与对象命名空间撞名）。- `CosStorage`（生产）：腾讯云 COS，签名一律交给 SDK。`cos-python-sdk-v5` 的 import **放在 `CosStorage` 内部**：本机模式不装它也能跑测试（spec §6.1：跑测试不需要云凭据、不需要联网）。## 对象名里的随机串`build_order_key` / `build_user_key` 的 `random_part` 是**可省略**的（省略时内部`uuid.uuid4().hex[:12]`）：签发方（ticket 端点）一次调用就拿到 key，落库后原样使用，不必自己再造随机串；显式传入则是为了可复现的测试与「同一 key 重签」。两个函数都**不落库**——key 是要存进数据库的那一列（spec §5），所以形状必须稳定。"""
from __future__ import annotations

import mimetypes
import pathlib
import threading
import time
import uuid
from dataclasses import dataclass

from django.conf import settings

#: 允许的图片后缀（照对标实测值，spec §3.2）
ALLOWED_EXTS = ("jpg", "jpeg", "png")
APPROVAL_EVIDENCE_KIND = "approval-evidence"
MAX_APPROVAL_EVIDENCE_BYTES = 5 * 1024 * 1024
#: 服务端上限（前端分档压缩是体验，这里是边界）
MAX_IMAGE_BYTES = 18 * 1024 * 1024
#: 业务方裁定：头像与收款码 10 MB；订单截图仍是 18 MiB（对标值）。⚠️ 这条上限的**权威强制点**在认领端点（`profile_views.MyProfileMediaClaimView`，浏览器直传不经过我们服务器 ⇒ 签发端点拿不到文件大小）；本机直传路由（`media_blob_views`）按 **key 的种类**取上限做早反馈，判据见 `key_media_kind`。
MAX_USER_MEDIA_BYTES = 10 * 1024 * 1024

PRIVATE_FINANCE_EXPORT_PREFIX = "order-finance-exports"

#: 浏览器直传签入的缓存头（URL 稳定化线，业务方 2026-11-09 拍板）：对象从出生就带显式缓存语义。
#: ⚠️ COS 把签进签名的头当成「请求必须原样携带」⇒ 头名与值必须和直传方**逐字节一致**，
#: 否则 COS 判签名不符拒写（web 直传唯一出口 `frontend/src/layui-island/directUpload.ts` 带同一字面量，
#: 两侧一致性由 `directUpload.spec.ts` 的跨栈结构网钉住）。`presign_get` 不签头 ⇒ 读请求不受它约束。
MEDIA_CACHE_CONTROL = "public, max-age=86400"

#: 列表/大厅缩图参数（数据万象 CI，业务方 2026-11-09 拍板；生产桶已开通并实测 794KB→99KB）。
#: ⛔ `format/jpg` 必带：CI 默认重编码会把小图反而变大（实测 4.7KB→24KB）。CI 参数不参与
#: COS 签名 ⇒ 拼在预签名 URL 尾部即生效；本机 blob 端点忽略 query ⇒ 同样无害（不用替身）。
THUMBNAIL_QUERY = "imageMogr2/thumbnail/300x/format/jpg/rquality/80"

# 图标保留原格式（包括 PNG 透明通道），处理失败时回退原图。
GAME_LOGO_THUMBNAIL_QUERY = "imageMogr2/thumbnail/300x300%3E/ignore-error/1"

#: 读链接的**小时桶**缓存（URL 稳定化线，业务方 2026-11-09 拍板「同一小时同一 key 逐字节同 URL」）。
#: ⚠️ 只把过期时刻对齐整点**不够**：SDK 把**签发时刻**也算进签名（`qcloud_cos/cos_auth.py`
#: `q-sign-time = "<now-60>;<now+expire>"`）⇒ URL 仍随墙钟逐秒漂移。所以同桶内**复用第一次签名**：
#: 桶翻转（跨整点）整体清空、条数超限清空（防极端流量下无界增长）。
#: ⚠️ 进程级：gunicorn 多 worker 各持一份 ⇒ 跨 worker 不共享（同一 key 不同 worker 的 URL 不同，
#: 都在同一过期时刻前有效，浏览器缓存以「用户实际拿到的串」为准，稳定性按 worker 分摊）。
_STABLE_GET_URLS: dict[object, str] = {}
_STABLE_GET_URLS_BUCKET = -1
_STABLE_GET_URLS_LOCK = threading.Lock()
_STABLE_GET_URLS_MAX_ENTRIES = 10_000


def _stable_get_url(key: str | tuple[str, int], sign) -> tuple[str, int]:
    """同小时桶内返回**同一条** URL；桶首调 `sign(expired_seconds)` 现签并缓存。

    `sign` 由调用方闭包（里头才碰 SDK），桶翻转 / 超限时在锁内清空。
    返回 `(url, 剩余有效秒数)`：过期时刻恒为「本小时起点 + 7200」⇒ 有效期最短约 1 小时、最长 2 小时。
    """
    global _STABLE_GET_URLS_BUCKET
    now = int(time.time())
    bucket = now // 3600
    with _STABLE_GET_URLS_LOCK:
        if bucket != _STABLE_GET_URLS_BUCKET:
            _STABLE_GET_URLS_BUCKET = bucket
            _STABLE_GET_URLS.clear()
        else:
            cached = _STABLE_GET_URLS.get(key)
            if cached is not None:
                return cached, (bucket + 2) * 3600 - now
    url = sign((bucket + 2) * 3600 - now)
    with _STABLE_GET_URLS_LOCK:
        if bucket == _STABLE_GET_URLS_BUCKET:
            if len(_STABLE_GET_URLS) >= _STABLE_GET_URLS_MAX_ENTRIES:
                _STABLE_GET_URLS.clear()
            _STABLE_GET_URLS[key] = url
    return url, (bucket + 2) * 3600 - now


def _reset_stable_get_urls() -> None:
    """清空读链接缓存（**只给测试隔离用**，生产不调）。"""
    global _STABLE_GET_URLS_BUCKET
    with _STABLE_GET_URLS_LOCK:
        _STABLE_GET_URLS_BUCKET = -1
        _STABLE_GET_URLS.clear()


def append_thumbnail_query(url: str) -> str:
    """给预签名 URL 拼 `THUMBNAIL_QUERY`：已带 `?`（COS 签名串）用 `&`，否则用 `?`。

    空串原样返回——调用方「签不出回空串」的回落口径不被拼出一个指不动的参数串。
    """
    if not url:
        return url
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}{THUMBNAIL_QUERY}"


def append_game_logo_thumbnail_query(url: str) -> str:
    if not url:
        return url
    return f"{url}{'&' if '?' in url else '?'}{GAME_LOGO_THUMBNAIL_QUERY}"


@dataclass(frozen=True)
class Presigned:
    url: str
    expires_in: int


def build_order_key(tenant: str, order_no: str, ext: str, *, random_part: str | None = None,
                    kind: str = "") -> str:
    """对象名：`<租户>/orders/<订单号>/[<kind>.]<随机>.<ext>`（spec §6.2）。- `kind=""` ⇒ `<随机>.<ext>`（订单截图）- `kind="qr"` ⇒ `qr.<随机>.<ext>`（群聊二维码；
    spec §6.2 第三行）⚠️ `kind` 参数是**评审修复**加的：原实现两种 `img_type` 都走同一个随机串，与本文档声明的 `qr.` 形态不符（文档与实现不一致）。⚠️ **P4 起订单截图不再用本函数**（它按**租户**分段 ⇒ 链上各租户的单号不同、同一张单会散成好几套图）。
    订单截图改用 `build_shared_order_key`。⚠️ **D-0316起群聊二维码也不用本函数**（它同样迁到共享段，`kind="qr"`）；本函数今天只产出**历史对象名**（老截图 / 老群码——不迁移）与 `img_type` 未知时的fail-closed 兜底（`screenshot_views.issue_screenshot_ticket` 那个 `else` 支）。"""
    part = random_part or uuid.uuid4().hex[:12]
    prefix = f"{kind}." if kind else ""
    return f"{tenant}/orders/{order_no}/{prefix}{part}.{ext.lower()}"


#: 订单截图**共享**段的字面量（P4）。它是 `key_belongs_to_tenant` 那条例外的唯一钥匙，
#: ⛔ 改这里等于改安全边界——两处（本常量与护栏）必须同源，别在别处再抄一份字符串。
SHARED_ORDER_PREFIX = "shared-orders"


def build_shared_order_key(origin_order_no: str, ext: str, *, random_part: str | None = None,
                           kind: str = "") -> str:
    """链上**共享**的订单截图对象名：`shared-orders/<最初单号>/[<kind>.]<随机>.<ext>`。⚠️ **为什么按「最初单号」分段**：
    链上各租户的**本地**单号不同，只有 origin 是链上唯一的（`PeerOrderLink.origin_order_no`）⇒ 它才让「同一张单 = 同一套图、字节只存一份」成立（业务裁定）。
    ⚠️ `origin_order_no` **不许为空**：空值会产出 `shared-orders//abc.jpg`，把**所有**没有链身份的订单挤进同一段。调用方负责把 origin 解析成一个非空值（订单侧收口在`apps/business/media_access.py` 的 `order_shared_origin`）。"""
    part = random_part or uuid.uuid4().hex[:12]
    prefix = f"{kind}." if kind else ""
    return f"{SHARED_ORDER_PREFIX}/{origin_order_no}/{prefix}{part}.{ext.lower()}"


#: 允许的图片**魔数**（**真正的类型判据**：⛔ 不依赖 `Content-Type`，也不依赖扩展名）。
#: JPEG `FF D8 FF`；PNG `89 50 4E 47 0D 0A 1A 0A`。
IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)

#: 魔数判出来的 Content-Type → 对象名后缀（`IMAGE_MAGIC` 的反向索引，⛔ 不另抄一张表）。
#: 消费方：`sniff_image_bytes` 之后要拼对象名的上传端点（`platform_auto_reply_views`）、
#: 以及代练通发图线（`dailiantong_qr.EXT_BY_CONTENT_TYPE` 就是本常量的别名）。
IMAGE_EXT_BY_CONTENT_TYPE: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
}


def sniff_image_bytes(data: bytes) -> str | None:
    """按**字节魔数**判图种；认不出回 `None`（调用方据此拒收）。

    ⚠️ `data` 至少要覆盖最长那条魔数（PNG 8 字节）；更短的串一律认不出。
    """
    for magic, content_type in IMAGE_MAGIC:
        if data.startswith(magic):
            return content_type
    return None


def sniff_image_type(storage, key: str) -> str | None:
    """读对象**头几个字节**判断真实类型；认不出回 `None`（调用方据此拒收）。⚠️ 为什么必须有它（评审 Important 实测）：
    `Content-Type` 是**客户端可控**的，而本机实现的`head()` 内容类型还是从**扩展名**推的 ⇒ 只看那个字段等于没校验（把非图片命名成 `.jpg` 
    照样落库）。魔数读的是**字节本身**，两个后端都走 `open_stream`，判据一致。⚠️ 判据只有一份：魔数表与匹配逻辑在 `sniff_image_bytes`，本函数只负责「先从对象读头 8 字节」。"""
    with storage.open_stream(key) as fh:
        head = fh.read(8)
    return sniff_image_bytes(head)


#: `build_user_key` 允许的**种类段**（白名单，⛔ 不接受任意字符串——它会直接成为路径段）：`avatar` / `wechat-qr`（头像与微信收款码，图片存储线 spec §6.2）；`feedback`（问题反馈截图，反馈线 spec §1.3）；`home-qr`（后台首页二维码横幅，D-0262）；`article-media`（频道文章的封面 / 正文插图）。
#: ⚠️ **刻意复用本函数的 `<租户>/users/<用户 id>/` 前缀**，⛔ 不新造别的租户级前缀：`key_media_kind` 按第 2 段判种类，新前缀会让那类对象落进 `"order"` 档（本机直传的早反馈上限从 10 MB 变成 18 MiB），与认领侧的上限口径分叉。`article-media` 的上限口径与头像 / 反馈截图一致 = `MAX_USER_MEDIA_BYTES`（10 MB），权威强制点在认领端点（`apps/business/api/article_views.py::ArticleMediaClaimView`）。
#: ⚠️ 文章正文 / 封面里引用的 key **不限于当前用户**：同租户的另一个管理员可能编辑同一篇（图是前一个人传的）⇒ 写文章时的归属校验是「本租户 + `users/<任意数字>/article-media.`」、比认领那一跳宽一档；⛔ 别把两者混成同一条判据。
USER_KEY_KINDS = ("avatar", "wechat-qr", "feedback", "home-qr", "article-media", APPROVAL_EVIDENCE_KIND)


def build_user_key(
    tenant: str, user_id: int, kind: str, ext: str, *, random_part: str | None = None
) -> str:
    """用户媒体（头像 / 微信收款码 / 反馈截图 / 首页二维码）对象名：`<租户>/users/<用户 id>/<kind>.<随机>.<ext>`。

    ⚠️ `kind` **只接受 `USER_KEY_KINDS` 里的字面量**（断言，spec §6.2 的白名单口径）。
    """
    assert kind in USER_KEY_KINDS
    part = random_part or uuid.uuid4().hex[:12]
    return f"{tenant}/users/{user_id}/{kind}.{part}.{ext.lower()}"


#: 用户媒体对象名的**种类段**（按 `/` 切段后的第 2 段；订单截图那一段是字面量 `orders`）。
#: 判据见 `key_media_kind`。
USER_KEY_SEGMENT = "users"


def is_approval_evidence_key(key: str) -> bool:
    parts = [part for part in key.split("/") if part not in ("", ".")]
    return (
        len(parts) == 4
        and parts[1] == USER_KEY_SEGMENT
        and parts[3].startswith(f"{APPROVAL_EVIDENCE_KIND}.")
    )

#: 游戏 logo 对象名的**种类段**（按 `/` 切段后的第 2 段）。形状 `<租户>/games/<游戏 id>/logo.<随机>.<ext>`，见 `build_game_logo_key`。⚠️ 它**不在** `USER_KEY_KINDS` 里：logo 属于**游戏**（租户级配置）、不属于某个用户 ⇒ 不能借 `build_user_key` 的 `<租户>/users/<用户 id>/` 前缀（那会把「这张图属于谁传的」写进对象名，换个管理员维护就签不出来）。
GAME_KEY_SEGMENT = "games"


def build_game_logo_key(
    tenant: str, game_id: int, ext: str, *, random_part: str | None = None
) -> str:
    """游戏 logo 对象名：`<租户>/games/<游戏 id>/logo.<随机>.<ext>`。⚠️ `game_id` 进对象名是**故意的**：
    认领端点要求 key 落在**这一款游戏**的前缀下（`apps/business/api/game_logo_views.py::is_game_logo_key`）⇒ 拿别的游戏的 key 认领会被拒。
    租户判据仍由 `key_belongs_to_tenant` 收口（⛔ 不靠这里的字符串拼法）。⚠️ 与 `build_user_key` / `build_order_key` 一样**不落库**：key 是要存进`Game.logo_key` 的那一列，形状必须稳定、可复现（显式 `random_part` 供测试用）。"""
    part = random_part or uuid.uuid4().hex[:12]
    return f"{tenant}/{GAME_KEY_SEGMENT}/{game_id}/logo.{part}.{ext.lower()}"


#: 交易平台自动回复规则图片的对象名段。形状 `<租户>/platform-auto-replies/<随机>.<ext>`——它是**租户级配置**（不属于某个用户、也不属于某张单），既不能借 `build_user_key` 的 `<租户>/users/<用户 id>/` 前缀（换个人维护规则就签不出图），也不能落进订单截图的共享段（共享段是「同一张单、链上共用」的语义，规则图与订单无关）；租户前缀仍是跨租户隔离的唯一钥匙（`key_belongs_to_tenant`）。
PLATFORM_REPLY_KEY_SEGMENT = "platform-auto-replies"


def build_platform_reply_key(
    tenant: str, ext: str, *, random_part: str | None = None
) -> str:
    """规则图片对象名：`<租户>/platform-auto-replies/<随机>.<ext>`。⚠️ 与 `build_order_key` / `build_game_logo_key` 一样**不落库**：
    key 要存进`PlatformAutoReplyRule.image_key`，形状必须稳定、可复现（显式 `random_part` 供测试用）。
    ⚠️ `key_media_kind` 对本前缀回 `"order"`（18 MiB 早反馈上限）——那里只服务本机**直传路由**的早反馈；本前缀的权威闸是 multipart 上传端点自己的 5 MiB 判据（`platform_auto_reply_views`）。"""
    part = random_part or uuid.uuid4().hex[:12]
    return f"{tenant}/{PLATFORM_REPLY_KEY_SEGMENT}/{part}.{ext.lower()}"


def key_media_kind(key: str) -> str:
    """对象名的种类：`"user"`（头像 / 微信收款码）还是 `"order"`（订单截图）。**判据 = 按 `/` 切段、剔除空段与 `.` 之后**，两条按顺序判：1. 首段等于 `SHARED_ORDER_PREFIX` ⇒ 一律 `"order"`（共享订单截图前缀，见下）；2. 否则第 2 段恰好等于 `"users"` ⇒ `"user"`；其余 ⇒ `"order"`。第 2 段那条的两条读码依据是本文件里两个 builder 的**返回行**（即下面两条 f-string）：- `build_order_key`：`f"{tenant}/orders/{order_no}/{prefix}{part}.{ext}"`⇒ 第 2 段**恒为** `orders`；- `build_user_key`：
    `f"{tenant}/users/{user_id}/{kind}.{part}.{ext}"`⇒ 第 2 段**恒为** `users`。⚠️ **第 1 条（共享前缀）是 P4 整体评审补的（Minor 1）**：共享订单截图的形状是`shared-orders/<origin>/…`，而 `<origin>` 是对端可控的（`PeerOrderLink.origin_order_no`来自事件载荷）⇒ 只看第 2 段的话，对端把 origin 填成 `users` 就能把**本租户**这张单的本机直传上限从 18 MiB 静默压到 10 MB。共享前缀下第 2 段**永远不是种类段**，所以这里按**前缀**短路，而不是去限制 origin 的取值。⛔ 不许写成「key 里含 `users` 就 10 MB」那种子串判法：
    `demo/orders/users/1/a.jpg`第 3 段才是 `users`，而它是订单截图；`demo/usersx/1/a.jpg` 只是**以** `users` 开头。归一化方式与 `key_belongs_to_tenant` **同一套**（剔除空段与 `.`），免得一份 key 两套解析。⚠️ **认不出的形状一律回 `"order"`（更宽的那条 18 MiB）**，方向是刻意选的：订单截图的上限比用户媒体**大**，判错只会让形状畸形的 key 拿到更宽的上限，**不会**把18 MiB 的订单截图误拒（回归）。更宽的默认**不构成漏洞**——权威闸是认领端点：`profile_views.MyProfileMediaClaimView` 按 10 MB 重判、`screenshot_views` 按 18 MiB 重判，这条分类只决定「本机直传路由的早反馈」用哪个数。"""
    parts = [p for p in key.split("/") if p not in ("", ".")]
    if parts and parts[0] == SHARED_ORDER_PREFIX:
        # ⭐ 共享前缀的**第 2 段是 origin**（对端可控）⇒ 必须在看第 2 段**之前**短路成订单截图。
        return "order"
    if len(parts) > 1 and parts[1] == USER_KEY_SEGMENT:
        return "user"
    return "order"


def key_belongs_to_tenant(key: str, tenant: str, *, shared_holder=None) -> bool:
    """**归一化后**再判前缀（spec §10 护栏②的判据）。⛔ **不许**写成 `key.startswith(f"{tenant}/")`：评审实测 `demo/../victim/orders/x.jpg` 能过字符串判据（首段确实是 `demo/`）、解析后却落进 `victim/` 的命名空间 ⇒ 跨租户读写同时成立；判据：按 `/` 切段、剔除空段与 `.`、**任何 `..` 一律拒**，再要求首段 == tenant。
    `shared-orders/` 的**窄例外**（安全核心）：共享订单截图的对象名首段是 `shared-orders`（**不是**某个租户名），硬护栏会一律拒它，而业务上「同一张单的图链上各租户共用」正是要放行的——例外只有这一条、且**绑在持有关系上**：`shared_holder` 是**关键词参数**、默认 `None` ⇒ **没给判据 = 一律拒**（fail-closed）；给了判据时**由判据回答**「本租户是否持有这张单」、护栏只做 `bool(...)` 收口（它拿得到完整的 key，可按对象名精确查）；⛔ **不许**在这里 `return True`、也不许给 `shared_holder` 默认放行的可调用对象——那会把例外从「本租户持有这张单」放宽成「凡共享前缀一律放行」（跨租户读泄漏）。
    ⚠️ 归一化与 `..` 拒绝**先于**例外：`shared-orders/../victim/x.jpg` 即便判据点头也必须拒——共享前缀不是逃出本租户命名空间的通行证。⚠️ 判据的实现**不在这里**（`apps/core` ⛔ 不在模块顶层 import `apps.business` 的**模型**——那会引出反向依赖；准确口径见 `apps/business/media_access.py` 的模块 docstring）；调用方各自决定给不给判据：订单截图那条路径给（`screenshot_views` / `media_blob_views`），**用户媒体**（`profile_views`）**不给** ⇒ 头像 / 收款码对共享前缀仍是一律拒。
    """

    parts = [p for p in key.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return False
    if parts[0] == PRIVATE_FINANCE_EXPORT_PREFIX:
        return False
    if parts[0] == SHARED_ORDER_PREFIX:
        # ⭐ **唯一的例外**：除了「共享前缀」这一个条件，还必须**调用方给了持有判据、且判据点头**。
        return bool(shared_holder is not None and shared_holder(key))
    return parts[0] == tenant


class Storage:
    """五个方法，缺一不可（spec §6.1）+ `put_bytes` 写入口（本线新增）。

    ⛔ 不要在这里长更多公开方法：端点只认这几个，多一个就多一条没人守的契约。
    """

    def put_bytes(self, key: str, data: bytes, *, content_type: str = "") -> None:
        raise NotImplementedError

    def presign_put(self, key: str, *, expires_in: int = 600,
                    cache_control: bool = False) -> Presigned:
        raise NotImplementedError

    def presign_get(self, key: str, *, expires_in: int = 300,
                    cache_preview: bool = False) -> Presigned:
        raise NotImplementedError

    def head(self, key: str) -> tuple[int, str] | None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def open_stream(self, key: str):
        raise NotImplementedError


class LocalStorage(Storage):
    """本机 / CI：写本地目录。presign 换成**后端自己的**端点（见 media_blob_views）。"""

    def __init__(self, root: pathlib.Path):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> pathlib.Path:
        """解析对象名：**任何 `..` 段、以及解析后越出 root，都拒**（防目录穿越）。两条判据缺一不可、**只做②是漏的**：① **先**按 `/` 切段、任何一段是 `..` 一律拒——评审实测 `demo/../victim/orders/x.jpg` 解析后落在 `<root>/victim/orders/x.jpg`（**仍在 root 之下**），② 会放行它、而它已跨进另一个租户的命名空间；② 解析后用 `Path.is_relative_to(root)` 再兜一次（符号链接、绝对路径段等被①拦住）；⛔ 判据**不是** `str(p).startswith(str(root))`——那会把 `/data/media-evil/x` 当成 `/data/media` 的子路径放行（比字符串前缀、不比路径段）。
        `p == root` 也拒：`key=""` / `key="."` 会解析成 root 本身，留着它会让 `write_bytes` 直接对目录动手（`IsADirectoryError`）。
        """

        if any(seg == ".." for seg in key.split("/")):
            raise ValueError("非法对象名")
        p = (self.root / key).resolve()
        root = self.root.resolve()
        if p == root or not p.is_relative_to(root):
            raise ValueError("非法对象名")
        return p

    def put_bytes(self, key, data, *, content_type=""):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def presign_put(self, key, *, expires_in=600, cache_control=False):
        return Presigned(url=f"/api/media/blob/{key}", expires_in=expires_in)

    def presign_get(self, key, *, expires_in=300, cache_preview=False):
        return Presigned(url=f"/api/media/blob/{key}", expires_in=expires_in)

    def head(self, key):
        p = self._path(key)
        if not p.is_file():
            return None
        # ⚠️ 本机实现从**扩展名**推内容类型（`mimetypes`）——不写旁挂元数据文件，
        # 少一类残留物；`.jpg/.jpeg → image/jpeg`、`.png → image/png`，认不出则回空串。
        content_type = mimetypes.guess_type(str(p))[0] or ""
        return (p.stat().st_size, content_type)

    def delete(self, key):
        self._path(key).unlink(missing_ok=True)

    def open_stream(self, key):
        return self._path(key).open("rb")


class CosStorage(Storage):
    """生产：腾讯云 COS。签名一律交给 SDK（⛔ 不手搓签名）。"""

    def __init__(self):
        from qcloud_cos import CosConfig, CosS3Client  # 函数内导入：本机模式不装也能跑

        cfg = CosConfig(
            Region=settings.COS_REGION,
            SecretId=settings.COS_SECRET_ID,
            SecretKey=settings.COS_SECRET_KEY,
        )
        self.client = CosS3Client(cfg)
        self.bucket = settings.COS_BUCKET

    def put_bytes(self, key, data, *, content_type=""):
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type or "application/octet-stream",
        )

    def presign_put(self, key, *, expires_in=600, cache_control=False):
        """`cache_control=True` 时把 `MEDIA_CACHE_CONTROL` 签进签名（对象落桶即带缓存头）。

        ⚠️ 只给**直传方是本仓网页前端**的取票点开（`directUpload.ts` 带同值头）；头像与订单
        截图取票被**已装 App 包**（`app/utils/upload.uts` 只发 Content-Type）和机器客户端直传，
        签了必 403 ⇒ 恒不签（App 发新包后再开属后续拍板）。判据：`CacheControlWiringTests`。
        """
        headers = {"Headers": {"Cache-Control": MEDIA_CACHE_CONTROL}} if cache_control else {}
        url = self.client.get_presigned_url(
            Method="PUT", Bucket=self.bucket, Key=key, Expired=expires_in, **headers
        )
        return Presigned(url=url, expires_in=expires_in)

    def presign_get(self, key, *, expires_in=300, cache_preview=False):
        """签名按小时复用；预览私有缓存须重验证，COS 校验仍受签名期限约束。"""
        cache_key = (key, 1) if cache_preview else key
        params = {"response-cache-control": "private, no-cache"} if cache_preview else {}
        url, remaining = _stable_get_url(
            cache_key,
            lambda expired: self.client.get_presigned_url(
                Method="GET", Bucket=self.bucket, Key=key, Expired=expired,
                **({"Params": params} if cache_preview else {}),
            ),
        )
        return Presigned(url=url, expires_in=remaining)

    def head(self, key):
        from qcloud_cos.cos_exception import CosServiceError

        try:
            r = self.client.head_object(Bucket=self.bucket, Key=key)
        except CosServiceError as exc:
            # 404 = 对象不存在（不是错误）；其余（403 / NoSuchBucket / 签名错…）照旧响亮地抛。
            if exc.get_status_code() == 404:
                return None
            raise
        return (int(r["Content-Length"]), r.get("Content-Type", ""))

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def open_stream(self, key):
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].get_raw_stream()


def get_storage() -> Storage:
    backend = getattr(settings, "MEDIA_STORAGE_BACKEND", "local")
    if backend == "cos":
        return CosStorage()
    return LocalStorage(settings.MEDIA_ROOT)
