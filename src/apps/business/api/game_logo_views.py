"""游戏 logo 的两个上传端点：`POST /api/business/games/<id>/logo/ticket` 与 `POST …/logo`（门都是登录 + 节点 `game.add_edit`）。`PUT <upload_url>` 是客户端直传（本机走 `/api/media/blob/<key>`，生产走 COS 预签名），**不经过**这两个视图。三条口径（照头像 / 首页二维码两条既有图片线的形状）：① **三步流**：取票（只签发、**不写库**）→ 客户端直传原始字节 → 认领（校验后写 `Game.logo_key`）；⛔ 不另造传输方式：key 构造走 `storage.build_game_logo_key`、签名走 `storage.presign_put`、读链接走 `storage.presign_get`。② **节点门写 `get_permissions()` 的方法体里**、参数是模块常量（照 `HomeQrTicketView.get_permissions`）；⛔ 不新开权限节点：logo 是「编辑游戏」的一部分 ⇒ 复用 `game.add_edit`（与 `GameViewSet.GAME_PERMISSIONS` 的 create / update 同一个点）。③ **平面守卫在视图体里**（`on_public_plane()` ⇒ 404）：`public` 平面上没有 `business_game` 表，放进去就是 500。
认领时的校验（顺序固定）：① 游戏必须**没被软删**（与 `GET /api/business/games/` 列表口径同一条），已删 / 不存在的游戏一律「记录不存在」（HTTP 200 + code 1002）；② `key_belongs_to_tenant` **归一化**判据（⛔ 不是裸 `startswith`：`demo/../victim/…` 解析后落在别人的命名空间里，见 `apps/core/storage.py::key_belongs_to_tenant` 的 docstring），跨租户 key ⇒ **403**（越权形状，不是入参格式错）；③ key 必须落在**这一款游戏**的 logo 形状上（`<本租户>/games/<id>/logo.<随机>.<ext>`）⇒ 别游戏的 key 或别的种类段一律 1000——拿 A 游戏 key 认领 B 游戏会把 B 的图标换成 A 的图，是**静默的数据错位**，所以这条要用形状判、不只判租户；④ 对象存在（`head`）→ 非 0 字节 → ≤ **10 MB**（`MAX_USER_MEDIA_BYTES`，业务方裁定的头像口径；⛔ 不是订单截图的 18 MiB）→ 魔数 jpg / png（`sniff_image_type`，⛔ 不信 `Content-Type` 与扩展名）；任何一条不过 ⇒ **删掉刚传上来的对象**再回拒绝（照 `MyProfileMediaClaimView` 的清理口径；删的**只可能是**第③条放行过的本租户本游戏对象 ⇒ 不是跨租户删除原语）。
⚠️ **本机直传路由的早反馈上限与这里不同，是已知的、不影响判据的分叉**：`apps/core/media_blob_views._max_bytes_for_key` 按 `key_media_kind` 取数，logo key 第 2 段是 `games`（不是 `users`）⇒ 它拿到**订单截图那条 18 MiB**；权威闸在这里（认领端）：10 MB 以上的图会被拒并删掉。⛔ 不为这个分叉去改 `key_media_kind`（「认不出形状一律回 order」是刻意方向，改它要连带改共享订单截图那条安全判据）。写库与留痕：`logo_key` 的写入与 `record(...)` 收在**同一个** `transaction.atomic()` 里（spec §7.4：不允许「图换了、日志没有」），旧对象在**写库成功之后**才删（反的话写库失败会留下「列指向已删对象」），同 key 重提交 = 什么都没发生 ⇒ 不写第二行留痕（R27.2 既有口径）。读：`GET /api/business/games/` 每行由 `GameSerializer.logo_url` 现签短时效读链接，**空 key 回空串**（⛔ 不是 `null`——网页端与打手 App 按空串回落「首字 + 色块」）；本机 `presign_get` 是**相对路径**（要登录态）、生产 COS 是**绝对签名 URL**——照现状，⛔ 不为它改存储层。
"""
from django.db import connection, transaction
from django.http import Http404
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.models import Game
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.plane import on_public_plane, plane_not_found_response
from apps.core.storage import (
    ALLOWED_EXTS,
    GAME_KEY_SEGMENT,
    MAX_USER_MEDIA_BYTES,
    build_game_logo_key,
    get_storage,
    key_belongs_to_tenant,
    sniff_image_type,
)
from apps.rbac.permissions import has_node

#: 游戏管理节点（`apps/rbac/catalog.py` 的 `game.add_edit`，**不新开节点**）。
#: 与 `GameViewSet.GAME_PERMISSIONS` 的 create / update / partial_update 同一个点。
GAME_LOGO_NODE = "game.add_edit"

#: `Game.logo_key` 的列宽（认领入参也按它限长，避免超长 key 拖到写库才报错）。
GAME_LOGO_KEY_MAX_LENGTH = 255

#: 写进 `Game.logo_key` 的**种类段**（形状判据用它，与 `build_game_logo_key` 同源常量）。
GAME_LOGO_KEY_NAME_PREFIX = "logo."

#: 拒绝文案（中文界面直接显示；与头像 / 首页二维码两条线的同位文案一致）。
IMAGE_SIZE_LIMIT_MSG = "图片超过 10 MB"
IMAGE_TYPE_LIMIT_MSG = "只支持 jpg / jpeg / png"
FOREIGN_KEY_MSG = "图片对象名不属于当前租户"
KEY_SHAPE_MSG = "图片对象名格式不正确"
KEY_MISSING_MSG = "缺少图片"
KEY_NOT_UPLOADED_MSG = "图片还没上传成功"
IMAGE_EMPTY_MSG = "图片无效或为空"


def _tenant_schema() -> str:
    """租户标识 = **租户 schema 名**（⛔ 不接受请求参数传入的租户标识，`AGENTS.md` §4）。"""
    return connection.schema_name


def _game_for_upload(pk: int) -> Game:
    """取**没被软删**的游戏；不存在 / 已删一律 Http404（信封翻成 200 + code 1002）。

    ⚠️ 判据与 `GET /api/business/games/` 的列表口径同一条（`deleted_at__isnull=True`）：
    已下架的游戏在列表里已经看不见，仍能给它换图标只会留下一张没人看得到的图。
    """
    game = Game.objects.filter(pk=pk, deleted_at__isnull=True).first()
    if game is None:
        raise Http404
    return game


def is_game_logo_key(key: str, schema: str, game_id: int) -> bool:
    """这个对象名是不是**本租户 · 这一款游戏 · logo 种类**的形状。形状：`<租户>/games/<游戏 id>/logo.<随机>.<ext>`，判据两条**缺一不可**：
    ① `key_belongs_to_tenant(key, schema)` —— 归一化判据（含 `..` 拒绝）；② 归一化后**恰好四段**、且逐段等于 `[本租户, "games", 本游戏 id, "logo." 开头]`。⚠️ ②里判「本游戏 id」不是防御性写法：A 游戏的 key 拿来认领 B 游戏会让 B 的图标静默变成 A 的图，判租户判不出这种错位。
    """
    if not key_belongs_to_tenant(key, schema):
        return False
    parts = [p for p in key.split("/") if p not in ("", ".")]
    return (
        len(parts) == 4
        and parts[0] == schema
        and parts[1] == GAME_KEY_SEGMENT
        and parts[2] == str(game_id)
        and parts[3].startswith(GAME_LOGO_KEY_NAME_PREFIX)
    )


class GameLogoTicketSerializer(serializers.Serializer):
    """要一张上传凭证。`ext` 空串**不在这里拒**，留给视图按 `ALLOWED_EXTS` 回同一句话。⚠️ 请求体先过序列化器是承重写法（AGENTS.md §5-E）：直接 `request.data.get(...)` 时，body 是数组 / 字符串会 `AttributeError` ⇒ 500；过序列化器则是 1000。形状逐字照 `feedback_serializers.FeedbackTicketSerializer`（含 `max_length=8`）。
    """

    ext = serializers.CharField(
        max_length=8,
        allow_blank=True,
        error_messages={
            "required": "缺少图片格式",
            "null": "缺少图片格式",
            "max_length": IMAGE_TYPE_LIMIT_MSG,
            "invalid": IMAGE_TYPE_LIMIT_MSG,
        },
    )


class GameLogoClaimSerializer(serializers.Serializer):
    """认领：只收一个对象名。长度与 `Game.logo_key` 的列宽同源。"""

    key = serializers.CharField(
        max_length=GAME_LOGO_KEY_MAX_LENGTH,
        error_messages={
            "required": KEY_MISSING_MSG,
            "blank": KEY_MISSING_MSG,
            "null": KEY_MISSING_MSG,
            "max_length": "图片对象名太长",
        },
    )


class _GameLogoBase(APIView):
    """两个端点的公共装配：登录 + 节点门（方法体里）+ 平面守卫（视图体里）。"""

    permission_classes = [IsAuthenticated]
    #: 节点门拒人时的 403 文案（照 `GameViewSet` / `HomeQrTicketView` 的同位文案）。
    NODE_DENIED_MESSAGE = "当前职位没有此操作所需的权限节点"

    def get_permissions(self):
        """登录 + `game.add_edit`；与默认权限类**取并集**（不替换）。⚠️ 参数取模块常量（`HomeQrTicketView.get_permissions` 的同款形状）。⚠️ public 平面上**不加节点门**：视图体会先回 404（`on_public_plane`），而 `has_node` 在 public 恒假 ⇒ 加了只会让 404 变成 403，形状与既有几条线不一致。
        """
        permissions = [IsAuthenticated()]
        if not on_public_plane():
            gate = has_node(GAME_LOGO_NODE)
            gate.message = self.NODE_DENIED_MESSAGE
            permissions.append(gate())
        return permissions

    def _plane_guard(self):
        if on_public_plane():
            return plane_not_found_response()
        return None


class GameLogoTicketView(_GameLogoBase):
    """`POST /api/business/games/<id>/logo/ticket` —— 签发一张只能写**本租户本游戏**对象的凭证。

    ⛔ **只签发、不写库**（同 `MyProfileMediaTicketView`）：签发了但没传完，
    `Game.logo_key` 不该指向一个不存在的对象。
    """

    def post(self, request, pk):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        game = _game_for_upload(pk)
        serializer = GameLogoTicketSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ext = serializer.validated_data["ext"].lower().lstrip(".")
        if ext not in ALLOWED_EXTS:
            return api_response(code=ErrorCode.VALIDATION, msg=IMAGE_TYPE_LIMIT_MSG)
        key = build_game_logo_key(_tenant_schema(), game.pk, ext)
        signed = get_storage().presign_put(key, cache_control=True)
        return api_response(
            {"key": key, "upload_url": signed.url, "expires_in": signed.expires_in}
        )


class GameLogoClaimView(_GameLogoBase):
    """`POST /api/business/games/<id>/logo` —— 校验后写 `logo_key` 并留痕。"""

    def post(self, request, pk):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        game = _game_for_upload(pk)
        serializer = GameLogoClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        key = serializer.validated_data["key"]
        schema = _tenant_schema()

        # 护栏①：跨租户的 key 是**越权**形状 ⇒ 403（不是 1000：两者是不同的事）。
        if not key_belongs_to_tenant(key, schema):
            raise PermissionDenied(FOREIGN_KEY_MSG)
        # 护栏②：必须是**这一款游戏**的 logo 形状（租户判据判不出「A 游戏换到 B 游戏」）。
        if not is_game_logo_key(key, schema, game.pk):
            return api_response(code=ErrorCode.VALIDATION, msg=KEY_SHAPE_MSG)

        storage = get_storage()
        meta = storage.head(key)
        if meta is None:
            return api_response(code=ErrorCode.VALIDATION, msg=KEY_NOT_UPLOADED_MSG)
        # ⚠️ 只取**大小**；`head()` 给的内容类型故意丢弃——客户端可控 / 本机按扩展名推，
        #    不可信（真正的类型判据是下面的魔数嗅探）。
        size, _header_content_type = meta
        if size <= 0:
            storage.delete(key)
            return api_response(code=ErrorCode.VALIDATION, msg=IMAGE_EMPTY_MSG)
        if size > MAX_USER_MEDIA_BYTES:
            storage.delete(key)
            return api_response(code=ErrorCode.VALIDATION, msg=IMAGE_SIZE_LIMIT_MSG)
        if sniff_image_type(storage, key) is None:
            storage.delete(key)
            return api_response(code=ErrorCode.VALIDATION, msg=IMAGE_TYPE_LIMIT_MSG)

        old_key = game.logo_key or ""
        # 同 key 重提交 = 什么都没发生（R27.2）：不写第二行留痕、也不重复删对象。
        if old_key == key:
            return api_response({"key": key})

        with transaction.atomic():
            game.logo_key = key
            game.save(update_fields=["logo_key"])
            record(
                log_type=ACTION_LOG_TYPES["system.game_update"],
                action="system.game_update",
                actor=request.user.get_username(),
                actor_id=request.user.pk,
                target_kind="game",
                target_id=game.pk,
                target_name=game.name,
            )

        # ⚠️ 旧对象在**写库成功之后**才删（反过来的话，写库失败会留下「列指向已删对象」）。
        if old_key and old_key != key:
            try:
                storage.delete(old_key)
            except Exception:  # noqa: BLE001 —— 旧对象删不掉不该让本次上传失败
                pass
        return api_response({"key": key})
