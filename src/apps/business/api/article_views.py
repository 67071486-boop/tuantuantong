"""频道文章的管理端端点（登录 + 节点 `system.article_manage`）：`GET`/`POST /api/business/articles/`（全部状态的分页列表带阅读数 / 新建默认草稿）、`GET`/`PATCH`/`DELETE /api/business/articles/<id>/`（详情额外给 `content_html`/`media`/`cover_url`；保存即生效；硬删——文章不是业务凭据，同公告口径）、`POST …/<id>/publish/`（发布，`published_at` 只写一次）、`POST …/<id>/unpublish/`（下架回草稿，保留 `published_at`）、`POST /api/business/articles/media/ticket`（签发直传凭证，按**当前用户 id** 生成 key）、`POST …/media/claim`（认领：归属 / 大小 / 魔数三道校验）、`GET`/`PUT /api/business/channel-profile/`（工作室名 + 简介 + 文章数 / 只写简介 ≤200）。
四条装配口径：① **全部端点（含读）都走 `system.article_manage` 节点门**（业务方裁定：网页端这一页是「发布文章作品」的独立入口）；判据写在 `get_permissions()` 的**方法体**里、code 取**模块常量** `ARTICLE_NODE`——接线位点表（`apps/rbac/wiring.py` 的 `SITES["system.article_manage"]`）的 T2 判据解析的就是那个方法体（含一层模块常量解引用），只写在类级 `permission_classes` 上它解析不到；⚠️ `has_node` **方法内导入**（照 `notice_views.py`）：本模块在 urlconf 载入期就被 import，模块级导入租户层模块会让平台层在导入期加载它。② **媒体两条端点用显式 `path()`、不用 `@action`**：计划给的路径**没有尾斜杠**（与游戏 logo 同形），`@action` 会生成带尾斜杠的第二套形状，POST 被 301 到带斜杠地址时浏览器会丢掉请求体。③ **`media/ticket` 只签发、不写库**（同 `MyProfileMediaTicketView` / `GameLogoTicketView`）：签发了但没传完，库里不该出现指向不存在对象的行。④ **`read_count` 只在公开详情读取时 +1**（`channel_views.py`），⛔ 管理端 GET **不计数**：管理员预览一次不该算一次阅读（计划 §2 判据 2）。
删除的边界：`DELETE` 是**硬删**文章行；⛔ **不删对象存储里的图**——正文 / 封面里的 key 可能被别的文章复用（复制一篇再改），按 key 删会把别人的图带走。代价如实登记：孤立对象会留在存储里——这是刻意选的，不是遗漏。
"""
from django.db import connection
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.business.api.article_serializers import (
    ArticleDetailSerializer,
    ArticleListSerializer,
    ArticleMediaClaimSerializer,
    ArticleMediaTicketSerializer,
    ArticleWriteSerializer,
    ChannelProfileWriteSerializer,
    apply_status,
    is_article_media_key,
)
from apps.business.api.base import EnvelopeModelViewSet, EnvelopePagination
from apps.business.models import ChannelArticle, ChannelProfile
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.storage import (
    ALLOWED_EXTS,
    MAX_USER_MEDIA_BYTES,
    build_user_key,
    get_storage,
    key_belongs_to_tenant,
    sniff_image_type,
)

#: 三个节点门端点（文章集合 / 频道资料 / 媒体两条）共用的权限节点。
#: ⚠️ 字符串必须是**字面量**：`tests/test_rbac_wiring.py` 的 T2 走 AST 取模块常量。
ARTICLE_NODE = "system.article_manage"

#: 节点门拒人时的 403 msg（⛔ 必须显式给：DRF 默认是英文句）。
NODE_DENIED_MESSAGE = "当前职位没有此操作所需的权限节点"

#: `build_user_key` 的种类段（`apps/core/storage.py` 的 `USER_KEY_KINDS` 里同名字面量）。
ARTICLE_MEDIA_KIND = "article-media"

#: 拒绝文案（中文界面直接显示；与头像 / 反馈 / 游戏 logo 几条线的同位文案一致）。
IMAGE_TYPE_LIMIT_MSG = "只支持 jpg / jpeg / png"
IMAGE_SIZE_LIMIT_MSG = "图片超过 10 MB"
IMAGE_EMPTY_MSG = "图片无效或为空"
KEY_NOT_UPLOADED_MSG = "图片还没上传成功"
FOREIGN_KEY_MSG = "图片对象名不属于当前工作室"
OWN_KEY_MSG = "图片对象名不属于当前用户"


def _tenant_schema() -> str:
    """租户标识 = **租户 schema 名**（⛔ 不接受请求参数传入的租户标识，`AGENTS.md` §4）。"""
    return connection.schema_name


def _tenant_name() -> str:
    """工作室名。**唯一出处**是 `connection.tenant.name`（⛔ 本仓不存副本，见模型 docstring）。"""
    return getattr(connection.tenant, "name", "") or ""


class _ArticleNodeBase:
    """三个面（文章集合 / 频道资料 / 媒体）共用的权限装配：登录 + `system.article_manage`。⚠️ 形状照 `notice_views.SystemNoticeViewSet.get_permissions`：与默认权限类**取并集**、code 取模块常量。`public` 平面上 `has_node` 恒假 ⇒ 请求在这里 403，**不会**走到视图体去查一张只存在于租户 schema 的表（不会 500）。
    """

    def get_permissions(self):
        base = [permission() for permission in self.permission_classes]
        others = [p for p in base if type(p) is not IsAuthenticated]
        from apps.rbac.permissions import has_node

        gate = has_node(ARTICLE_NODE)
        gate.message = NODE_DENIED_MESSAGE
        return [IsAuthenticated(), gate(), *others]


def _detail_payload(view, article) -> dict:
    """管理端详情载荷（三个写入口的响应共用一份形状）。"""
    serializer = ArticleDetailSerializer(article, context=view.get_serializer_context())
    return dict(serializer.data)


class ArticleViewSet(_ArticleNodeBase, EnvelopeModelViewSet):
    """频道文章的 CRUD + 发布 / 下架。"""

    queryset = ChannelArticle.objects.all()
    pagination_class = EnvelopePagination
    #: 给 `get_serializer_class` 兜底；三种形状见那里。
    serializer_class = ArticleDetailSerializer
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == "list":
            return ArticleListSerializer
        if self.action in ("create", "update", "partial_update"):
            return ArticleWriteSerializer
        return ArticleDetailSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        article = serializer.save()
        return api_response(_detail_payload(self, article))

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        article = self.get_object()
        serializer = self.get_serializer(article, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        article = serializer.save()
        return api_response(_detail_payload(self, article))

    @action(detail=True, methods=["post"], url_path="publish")
    def publish(self, request, pk=None):
        """发布：`published_at` **只写一次**（已发布再点不覆盖首次时刻）。"""
        article = self.get_object()
        if apply_status(article, ChannelArticle.Status.PUBLISHED):
            article.save()
        return api_response(_detail_payload(self, article))

    @action(detail=True, methods=["post"], url_path="unpublish")
    def unpublish(self, request, pk=None):
        """下架回草稿：只改 `status`，⛔ **不清** `published_at`（保留历史值）。"""
        article = self.get_object()
        if apply_status(article, ChannelArticle.Status.DRAFT):
            article.save()
        return api_response(_detail_payload(self, article))


class ArticleMediaTicketView(_ArticleNodeBase, APIView):
    """`POST /api/business/articles/media/ticket` —— 签发一张只能写**本用户**对象的凭证。"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ArticleMediaTicketSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ext = serializer.validated_data["ext"].lower().lstrip(".")
        if ext not in ALLOWED_EXTS:
            return api_response(code=ErrorCode.VALIDATION, msg=IMAGE_TYPE_LIMIT_MSG)
        key = build_user_key(_tenant_schema(), request.user.pk, ARTICLE_MEDIA_KIND, ext)
        signed = get_storage().presign_put(key, cache_control=True)
        return api_response(
            {"key": key, "upload_url": signed.url, "expires_in": signed.expires_in}
        )


class ArticleMediaClaimView(_ArticleNodeBase, APIView):
    """`POST /api/business/articles/media/claim` —— 校验后回一张短时效读链接。校验顺序固定（与头像 / 反馈 / 游戏 logo 三条线同序）：
    ① `key_belongs_to_tenant` —— 跨租户的 key ⇒ **403**（越权形状，不是入参格式错）；② `is_article_media_key(..., user_id=本人)` —— **认领只认自己刚传的那张**（⛔ 与「写文章」那一档的宽口径**不是**同一条判据，见 `article_serializers` 的表）；③ `head` 存在 → 非 0 字节 → ≤ 10 MB（`MAX_USER_MEDIA_BYTES`，头像那条口径）→ 魔数认得出是 jpg / png（`sniff_image_type`，⛔ 不信 `Content-Type` 与扩展名）。第③步任何一条不过都**删掉这个刚传上来的对象**再回拒绝（照 `MyProfileMediaClaimView` 的清理口径）；前两步不过**不删**——那可能是别人的对象。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ArticleMediaClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        key = serializer.validated_data["key"]
        schema = _tenant_schema()

        if not key_belongs_to_tenant(key, schema):
            raise PermissionDenied(FOREIGN_KEY_MSG)
        if not is_article_media_key(key, schema, user_id=request.user.pk):
            raise PermissionDenied(OWN_KEY_MSG)

        storage = get_storage()
        meta = storage.head(key)
        if meta is None:
            return api_response(code=ErrorCode.VALIDATION, msg=KEY_NOT_UPLOADED_MSG)
        # ⚠️ 只取**大小**；`head()` 给的内容类型故意丢弃——客户端可控 / 本机按扩展名推，不可信。
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

        return api_response({"key": key, "url": storage.presign_get(key).url})


class ChannelProfileAdminView(_ArticleNodeBase, APIView):
    """`GET/PUT /api/business/channel-profile/` —— 频道简介的维护面。`name` 自动取租户名（**不存库**，唯一出处 `connection.tenant.name`）；`article_count` = **已发布**篇数，与公开端 `channel_views.channel_profile_payload` **同义**（业务方裁定：网页端右侧的手机预览显示的就是这个数，两边同名不同义会让预览和真机对不上）；`total_count` = **全部状态**篇数（管理端自己的那一格：草稿也数）。
    """

    permission_classes = [IsAuthenticated]

    def _payload(self) -> dict:
        # ⚠️ 读路径**不** `get_or_create`：GET 不该写库（与公开端同一条口径；
        #    单例行由第一次 `PUT` 建出来，没建过时 `intro` 就是空串）。
        profile = ChannelProfile.objects.filter(pk=1).first()
        return {
            "name": _tenant_name(),
            "intro": profile.intro if profile is not None else "",
            "article_count": ChannelArticle.objects.filter(
                status=ChannelArticle.Status.PUBLISHED
            ).count(),
            "total_count": ChannelArticle.objects.count(),
        }

    def get(self, request):
        return api_response(self._payload())

    def put(self, request):
        serializer = ChannelProfileWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # PUT 是整体替换：没给 `intro` = 清空（⛔ 不要用 `get("intro", 现值)`，那会让
        # 「清空简介」这个动作永远做不到）。
        intro = serializer.validated_data.get("intro", "")
        profile, _ = ChannelProfile.objects.get_or_create(pk=1)
        if profile.intro != intro:
            profile.intro = intro
            profile.save(update_fields=["intro", "updated_at"])
        return api_response(self._payload())
