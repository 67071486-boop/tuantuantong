"""后台首页「二维码横幅」的端点（自造功能，决策 D-0262）：`POST /api/business/home/qr-codes/media/ticket/`（登录 + `home.qr_manage`）、`GET /api/business/home/qr-codes/`（**登录即可**，首页人人要渲染）、`POST` / `PUT` / `PATCH` / `DELETE`（登录 + `home.qr_manage`）。三条口径（照问题反馈 / 头像两条既有图片线的形状）：① **节点门写在 `get_permissions()` 的方法体里**、参数是模块常量——接线位点表（`apps/rbac/wiring.py` 的 `SITES["home.qr_manage"]`）的 T2 判据解析的就是那个方法体（含一层模块常量解引用），只写在类级 `permission_classes` 上它解析不到；② **读列表按持点分档**：持点的人拿**全部**行（含停用——管理面要能看见并重新启用），不持的只拿 `is_enabled=True` 的行，收口在 **queryset 层**（`get_queryset`），⛔ 不是前端过滤、⛔ 不是「先取全部再筛」（同 `feedback_views.FeedbackCollectionView` 口径）；③ **平面守卫在视图体里**（`on_public_plane()` ⇒ 404）——`public` 平面上没有 `business_homeqrcode` 表，放进去就是 500。
上传流（前端先直传、后端只认 key）：前端拿票据（`HomeQrTicketView`）→ 浏览器 PUT 字节 → `POST {kind, game_id, media_key}`；后端对 `media_key` 的五条校验收口在 `_media_key_error`：本租户（归一化判据）、`home-qr` 种类段、对象真实存在（`head`）、非 0 字节、魔数是图片。⛔ **校验失败不删对象**（与 feedback 的「清理」**刻意不同**）：feedback 清理的是提交者自己的截图（判据收到「本人」）；本端点不做任何对象删除——少一条删除口子、多留一个孤儿对象是可接受代价（同一取舍见 feedback 模块 docstring）。
"""

from django.db import connection
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.business.api.base import EnvelopeModelViewSet
from apps.business.models import Game, HomeQrCode
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.plane import on_public_plane, plane_not_found_response
from apps.core.storage import (
    ALLOWED_EXTS,
    MAX_USER_MEDIA_BYTES,
    USER_KEY_SEGMENT,
    build_user_key,
    get_storage,
    key_belongs_to_tenant,
    sniff_image_type,
)
from apps.rbac.permissions import has_node, has_node_now

#: 横幅管理端点的权限节点（**本仓第三条自有节点**，`benchmark_id = None`，D-0262）。
#: ⚠️ 接线位点表指向本模块的 `get_permissions`，T2 判据向下解析**一层**模块常量
#: ⇒ 这里改名要同批改 `apps/rbac/wiring.py`。
HOME_QR_MANAGE_NODE = "home.qr_manage"

#: 对象名里的种类段（`build_user_key(..., kind="home-qr", ...)` 产出 `home-qr.<随机>.<ext>`）。
HOME_QR_KEY_SEGMENT = "home-qr"

#: 校验失败文案（中文界面直接显示；与反馈 / 头像两条线的同位文案逐字一致）。
IMAGE_SIZE_LIMIT_MSG = "图片超过 10 MB"
IMAGE_TYPE_LIMIT_MSG = "只支持 jpg / jpeg / png"
FOREIGN_KEY_MSG = "二维码图片对象名不属于当前租户"
KEY_SHAPE_MSG = "二维码图片对象名格式不正确"
KEY_MISSING_MSG = "缺少二维码图片"
KEY_NOT_UPLOADED_MSG = "二维码图片还没上传成功"
IMAGE_EMPTY_MSG = "图片无效或为空"
#: `qq_bot` / `wechat_mp` 已存在再建时的拒绝文案（码 = `ErrorCode.CONFLICT`）。
SINGLETON_CONFLICT_MSG = "该类型的二维码已存在，请直接编辑它，不要重复添加"


def _tenant_schema() -> str:
    """租户标识 = **租户 schema 名**。

    ⛔ 只来自 `connection.schema_name`（请求期已由 django-tenants 的中间件设好），
    **绝不接受任何来自请求参数的租户标识**（`AGENTS.md` §4）。
    """
    return connection.schema_name


def is_home_qr_key(key: str, schema: str) -> bool:
    """这个对象名是不是**本租户**的首页二维码（种类段 = `home-qr`）。形状：`<租户>/users/<用户 id>/home-qr.<随机>.<ext>`，判据两条**缺一不可**：
    ① `key_belongs_to_tenant(key, schema)` —— **归一化**判据（按 `/` 切段、剔除空段与 `.`、任何 `..` 一律拒；⛔ 不能写成裸 `key.startswith(f"{schema}/")`：`demo/../victim/…` 的字符串前缀确实是 `demo/`，解析后却落在**别人的命名空间**里，见 `apps/core/storage.py::key_belongs_to_tenant` 的 docstring）；② 归一化后**恰好四段**、且逐段等于 `[本租户, "users", 任意 id, "home-qr." 开头]`。
    ⚠️ **为什么不绑「本人 id」**（与 feedback 的 `is_my_feedback_key` 不同）：feedback 绑本人是因为它的提交端点带**清理路径**（校验失败会删对象）⇒ 不绑就成了跨用户删除原语；本端点**没有任何对象删除**（见模块 docstring）⇒ 没有可被滥用的删除口子，「同租户任意管理员的 `home-qr` 对象」都允许挂上横幅（A 传完 B 接手维护是正常场景）。
    """
    if not key_belongs_to_tenant(key, schema):
        return False
    parts = [p for p in key.split("/") if p not in ("", ".")]
    return (
        len(parts) == 4
        and parts[0] == schema
        and parts[1] == USER_KEY_SEGMENT
        and parts[3].startswith(f"{HOME_QR_KEY_SEGMENT}.")
    )


def _media_key_error(storage, key: str, schema: str):
    """`media_key` 的五条校验。全过回 `None`；任一条不过回**拒绝响应**（或抛 403）。⛔ 校验里**一个字节都不写库**、**不删任何对象**（理由见模块 docstring）⇒ 调用方可以放心地把它放在任何写库动作之前。
    ⚠️ 前置：`key` 非空（「缺 key / 空 key」由序列化器的 `required` / `blank` 判，那是 1000 的入参形状；这里只判**这张 key 本身合不合法**）。
    """
    if not key_belongs_to_tenant(key, schema):
        # ⚠️ 跨租户的 key 是**越权**形状 ⇒ 403（同 feedback 的处置），不是 1000：
        #    「对象名不属于你」与「对象名写错了」是两件事，混成一句会让排障走错方向。
        raise PermissionDenied(FOREIGN_KEY_MSG)
    if not is_home_qr_key(key, schema):
        return api_response(code=ErrorCode.VALIDATION, msg=KEY_SHAPE_MSG)
    meta = storage.head(key)
    if meta is None:
        return api_response(code=ErrorCode.VALIDATION, msg=KEY_NOT_UPLOADED_MSG)
    # ⚠️ 只取**大小**；`head()` 给的内容类型**故意丢弃**——它客户端可控 / 本机按扩展名推，
    #    不可信（真正的类型判据是下面的魔数嗅探）。
    size, _header_content_type = meta
    if size <= 0:
        return api_response(code=ErrorCode.VALIDATION, msg=IMAGE_EMPTY_MSG)
    if size > MAX_USER_MEDIA_BYTES:
        return api_response(code=ErrorCode.VALIDATION, msg=IMAGE_SIZE_LIMIT_MSG)
    if sniff_image_type(storage, key) is None:
        return api_response(code=ErrorCode.VALIDATION, msg=IMAGE_TYPE_LIMIT_MSG)
    return None


class HomeQrCodeSerializer(serializers.ModelSerializer):
    """横幅行的对外形状。写入收 `game_id`（spec §1.4 的载荷形状）；读侧给 `game_name`（前端不用再拿游戏字典来映射——字典拉取失败时轮播的游戏名也不该空着）。`media_url` 是**现签**的短时效链接（`presign_get`），⛔ 不落库；`media_key` 才是持久事实（同 feedback 的 `images[].key/url` 双键口径）。
    """

    game_id = serializers.PrimaryKeyRelatedField(
        source="game",
        queryset=Game.objects.all(),
        required=False,
        allow_null=True,
        error_messages={
            "required": "请选择游戏",
            "does_not_exist": "游戏不存在或已被删除",
            "incorrect_type": "游戏标识格式不正确",
            "null": "游戏标识不能为 null",
        },
    )
    game_name = serializers.SerializerMethodField()
    media_url = serializers.SerializerMethodField()

    class Meta:
        model = HomeQrCode
        fields = [
            "id", "kind", "game_id", "game_name", "media_key", "media_url",
            "sort_order", "is_enabled", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["created_by", "created_at", "updated_at"]
        extra_kwargs = {
            "media_key": {
                "error_messages": {
                    "required": KEY_MISSING_MSG,
                    "blank": KEY_MISSING_MSG,
                    "null": KEY_MISSING_MSG,
                    "max_length": "二维码图片对象名太长",
                },
            },
            "kind": {
                # ⛔ **摘掉 DRF 按 `UniqueConstraint` 自动挂的 UniqueValidator**：那个验证器把「固定格已存在」报成**入参错误**（1000，「已存在。」），而它是一条**业务拒绝**（那张码已经在横幅上了）⇒ 判据收口在 `_reject_before_save` 的显式检查里、回 `ErrorCode.CONFLICT`（1001）；ChoiceField 的取值域校验**不受影响**（`validators` 只摘 unique 那一条）。
                "validators": [],
                "error_messages": {
                    "required": "请选择二维码种类",
                    "null": "请选择二维码种类",
                    "invalid_choice": "二维码种类不正确",
                },
            },
            "sort_order": {
                "min_value": 0,
                "error_messages": {
                    "invalid": "排序必须是数字",
                    "null": "排序不能为空",
                    "min_value": "排序不能是负数",
                },
            },
            "is_enabled": {
                "error_messages": {"invalid": "启停状态格式不正确"},
            },
        }

    def get_game_name(self, obj: HomeQrCode):
        return obj.game.name if obj.game_id is not None else None

    def get_media_url(self, obj: HomeQrCode):
        # 取链接要读存储层 ⇒ 失败时回落 None（列表不该因为对象存储抽风而整页失败，
        # 同 `apps/core/profile_views.py::_media_url` 的口径）。
        try:
            return get_storage().presign_get(obj.media_key, cache_preview=True).url
        except Exception:  # noqa: BLE001 —— 存储后端异常只降级这一格
            return None

    def validate(self, attrs):
        """`kind` × `game` 的配对判据（库层 `CheckConstraint` 的 API 层同批保险）。⚠️ **部分更新的取值来源**：PATCH 只带 `game_id` 时 `kind` 不在 `attrs` 里，判据要拿**实例上的现值**补齐——否则「群码换游戏」「群码只改排序」这类合法请求会被误判成「缺游戏」；`self.instance` 为空 = 新建。
        """
        kind = attrs.get("kind") or (self.instance.kind if self.instance is not None else None)
        game = attrs.get("game") if "game" in attrs else (
            self.instance.game if self.instance is not None else None
        )
        if kind == HomeQrCode.Kind.QQ_GROUP and game is None:
            raise serializers.ValidationError("QQ 群码必须选择游戏")
        if kind in HomeQrCode.SINGLETON_KINDS and game is not None:
            raise serializers.ValidationError("QQ 机器人 / 微信公众号二维码不关联游戏")
        return attrs


class HomeQrPlaneMixin:
    """平面守卫（`public` 平面一律 404）。视图体里调用（理由见模块 docstring）。"""

    def _plane_guard(self):
        if on_public_plane():
            return plane_not_found_response()
        return None


class HomeQrTicketSerializer(serializers.Serializer):
    """要一张上传凭证。`ext` 由前端给（后端再按 `storage.ALLOWED_EXTS` 校一次）。形状逐字照 `feedback_serializers.FeedbackTicketSerializer`（含 `max_length=8`）：空串**不在这里拒**，留给视图的 `ALLOWED_EXTS` 判据回「只支持 jpg / jpeg / png」。
    ⚠️ 请求体先过序列化器还有一个承重理由（AGENTS §5-E）：`request.data.get(...)` 直接读的话，body 是数组 / 字符串会 `AttributeError` ⇒ 500；过序列化器则是 1000。
    """

    ext = serializers.CharField(
        max_length=8,
        allow_blank=True,
        error_messages={
            "required": "缺少图片格式",
            "null": "缺少图片格式",
            "max_length": "只支持 jpg / jpeg / png",
            "invalid": "只支持 jpg / jpeg / png",
        },
    )


class HomeQrTicketView(HomeQrPlaneMixin, APIView):
    """`POST /api/business/home/qr-codes/media/ticket/` —— 签发一张只能写**本用户**对象的直传凭证。

    ⛔ **只签发、不写库**（同 `MyProfileMediaTicketView`）：签发了但没传完，
    横幅表里不该出现指向一个不存在对象的行。
    """

    permission_classes = [IsAuthenticated]
    NODE_DENIED_MESSAGE = "当前职位没有此操作所需的权限节点"

    def get_permissions(self):
        """登录 + 节点门（写在方法体里、参数取模块常量——wiring T2 的解析形状）。"""
        permissions = [IsAuthenticated()]
        if not on_public_plane():
            gate = has_node(HOME_QR_MANAGE_NODE)
            gate.message = self.NODE_DENIED_MESSAGE
            permissions.append(gate())
        return permissions

    def post(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        ser = HomeQrTicketSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ext = ser.validated_data["ext"].lower().lstrip(".")
        if ext not in ALLOWED_EXTS:
            return api_response(code=ErrorCode.VALIDATION, msg=IMAGE_TYPE_LIMIT_MSG)
        key = build_user_key(_tenant_schema(), request.user.pk, HOME_QR_KEY_SEGMENT, ext)
        signed = get_storage().presign_put(key, cache_control=True)
        return api_response({"key": key, "upload_url": signed.url, "expires_in": signed.expires_in})


class HomeQrCodeViewSet(HomeQrPlaneMixin, EnvelopeModelViewSet):
    """横幅行 CRUD。读 = 登录即可（按持点分档）；写 = `home.qr_manage`（含删除）。

    列表**不分页**：一个租户的行数上界 = 2（固定两格）+ 群码张数（一个游戏一张，
    与游戏档案同量级）⇒ 一次取全、`{items, total}` 形状（同 feedback 列表）。
    """

    queryset = HomeQrCode.objects.all()
    serializer_class = HomeQrCodeSerializer
    permission_classes = [IsAuthenticated]
    NODE_DENIED_MESSAGE = "当前职位没有此操作所需的权限节点"

    #: 逐 action 的节点 code 表。⚠️ 取值必须是**字符串字面量**（照 `SystemNoticeViewSet` 同一张表的口径）：`tests/test_rbac_wiring.py` 的 T2 解析锚点方法体 + **一层**类体常量、只收字符串常量——写成 `HOME_QR_MANAGE_NODE` 的引用会让位点被判假（引用链两层：`self.HOME_QR_PERMISSIONS` → `HOME_QR_MANAGE_NODE` → 字符串）。
    HOME_QR_PERMISSIONS = {
        "create": ("home.qr_manage",),
        "update": ("home.qr_manage",),
        "partial_update": ("home.qr_manage",),
        "destroy": ("home.qr_manage",),
    }

    def get_permissions(self):
        """与默认权限类取并集（形状照 `SystemNoticeViewSet.get_permissions`）。"""
        base = [permission() for permission in self.permission_classes]
        codes = self.HOME_QR_PERMISSIONS.get(self.action)
        if not codes:
            return base
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), has_node(*codes)(), *others]

    def get_queryset(self):
        """持点 ⇒ 全部行（管理面要能看见停用的）；不持 ⇒ 只看启用的行。⚠️ 收口在 **queryset 层**，⛔ 不是序列化后过滤（模块 docstring 第二条口径）。⚠️ `has_node_now` 对持「系统管理员」职位的人短路为真——前端 `canNode` 的 `is_tenant_admin`/`is_account_admin` 短路与它**同向**（照 `home.feedback` 先例，不另造口径）。
        """
        queryset = super().get_queryset()
        if has_node_now(self.request.user, HOME_QR_MANAGE_NODE):
            return queryset
        return queryset.filter(is_enabled=True)

    def list(self, request, *args, **kwargs):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        rows = list(self.get_queryset())
        serialized = self.get_serializer(rows, many=True).data
        return api_response({"items": serialized, "total": len(serialized)})

    def create(self, request, *args, **kwargs):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rejected = self._reject_before_save(serializer, exclude_pk=None)
        if rejected is not None:
            return rejected
        self.perform_create(serializer)
        # ⚠️ 信封由 `EnvelopeModelViewSet.finalize_response` 统一包（HTTP 也归一成 200），
        #    这里只回序列化数据（与 `super().create()` 的产物一致、少一次重复校验）。
        return Response(serializer.data)

    def update(self, request, *args, **kwargs):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        rejected = self._reject_before_save(serializer, exclude_pk=instance.pk)
        if rejected is not None:
            return rejected
        self.perform_update(serializer)
        return Response(serializer.data)

    def perform_create(self, serializer):
        """`created_by` 记**用户名快照**（同 `SystemNotice.created_by`：删账号不带走配置）。"""
        serializer.save(created_by=self.request.user.get_username())

    def destroy(self, request, *args, **kwargs):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        # 硬删（照 SystemNotice 先例，理由见模块 docstring）；存储里的对象不跟着删（孤儿对象）。
        return super().destroy(request, *args, **kwargs)

    # ── 写库前的两道**业务**判据（校验先行，⛔ 这里一个字节都还没写库）──────────────

    def _reject_before_save(self, serializer, *, exclude_pk):
        """`media_key` 五条校验 + 固定格**已存在**判据。该拒回响应，不该拒回 `None`。"""
        validated = serializer.validated_data
        # ⚠️ 只在**这次请求带了 `media_key`** 时才做媒体校验：PATCH `{is_enabled: false}` /
        #    `{sort_order: 2}` 不带它，那是「这一格不变」而不是「缺图片」。
        #    （新建与 PUT 全量更新时 `media_key` 是必填 ⇒ 必在 `validated_data` 里。）
        if "media_key" in validated:
            rejected = _media_key_error(
                get_storage(), validated["media_key"], _tenant_schema()
            )
            if rejected is not None:
                return rejected
        kind = validated.get("kind") or (
            serializer.instance.kind if serializer.instance is not None else None
        )
        if kind in HomeQrCode.SINGLETON_KINDS:
            existing = HomeQrCode.objects.filter(kind=kind)
            if exclude_pk is not None:
                existing = existing.exclude(pk=exclude_pk)
            if existing.exists():
                return api_response(code=ErrorCode.CONFLICT, msg=SINGLETON_CONFLICT_MSG)
        return None
