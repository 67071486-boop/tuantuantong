"""工作室频道的**公开读**端点（匿名可读）：`GET /api/channel/profile/`（工作室名、简介、已发布篇数、头像首字）；`GET /api/channel/articles/`（已发布文章分页，`page_size` 默认 10、上限 50）；`GET /api/channel/articles/<id>/`（详情 + 扁平块，**每次读取 `read_count += 1`**）。
三条硬口径：① **匿名可读**：`permission_classes = [AllowAny]` **必须显式写**（全局默认 `IsAuthenticated` fail-closed，不写就是 401）；⚠️ **`authentication_classes = []` 也必须显式写**（业务方裁定）：移动端 `apiGet` 只要本地有令牌就带 `Authorization: Bearer …`，令牌过期 / 无效时 DRF 在**认证层**先 401、轮不到 `AllowAny` ⇒ 症状变成「没登录也能看，但令牌一过期整个频道页加载失败」；公开端点不做个性化（全租户同一份内容）⇒ 干脆不认证，与 `xgj_push_views.py` 同口径，租户仍只从 Host 解析。② **不接受任何租户参数**（AGENTS.md §4）：租户只来自 Host 解析出的 schema，没有也不该加 `tenant`/`schema` 入参。③ **平面守卫**：`public` 平面上没有 `business_channelarticle` 表，查它就是 500 ⇒ 三个视图体开头一律 `on_public_plane()` 回 **404**（照 `profile_views` 口径），不靠权限类挡（`AllowAny` 挡不住任何东西）。
草稿一律「查不到」（不泄露存在性）：列表只 `filter(status=published)`；详情在同一条查询里带 `status=published` 做原子自增、`update()` 返回 0 行 ⇒ `raise Http404`——草稿与「不存在」走同一条出口同一句文案（计划 §2 判据 1），⛔ 不要先 `get()` 再判状态（草稿会以 403 等形态泄露存在性）。⚠️ 这里的「404」是本仓的业务 404（`apps/core/exceptions.py`：`Http404` → **HTTP 200 + `code=1002` +「记录不存在或已被删除」**），⛔ 不是 HTTP 404——全仓口径「HTTP 状态码只表达传输与认证层，业务结果由 `code` 表达」，移动端按信封 `code` 分支。阅读数只在公开详情 +1（管理端 `article_views.ArticleViewSet.retrieve` **不计数**：管理员预览不该算阅读，计划 §2 判据 2）；自增用 `F("read_count") + 1` 交给数据库做（原子），不是「读出来 +1 再写回去」（并发丢计数）。回块不回 HTML：移动端对 HTML 三端渲染差异大，服务端把白名单 HTML 解析成扁平块、移动端按 `type` 排版（计划 §1：⛔ 不把 HTML 直接回给移动端），解析器只有一处 `apps/business/article_content.parse_blocks`。
"""
from django.db import connection
from django.db.models import F
from django.http import Http404
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.business.api.base import EnvelopePagination
from apps.business.article_content import parse_blocks
from apps.business.models import ChannelArticle, ChannelProfile
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response
from apps.core.plane import on_public_plane, plane_not_found_response
from apps.core.storage import append_thumbnail_query, get_storage, key_belongs_to_tenant


class ChannelPagination(EnvelopePagination):
    """公开列表分页：默认 10 条、**上限 50**（移动端下拉刷新一屏的量）。⚠️ 不复用管理端的 20 / 200：那是给网页端表格的；移动端一次 200 条会把首屏拖垮。"""

    page_size = 10
    max_page_size = 50


def _sign(key: str) -> str:
    """对象名 → 短时效读链接；空 key / 签不出来一律空串（移动端按空串不渲染封面）。⚠️ 纵深：库里的内容写端已校验过租户归属，但读路径**再核一次**（`key_belongs_to_tenant`）——畸形 / 历史脏行的跨租户 key 一律不签，返回空串。"""
    if not key:
        return ""
    if not key_belongs_to_tenant(key, connection.schema_name):
        return ""
    try:
        return get_storage().presign_get(key).url
    except Exception:  # noqa: BLE001 —— 存储后端异常不该让公开读整页 500
        return ""


def _sign_cover_thumb(key: str) -> str:
    """**文章列表**封面拼 CI 缩图参数（数据万象 CI，业务方 2026-11-09）。

    ⚠️ 只用于列表行的小卡封面；详情封面 / 正文插图走 `_sign` 原图（读正文要清晰）。
    """
    return append_thumbnail_query(_sign(key))


class _PublicPlaneMixin:
    """三个公开端点的公共装配：**不认证** + `AllowAny` + 平面守卫。⚠️ `authentication_classes = []` 是**承重**写法（不是防御性写法）：留着默认认证类时，请求只要带一个无效 / 过期的 `Bearer` 令牌，DRF 会在认证层抛 `AuthenticationFailed` ⇒ **401**、`AllowAny` 根本轮不到 ⇒「未登录也能看频道」在**令牌过期**这个真实场景下直接破产（移动端 `apiGet` 会无脑带上本地令牌）。公开读不做个性化，所以不认证是安全的。"""

    authentication_classes = []
    permission_classes = [AllowAny]

    def _plane_guard(self):
        if on_public_plane():
            return plane_not_found_response()
        return None


def channel_profile_payload() -> dict:
    """频道头部（计划 §1 的字段表）。⚠️ `article_count` **只数已发布**（草稿不该出现在公开面的计数里）；⚠️ `avatar_text` = 工作室名第一个字符（空名回空串，移动端据此画「首字 + 色块」）；⚠️ 简介用 `filter(pk=1).first()` 而**不是** `get_or_create`：公开 GET 不该写库。"""
    profile = ChannelProfile.objects.filter(pk=1).first()
    name = getattr(connection.tenant, "name", "") or ""
    return {
        "name": name,
        "intro": profile.intro if profile is not None else "",
        "article_count": ChannelArticle.objects.filter(
            status=ChannelArticle.Status.PUBLISHED
        ).count(),
        "avatar_text": name[:1],
    }


class ChannelProfileView(_PublicPlaneMixin, APIView):
    """`GET /api/channel/profile/` —— 匿名可读。"""

    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        return api_response(channel_profile_payload())


class ChannelArticleListView(_PublicPlaneMixin, APIView):
    """`GET /api/channel/articles/?page=&page_size=` —— 只回已发布，按发布时间倒序。"""

    pagination_class = ChannelPagination

    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        rows = ChannelArticle.objects.filter(
            status=ChannelArticle.Status.PUBLISHED
        ).order_by(F("published_at").desc(nulls_last=True), "-id")
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(rows, request, view=self)
        items = [
            {
                "id": row.pk,
                "title": row.title,
                "intro": row.intro,
                "cover_url": _sign_cover_thumb(row.cover_key),
                "published_at": local_iso(row.published_at),
            }
            for row in page
        ]
        return paginator.get_paginated_response(items)


class ChannelArticleDetailView(_PublicPlaneMixin, APIView):
    """`GET /api/channel/articles/<id>/` —— 已发布才可见；每次读取阅读数 +1。"""

    def get(self, request, pk):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        # ⚠️ 自增与「可见性判据」放在**同一条 UPDATE** 里：草稿 / 不存在都返回 0 行 ⇒ 404。
        #    换成「先 get 再判状态」会让草稿以别的形态泄露存在性（计划 §2 判据 1）。
        updated = ChannelArticle.objects.filter(
            pk=pk, status=ChannelArticle.Status.PUBLISHED
        ).update(read_count=F("read_count") + 1)
        if not updated:
            raise Http404
        article = ChannelArticle.objects.filter(pk=pk).first()
        if article is None:
            # 自增成功后、读回之前被删：与「不存在」同一出口，⛔ 不要 500。
            raise Http404
        return api_response(
            {
                "id": article.pk,
                "title": article.title,
                "cover_url": _sign(article.cover_key),
                "published_at": local_iso(article.published_at),
                "blocks": parse_blocks(article.content_html, _sign),
                "studio_name": getattr(connection.tenant, "name", "") or "",
            }
        )
