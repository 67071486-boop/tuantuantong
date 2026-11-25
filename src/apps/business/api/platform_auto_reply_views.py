"""交易平台自动回复规则：CRUD。端点：`GET`/`POST /api/business/platform-auto-replies/`（列表数组不分页 / 新增）、`GET`/`PATCH`/`DELETE …/<id>/`（单行 / 修改〔页面「修改」/ 行内启用开关 / 批量改内容都用它〕/ 删除）、`POST …/upload-image/`（**规则图片上传**，multipart）。⚠️ **不暴露 `PUT`**：页面只用 `PATCH`（同 `PlatformIntegrationViewSet` 先例），没有消费方就不给这条能力面。读侧字段口径：**`image_key` 不下发原值**（`write_only`——对象名是内部命名空间 `<租户>/…` 的一部分，前端拿它没用；读侧给 `has_image` 布尔与 `image_url` **现签**短时效链接，⛔ 不落库不缓存）；`game_name` 为空串时回 **`""`**（= 全部游戏），⛔ 不在后端编「全部游戏」这句——那是页面文案，一处口径一个地方；`trigger_name` / `platform_label` 走 `get_*_display()`（码值与文案唯一出处是模型，逐字照证据件，不维护第二份码表）。
`image_key` 的租户护栏（**安全判据，⛔ 不要删**）：生产是**共享 COS 桶**，`presign_get` 拿到对象名就签出可读链接、**桶本身不区分租户** ⇒「谁持有本节点谁就能给别人的对象签链接」完全成立（本机 local 后端只是被 `media_blob_views` 的租户前缀闸**恰巧**挡住，不能当护栏存在）。写侧两道：① `key_belongs_to_tenant(...)` 归一化前缀判据（⛔ 不是 `startswith`；`..` 段一律拒；共享前缀 `shared-orders/` 在不给持有判据时 fail-closed），不过回带字段名中文业务错误、不落库；② 只收 `"<本租户 schema>/platform-auto-replies/"` 形状（同租户的订单截图 / 头像 / 别的规则的图也能过①，放行等于把它们签成可读链接）；③ `head(key)` 必须找得到对象（消掉「`has_image=true` 但链接 404」；对象名形态非法也翻成同类业务错误，E-0010）。⚠️ **读侧不做 `head()`**（列表每行一次远端往返不成比例）⇒ 存量行对象被删后 `has_image` 仍 true、链接 404——如实登记的已知边界，写侧不会再造出这种行。
组合唯一性（一键抛单计划 §2.2）：同一 `(platform, game, trigger)` **只能配一条**（`game` 空 = 全部游戏、单独算一个组合，**停用的也算**）：序列化器 `validate` 先查（撞车回带字段名中文错误 1000）+ 库层 `UniqueConstraint(..., nulls_distinct=False)` 兜并发窗口（让 `game IS NULL` 也参与去重）；⛔ 不要退回「只靠应用层查一遍」。平台收窄：接口只收**代练通（10）**——`platform-integration` 页的 7 个平台是「**能存配置**」（`AVAILABLE_PLATFORMS`），本页要「**能跑引擎**」（`PLATFORMS_WITH_CLIENT`），今天只有代练通有真客户端，收 13 个会出现页面选不出来、引擎也不会用的幽灵行。权限：整个集合（读 + 写）共用页面节点 `system.platform_auto_reply`——端点面与页面同宽、不留「列表要节点、单行不要」的窄门；⚠️ 节点 code 必须是**字面量**（`tests/test_rbac_structure.py` 的 B2 走 AST 取字符串常量；`apps/rbac/wiring.py` 的位点登记本模块的 `permission_classes`）。
"""

from django.db import IntegrityError, connection, transaction
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.base import EnvelopeModelViewSet
from apps.business.models import Game, PlatformAutoReplyRule, PlatformIntegration
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.storage import (
    IMAGE_EXT_BY_CONTENT_TYPE,
    PLATFORM_REPLY_KEY_SEGMENT,
    build_platform_reply_key,
    get_storage,
    key_belongs_to_tenant,
    sniff_image_bytes,
)
from apps.rbac.permissions import has_node

#: 本集合共用的 RBAC 节点（对标点 1238「交易平台自动回复打手设置」）。
#: ⚠️ 字符串必须是**字面量**（同 `platform_integration_views.PLATFORM_INTEGRATION_NODE`）。
PLATFORM_AUTO_REPLY_NODE = "system.platform_auto_reply"

#: 回复正文长度上限：照证据件 `#replycontent` / `#batchContent` 的 `maxlength="200"`。
CONTENT_MAX_LENGTH = 200

#: 操作日志的 `target_kind`（`OperationLog.target_kind` 是 `CharField(16)` ⇒ 必须 ≤ 16）。
PLATFORM_AUTO_REPLY_TARGET_KIND = "platform_reply"

#: 「同一（平台 + 游戏 + 时机）只能一条」的库层约束名（`PlatformAutoReplyRule.Meta.constraints`）。
#: ⚠️ 只用来**认约束**；异常原文（PG 的 `DETAIL: Failing row contains (...)`）不进日志 / 响应。
RULE_COMBO_CONSTRAINT = "uniq_platform_auto_reply_rule_combo"

#: 组合重复时的**带字段名**中文错误（接口层与数据库层共用同一句，⛔ 不各写一份）。
RULE_COMBO_ERROR = (
    "同一平台 + 同一游戏 + 同一时机的自动回复规则只能有一条"
    "（停用的也算一条；如需另一套话术请先改这一条）"
)

#: 规则图片上传：multipart 字段名与单张上限。
#: ⚠️ 上限取 **2 MiB**，⛔ 不是需求里举例的 5 MiB —— 卡住的不是我们的业务判据，而是**仓里既有的请求体墙**：`NonDirectUploadBodyLimitMiddleware`（E-0095）对**非直传路径**恢复 Django 出厂默认的 2.5 MiB，超出的 multipart 请求**进视图之前**就被回 400 空体（只有 `/api/media/blob/` 直传前缀被抬到 20 MiB）⇒ 端点自己的上限必须**小于那道墙**，用户才看得到中文原因而不是无解释的 400；2 MiB 给 multipart 头部留了余量，对规则图（群码 / 二维码这类小图）也够用。
#: ⚠️ 顺带：规则图最终要被代练通发图线整份读进内存再传 OSS（`dailiantong_qr._load_picture`），大图只会让发送变慢。
UPLOAD_IMAGE_FIELD = "file"
MAX_RULE_IMAGE_BYTES = 2 * 1024 * 1024

#: 上传端的拒绝文案（前端按同一份契约提示；⛔ 标点不要单方面改）。
UPLOAD_MISSING_MSG = "请选择要上传的图片"
UPLOAD_EMPTY_MSG = "图片无效或为空"
UPLOAD_TOO_LARGE_MSG = "图片超过 2 MB，请压缩后再上传"
UPLOAD_BAD_TYPE_MSG = "只支持 jpg / jpeg / png 格式的图片"

#: `image_key` 写侧的形状闸：只收**本页上传端点产出的那一族对象名**。⚠️ 它比「本租户前缀 + 对象存在」更窄一档：那条只防跨租户，这条防的是「把本租户**别的**对象（订单截图 / 头像 / 另一条规则的图）挂到规则上」——那些对象同样会被 `presign_get` 签出可读链接。⛔ 别放宽成 `key_belongs_to_tenant` 就完事。
RULE_IMAGE_KEY_MSG = "图片对象名不合法，请通过本页的「上传」按钮重新上传"

#: 本版**能跑发送引擎**的平台（只有代练通有真客户端）。
#: ⚠️ 与 `platform_integration_views.AVAILABLE_PLATFORMS`（7 个「能存配置」）**不是同一个集合**，
#: ⛔ 别合并成一份：那 7 个里另外 6 个只落配置、没有客户端（见模块 docstring 的「平台收窄」）。
ENABLED_PLATFORMS = (PlatformIntegration.Platform.DAILIANTONG,)


def _target_name(rule: PlatformAutoReplyRule) -> str:
    """审计行里的「名字」：游戏 + 时机（⛔ 不用正文，见模块 docstring）。"""
    game = rule.game.name if rule.game_id else "全部游戏"
    return f"{game} · {rule.get_trigger_display()}"


class PlatformAutoReplyRuleSerializer(serializers.ModelSerializer):
    """一行自动回复规则的对外形状（读侧）与可写字段（写侧）。

    ⚠️ `sort_order` **不在**读写字段里（只要求模型有这一列；证据件的表格与弹层都没有它，
    页面也没有输入点 —— 暴露一个没人消费的写字段只是多一条能改乱列表顺序的路）。
    """

    #: 平台：**只收代练通**（`ENABLED_PLATFORMS`；见模块 docstring 的「平台收窄」）。
    #: `choices` 仍取模型那一份（文案与 id 字典同源），白名单在 `validate_platform` 收口。
    platform = serializers.ChoiceField(
        choices=PlatformIntegration.Platform.choices,
        required=False,
        default=PlatformIntegration.Platform.DAILIANTONG,
        error_messages={
            "invalid_choice": "平台不在可选范围内",
        },
    )
    #: 限定游戏：`null` = 全部游戏（弹层下拉的第一项）。
    #: ⚠️ `queryset` 排除**软删除**的游戏：挂在已删游戏上的规则在页面上选不出来，
    #: 却会被发送引擎读到 —— 那是幽灵配置。⛔ 不写 `Game.objects.all()`。
    game = serializers.PrimaryKeyRelatedField(
        queryset=Game.objects.filter(deleted_at__isnull=True),
        required=False,
        allow_null=True,
        error_messages={
            "does_not_exist": "限定游戏不存在或已被删除",
            "incorrect_type": "限定游戏的取值不正确",
        },
    )
    trigger = serializers.ChoiceField(
        choices=PlatformAutoReplyRule.Trigger.choices,
        required=True,
        error_messages={
            "required": "请选择回复时机",
            "blank": "请选择回复时机",
            "null": "请选择回复时机",
            "invalid_choice": "回复时机只能是 0（接单后）或 10（申请验收后）",
        },
    )
    #: 正文：必填、≤ 200、**去首尾空白**（全空白按空处理 ⇒ 不落一条「看着有内容」的空规则）。
    #: 内部换行保留（话术是多行的）。
    content = serializers.CharField(
        max_length=CONTENT_MAX_LENGTH,
        trim_whitespace=True,
        error_messages={
            "required": "回复内容不能为空",
            "blank": "回复内容不能为空",
            "null": "回复内容不能为空",
            "max_length": f"回复内容不能超过 {CONTENT_MAX_LENGTH} 个字符",
        },
    )
    #: 图片对象名：**只写不读**（读侧见 `has_image` / `image_url`）；空串 = 清空图片（那是明确的写操作，不当作「不改」）。
    #: 非空时必须过**两道**：本租户前缀（`key_belongs_to_tenant`）+ 对象真的存在（`storage.head`）——见 `validate_image_key` 与模块 docstring 的租户护栏段。
    image_key = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        write_only=True,
        help_text="图片对象名（只写不读；读侧给 has_image / image_url）",
    )

    game_id = serializers.IntegerField(read_only=True)
    game_name = serializers.SerializerMethodField()
    trigger_name = serializers.CharField(source="get_trigger_display", read_only=True)
    platform_label = serializers.CharField(source="get_platform_display", read_only=True)
    has_image = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = PlatformAutoReplyRule
        # ⭐ 关掉 DRF 从库层 `UniqueConstraint` 自动派生的 UniqueTogetherValidator：它的文案是通用的「字段 … 必须能构成唯一集合。」，讲不清「停用的也算」这条业务口径；唯一性判据**只保留**本类的 `validate()`（文案 = `RULE_COMBO_ERROR`，PATCH 只改一个键时也生效）。库层约束仍是并发窗口的最后一道。
        validators = []
        fields = [
            "id",
            "platform",
            "platform_label",
            "game_id",
            "game",
            "game_name",
            "trigger",
            "trigger_name",
            "content",
            "image_key",
            "has_image",
            "image_url",
            "is_enabled",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    # ── 只读替身 ────────────────────────────────────────────────────
    def get_game_name(self, obj) -> str:
        """游戏名；`null`（全部游戏）回**空串**（见模块 docstring）。"""
        return obj.game.name if obj.game_id else ""

    def get_has_image(self, obj) -> bool:
        return bool(obj.image_key)

    def get_image_url(self, obj) -> str:
        """**现签**的短时效读链接（没有图片就是空串）。

        ⚠️ 不落库：链接时效有限（生产 COS 整点对齐 1–2 小时），每次取列表都现取。
        ⚠️ 读侧**不** `head()`（见模块 docstring 的「读侧不做 head」那条已知边界）。
        """
        if not obj.image_key:
            return ""
        return get_storage().presign_get(obj.image_key).url

    # ── 写侧校验（安全判据，⛔ 不要删）───────────────────────────────
    def validate(self, attrs):
        """同一 `(platform, game, trigger)` 只允许一条（计划 §2.2）。库层唯一约束是最后一道；这里先查一遍，把「撞车」变成**带字段名的中文错误**（`trigger` 是本组合里页面最可能想改的那一格），而不是让用户看到 500。
        ⚠️ PATCH 时三个键都可能不在 `attrs` 里 ⇒ 回落到实例当前值；⚠️ 停用的也参与判重（约束本身就不带 `is_enabled` 条件）。
        """
        instance = self.instance
        platform = attrs.get(
            "platform",
            instance.platform
            if instance is not None
            else PlatformIntegration.Platform.DAILIANTONG,
        )
        game = attrs.get("game", instance.game if instance is not None else None)
        trigger = attrs.get("trigger", instance.trigger if instance is not None else None)
        if trigger is None:
            return attrs
        queryset = PlatformAutoReplyRule.objects.filter(
            platform=platform, trigger=trigger
        )
        queryset = (
            queryset.filter(game=game)
            if game is not None
            else queryset.filter(game__isnull=True)
        )
        if instance is not None:
            queryset = queryset.exclude(pk=instance.pk)
        if queryset.exists():
            raise serializers.ValidationError({"trigger": RULE_COMBO_ERROR})
        return attrs

    def validate_platform(self, value):
        """只收**代练通**（本版唯一有客户端、能跑引擎的平台）。"""
        if value not in ENABLED_PLATFORMS:
            raise serializers.ValidationError(
                "平台只能是「代练通」（本版只有代练通能自动回复；其它平台尚未开放）"
            )
        return value

    def validate_image_key(self, value):
        """图片对象名**三道**闸：本租户前缀 → 本页上传端点产出的形状 → 对象真的存在。⚠️ **为什么必须在这里拦**：`presign_get` 拿到任何对象名都会签出可读链接，生产是**共享 COS 桶** ⇒ 不拦就等于「持本节点的人可以给别的租户的对象签读链接」。
        ⚠️ 第二道（形状）挡的是**同租户的其它对象**：订单截图 / 头像 / 另一条规则的图都在本租户前缀下、都能被 `head()` 找到，放行就等于让这条规则把它们签成可读链接；⛔ 它**不是**「B1 真的写过这个 key」的证明（对象存储没有台账），只保证「形状只可能由 B1 产出」，存在性由第三道核，边界见模块 docstring。⚠️ 空串 = 清空图片（明确的写操作），**不查**对象（不需要它存在）。
        """
        key = value or ""
        if key == "":
            return key
        if not key_belongs_to_tenant(key, connection.schema_name):
            raise serializers.ValidationError(
                "图片对象名不属于本租户，已拒绝（请通过本系统的上传入口重新上传）"
            )
        if not key.startswith(f"{connection.schema_name}/{PLATFORM_REPLY_KEY_SEGMENT}/"):
            raise serializers.ValidationError(RULE_IMAGE_KEY_MSG)
        try:
            found = get_storage().head(key)
        except ValueError:
            # `LocalStorage._path` 对非法形态（绝对路径 / NUL 字节等）抛 ValueError：
            # 那是**请求来源的值**，必须翻成业务错误，⛔ 不许漏成 500（E-0010）。
            raise serializers.ValidationError("图片对象名不合法") from None
        if found is None:
            raise serializers.ValidationError("图片对象不存在或已失效，请重新上传")
        return key


class PlatformAutoReplyRuleViewSet(EnvelopeModelViewSet):
    """`/api/business/platform-auto-replies/`：规则列表 / 新增 / 单行 / 修改 / 删除。⚠️ 整个集合（读 + 写）共用页面节点 `system.platform_auto_reply`：端点面与页面同宽；`lookup_value_regex` 只认数字：非数字主键让 Django 走 404 而不是交给 ORM（`ValueError` → 500）。
    """

    queryset = PlatformAutoReplyRule.objects.select_related("game").all()
    serializer_class = PlatformAutoReplyRuleSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    lookup_value_regex = r"[0-9]+"
    permission_classes = [
        IsAuthenticated,
        has_node(PLATFORM_AUTO_REPLY_NODE),
    ]

    # ── 规则图片上传────────────────────────────────────
    @action(detail=False, methods=["post"], url_path="upload-image")
    def upload_image(self, request):
        """接收一张 multipart 图片、**服务端**写进既有媒体存储，返回对象名与短时效读链接。判据：字段名 `file`（multipart）；类型按**字节魔数**判（`storage.IMAGE_MAGIC`，jpg / png；⛔ 不信文件名与 `Content-Type`）；大小 ≤ `MAX_RULE_IMAGE_BYTES`（**2 MiB**，⛔ 不是 5 MiB，理由见该常量处）；对象名 `<本租户 schema>/platform-auto-replies/<随机>.<真实后缀>`（`build_platform_reply_key`）；权限与本集合同节点 `system.platform_auto_reply`；留痕一条 `system.platform_auto_reply_image_upload`（**不含**对象名 / 文件名 / 内容）。
        ⚠️ **为什么是 multipart 直传、⛔ 不是截图那套「presign + 认领」**：截图是三段式（浏览器直传对象存储、服务端拿不到字节），因为要支持大图与断点；规则图**必须由服务端判类型 / 定对象名**（⛔ 不接受客户端传任意 key）——两段式在这里更简单、也少一个「签发后没人认领」的孤儿状态；存储与租户护栏仍是同一套（`core/storage.py`，`key_belongs_to_tenant` 只用来复核自己刚拼出来的名字）。
        ⚠️ **已知边界（如实登记）**：上传成功但用户没保存规则 ⇒ 对象会留着（没有回收任务，今天也没有「按对象名反查规则」的台账）；它只占一份 2 MiB 以内的空间，不会被任何规则引用。
        """
        upload = request.FILES.get(UPLOAD_IMAGE_FIELD)
        if upload is None:
            return api_response(code=ErrorCode.VALIDATION, msg=UPLOAD_MISSING_MSG)
        if upload.size is None or upload.size <= 0:
            return api_response(code=ErrorCode.VALIDATION, msg=UPLOAD_EMPTY_MSG)
        if upload.size > MAX_RULE_IMAGE_BYTES:
            return api_response(code=ErrorCode.VALIDATION, msg=UPLOAD_TOO_LARGE_MSG)
        data = upload.read()
        # 读完按**真实长度**再兜一次：`upload.size` 是上传处理器报的，⛔ 不把它当唯一判据
        # （与 `media_blob_views` 的「读完再按实际长度兜一次」同口径）。
        if len(data) == 0:
            return api_response(code=ErrorCode.VALIDATION, msg=UPLOAD_EMPTY_MSG)
        if len(data) > MAX_RULE_IMAGE_BYTES:
            return api_response(code=ErrorCode.VALIDATION, msg=UPLOAD_TOO_LARGE_MSG)

        # ⭐ 类型判据 = **魔数**（⛔ 不看文件名、⛔ 不看 `Content-Type`）；后缀也由它派生，
        #    这样「png 改名成 .jpg」不会落出扩展名与内容不一致的对象名。
        content_type = sniff_image_bytes(data)
        if content_type is None:
            return api_response(code=ErrorCode.VALIDATION, msg=UPLOAD_BAD_TYPE_MSG)
        ext = IMAGE_EXT_BY_CONTENT_TYPE[content_type]

        # 租户上下文只来自认证态 / django-tenants 路由结果（⛔ 不接受请求参数，`AGENTS.md` §4）。
        key = build_platform_reply_key(connection.schema_name, ext)
        storage = get_storage()
        # 写失败（磁盘满 / COS 报错）**响亮地 500**：⛔ 不许回一个指不到对象的 key
        # （E-0010：未预期的错误仍应响亮）。
        storage.put_bytes(key, data, content_type=content_type)
        signed = storage.presign_get(key)

        user = request.user
        record(
            log_type=ACTION_LOG_TYPES["system.platform_auto_reply_image_upload"],
            action="system.platform_auto_reply_image_upload",
            actor=user.get_username(),
            actor_id=user.pk,
            target_kind=PLATFORM_AUTO_REPLY_TARGET_KIND,
            target_name="自动回复图片",
        )
        return api_response(
            {"image_key": key, "url": signed.url, "expires_in": signed.expires_in}
        )

    def perform_create(self, serializer):
        # 留痕与业务写入同一个事务：日志写不进去时规则行要跟着回滚。
        with transaction.atomic():
            self._save_translating_duplicate(serializer)
            user = self.request.user
            record(
                log_type=ACTION_LOG_TYPES["system.platform_auto_reply_create"],
                action="system.platform_auto_reply_create",
                actor=user.get_username(),
                actor_id=user.pk,
                target_kind=PLATFORM_AUTO_REPLY_TARGET_KIND,
                target_id=serializer.instance.pk,
                target_name=_target_name(serializer.instance),
            )

    def perform_update(self, serializer):
        with transaction.atomic():
            # no-op 判据必须在**写之前**取：`save()` 之后实例上就是新值了。
            changed = self._changes_anything(serializer)
            self._save_translating_duplicate(serializer)
            if changed:
                user = self.request.user
                record(
                    log_type=ACTION_LOG_TYPES["system.platform_auto_reply_update"],
                    action="system.platform_auto_reply_update",
                    actor=user.get_username(),
                    actor_id=user.pk,
                    target_kind=PLATFORM_AUTO_REPLY_TARGET_KIND,
                    target_id=serializer.instance.pk,
                    target_name=_target_name(serializer.instance),
                )

    def perform_destroy(self, instance):
        # `delete()` 会把实例 pk 置 `None` ⇒ 目标两列必须在**删除前**取。
        target_id = instance.pk
        target_name = _target_name(instance)
        with transaction.atomic():
            super().perform_destroy(instance)
            user = self.request.user
            record(
                log_type=ACTION_LOG_TYPES["system.platform_auto_reply_destroy"],
                action="system.platform_auto_reply_destroy",
                actor=user.get_username(),
                actor_id=user.pk,
                target_kind=PLATFORM_AUTO_REPLY_TARGET_KIND,
                target_id=target_id,
                target_name=target_name,
            )

    @staticmethod
    def _save_translating_duplicate(serializer) -> None:
        """`save()` 撞组合唯一约束 ⇒ 带字段名的中文业务错误（`code=1000`）。

        ⚠️ 正常路径走不到这里：序列化器的 `validate` 已经查过一遍；这条接住的是**并发窗口**
        （两个请求同时提交同一组合）。只拿约束名判断；其它完整性错误原样 re-raise（E-0010）。
        """
        try:
            serializer.save()
        except IntegrityError as exc:
            if RULE_COMBO_CONSTRAINT not in str(exc):
                raise
            raise serializers.ValidationError({"trigger": RULE_COMBO_ERROR}) from None

    @staticmethod
    def _changes_anything(serializer) -> bool:
        """`PATCH` 的载荷里有没有任何一处与实例**当前值**不同（纯 no-op 不写留痕）。

        ⚠️ `game` 是 FK：`validated_data` 里是 `Game` 实例、`getattr(instance, "game")` 也是
        实例 ⇒ Django 模型的 `__eq__` 比主键（不是对象同一性），两边同一个游戏时不会误判成变化。
        """
        instance = serializer.instance
        for field, value in serializer.validated_data.items():
            if value != getattr(instance, field):
                return True
        return False
