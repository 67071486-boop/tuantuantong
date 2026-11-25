"""频道文章的序列化器（管理端）。三件事只在这里定义、视图只搬运：① **字段长度与中文文案**（`title` / `intro` / `content_html` / `cover_key`）——列宽在模型上、判据在这里，⛔ 不靠数据库抛 `DataError` 才发现超长；② **`status` 的副作用**（`published_at` 只在第一次发布时落）——新建、PATCH、`publish` 动作三处入口共用 `apply_status`，各写一份必然漂；③ **图片对象名的归属判据**（`is_article_media_key`）——写文章的封面 / 正文与媒体认领都用它，但**档位不同**（见该函数的 docstring）。
为什么写文章的图片判据比「认领」宽一档（业务方修正）：同一租户可能有第二个管理员编辑同一篇文章、图是前一个人传的，若把「当前用户 id」写进写文章的判据，那位管理员一保存就失败。三档判据：签发票据 `media/ticket` 固定按**当前用户 id** 生成 key（`build_user_key`）；认领 `media/claim` 只认**当前用户自己**上传的 key（`is_article_media_key(..., user_id=本人)`）；写文章（`cover_key` / 正文 `<img data-key>`）= 本租户 + `users/<任意数字>/article-media.`。
⛔ 三者不能合并成一份判据：合成「当前用户」会让协作编辑失败，合成「任意用户」会让认领端点变成「谁都能把别人刚传的图挂到自己名下」。
"""
from django.db import connection
from django.utils import timezone
from rest_framework import serializers

from apps.business.article_content import (
    MAX_CONTENT_LENGTH,
    extract_image_keys,
    sanitize_html,
)
from apps.business.models import ChannelArticle, ChannelProfile
from apps.core.storage import USER_KEY_SEGMENT, get_storage, key_belongs_to_tenant

#: `build_user_key` 的种类段（与 `apps/core/storage.py::USER_KEY_KINDS` 里的同名字面量同源）。
ARTICLE_MEDIA_KIND = "article-media"

#: `ChannelArticle.title` / `.intro` / `.cover_key` 的列宽（判据与模型列宽同源，⛔ 不各写一个数）。
MAX_TITLE_LENGTH = ChannelArticle._meta.get_field("title").max_length
MAX_INTRO_LENGTH = ChannelArticle._meta.get_field("intro").max_length
MAX_COVER_KEY_LENGTH = ChannelArticle._meta.get_field("cover_key").max_length
MAX_PROFILE_INTRO_LENGTH = ChannelProfile._meta.get_field("intro").max_length

#: 中文文案（前端直接显示；⛔ 标点不要动）。
TITLE_REQUIRED_MSG = "请输入文章标题"
TITLE_TOO_LONG_MSG = f"标题不能超过 {MAX_TITLE_LENGTH} 个字"
INTRO_TOO_LONG_MSG = f"摘要不能超过 {MAX_INTRO_LENGTH} 个字"
CONTENT_TOO_LONG_MSG = f"正文不能超过 {MAX_CONTENT_LENGTH} 个字"
STATUS_INVALID_MSG = "状态只能是草稿或已发布"
COVER_NOT_OURS_MSG = "封面图片不属于本工作室"
CONTENT_IMAGE_NOT_OURS_MSG = "正文图片不属于本工作室"


def is_article_media_key(key: str, schema: str, *, user_id: int | None = None) -> bool:
    """这个对象名是不是**本租户的文章图片**（`user_id` 给了就再收窄到那个人）。形状：`<租户>/users/<用户 id>/article-media.<随机>.<ext>`。判据三段：① `key_belongs_to_tenant(key, schema)` —— **归一化**判据（按 `/` 切段、剔除空段与 `.`、任何 `..` 一律拒；⛔ 不是裸 `startswith`：`demo/../victim/…` 的字符串前缀确实是 `demo/`，解析后却落在别人的命名空间里，见 `apps/core/storage.py` 的 docstring）；② 归一化后**恰好四段**：`[本租户, "users", 用户 id(纯数字), "article-media." 开头]`；③ `user_id` 不为 `None` 时，第 3 段必须**就是**它（认领端点用这一档）。
    ⚠️ 写文章时**不传** `user_id`：协作编辑要能引用同事传的图（见模块 docstring 的表）。
    """
    if not key or not key_belongs_to_tenant(key, schema):
        return False
    parts = [p for p in key.split("/") if p not in ("", ".")]
    if len(parts) != 4:
        return False
    if parts[0] != schema or parts[1] != USER_KEY_SEGMENT:
        return False
    if not parts[2].isdigit():
        return False
    if user_id is not None and parts[2] != str(user_id):
        return False
    return parts[3].startswith(f"{ARTICLE_MEDIA_KIND}.")


def _tenant_schema() -> str:
    """租户标识 = **租户 schema 名**（⛔ 不接受任何请求参数传入的租户标识，`AGENTS.md` §4）。"""
    return connection.schema_name


def _media_url(key: str) -> str:
    """对象名 → 短时效读链接；空 key / 签不出来一律回**空串**（前端按空串不渲染）。

    ⚠️ 回落空串而不是 `null`：与 `GameSerializer.logo_url` 同一条口径（前端只判空串）。
    ⚠️ 签名失败（对象存储抽风）不该让整个列表 / 详情 500。
    """
    if not key:
        return ""
    try:
        return get_storage().presign_get(key).url
    except Exception:  # noqa: BLE001 —— 存储后端异常不该让读接口整页失败
        return ""


def apply_status(article: ChannelArticle, new_status: str) -> bool:
    """把状态改成 `new_status`；**首次**发布补 `published_at`，之后不覆盖。返回「有没有真的改了什么」（视图据此决定要不要 `save()`）。⚠️ 改回草稿**不清** `published_at`：那是「这篇什么时候第一次发出去」的历史事实（计划 §2 判据 4：下架后公开列表消失，但历史值留着）。
    """
    changed = False
    if article.status != new_status:
        article.status = new_status
        changed = True
    if (
        new_status == ChannelArticle.Status.PUBLISHED
        and article.published_at is None
    ):
        article.published_at = timezone.now()
        changed = True
    return changed


class ArticleListSerializer(serializers.ModelSerializer):
    """管理端列表的一行（不含正文——列表不背长文本）。"""

    cover_url = serializers.SerializerMethodField()

    class Meta:
        model = ChannelArticle
        fields = [
            "id",
            "title",
            "intro",
            "status",
            "cover_url",
            "read_count",
            "published_at",
            "created_at",
            "updated_at",
            "created_by",
        ]
        read_only_fields = fields

    def get_cover_url(self, obj) -> str:
        return _media_url(obj.cover_key)


class ArticleDetailSerializer(ArticleListSerializer):
    """管理端详情 = 列表那几列 + 原文 + 正文图片的读链接。⚠️ `content_html` **原样**回（白名单 HTML，`data-key` 仍在）：编辑器要拿它回填 `contenteditable`，把 key 换成 URL 再回填会把会过期的签名地址写进正文。⚠️ `media` = 正文里出现过的每个 key → 现签读链接（编辑器据此渲染 `<img>`），`cover_url` 单独给（封面不在正文里）。
    """

    media = serializers.SerializerMethodField()

    class Meta(ArticleListSerializer.Meta):
        fields = [*ArticleListSerializer.Meta.fields, "content_html", "media"]
        read_only_fields = fields

    def get_media(self, obj) -> dict:
        return {key: _media_url(key) for key in extract_image_keys(obj.content_html)}


class ArticleWriteSerializer(serializers.ModelSerializer):
    """新建 / PATCH 的入参。字段级中文文案，长度判据在这一层（模型列宽只是兜底）。"""

    title = serializers.CharField(
        max_length=MAX_TITLE_LENGTH,
        trim_whitespace=True,
        error_messages={
            "required": TITLE_REQUIRED_MSG,
            "blank": TITLE_REQUIRED_MSG,
            "null": TITLE_REQUIRED_MSG,
            "max_length": TITLE_TOO_LONG_MSG,
            "invalid": TITLE_REQUIRED_MSG,
        },
    )
    intro = serializers.CharField(
        max_length=MAX_INTRO_LENGTH,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        error_messages={
            "max_length": INTRO_TOO_LONG_MSG,
            "invalid": INTRO_TOO_LONG_MSG,
            "null": INTRO_TOO_LONG_MSG,
        },
    )
    cover_key = serializers.CharField(
        max_length=MAX_COVER_KEY_LENGTH,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        error_messages={
            "max_length": "封面对象名太长",
            "invalid": "封面对象名格式不正确",
            "null": "封面对象名格式不正确",
        },
    )
    content_html = serializers.CharField(
        max_length=MAX_CONTENT_LENGTH,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        error_messages={
            "max_length": CONTENT_TOO_LONG_MSG,
            "invalid": CONTENT_TOO_LONG_MSG,
            "null": CONTENT_TOO_LONG_MSG,
        },
    )
    status = serializers.ChoiceField(
        choices=ChannelArticle.Status.choices,
        required=False,
        error_messages={
            "invalid_choice": STATUS_INVALID_MSG,
            "null": STATUS_INVALID_MSG,
        },
    )

    class Meta:
        model = ChannelArticle
        fields = ["title", "intro", "cover_key", "content_html", "status"]

    # -- 字段级校验 --------------------------------------------------------
    def validate_cover_key(self, value: str) -> str:
        if not value:
            return ""
        if not is_article_media_key(value, _tenant_schema()):
            raise serializers.ValidationError(COVER_NOT_OURS_MSG)
        return value

    def validate_content_html(self, value: str) -> str:
        """落库前清洗（唯一出口 `article_content.sanitize_html`），并核正文图片的归属。"""
        cleaned = sanitize_html(value)
        schema = _tenant_schema()
        if any(not is_article_media_key(key, schema) for key in extract_image_keys(cleaned)):
            raise serializers.ValidationError(CONTENT_IMAGE_NOT_OURS_MSG)
        return cleaned

    # -- 写入 ---------------------------------------------------------------
    def create(self, validated_data):
        request = self.context.get("request")
        validated_data["created_by"] = (
            getattr(request.user, "get_username", lambda: "")() if request else ""
        )
        # ⚠️ 不能直接 `super().create(...)`：`status=published` 时还要落 `published_at`，
        #    而那个联动只允许有一处实现（`apply_status`）。
        article = ChannelArticle(**validated_data)
        apply_status(article, article.status)
        article.save()
        return article

    def update(self, instance, validated_data):
        new_status = validated_data.pop("status", None)
        if new_status is not None:
            # ⚠️ 状态与 `published_at` 的联动只有 `apply_status` 一处实现。
            apply_status(instance, new_status)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        return instance


class ChannelProfileWriteSerializer(serializers.Serializer):
    """频道简介（PUT，唯一可写列）。

    ⚠️ `name` **不在这里**：它一律取 `connection.tenant.name`（唯一出处）；
    调用方就算把它塞进请求体，DRF 也会忽略（不会落库、也不会 500）。
    """

    intro = serializers.CharField(
        max_length=MAX_PROFILE_INTRO_LENGTH,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        error_messages={
            "max_length": f"简介不能超过 {MAX_PROFILE_INTRO_LENGTH} 个字",
            "invalid": f"简介不能超过 {MAX_PROFILE_INTRO_LENGTH} 个字",
            "null": "简介格式不正确",
        },
    )


class ArticleMediaTicketSerializer(serializers.Serializer):
    """要一张上传凭证（只收扩展名）。形状照 `GameLogoTicketSerializer`。"""

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


class ArticleMediaClaimSerializer(serializers.Serializer):
    """认领一张刚上传的图（只收对象名，长度与列宽同源）。"""

    key = serializers.CharField(
        max_length=MAX_COVER_KEY_LENGTH,
        error_messages={
            "required": "缺少图片",
            "blank": "缺少图片",
            "null": "缺少图片",
            "max_length": "图片对象名太长",
        },
    )
