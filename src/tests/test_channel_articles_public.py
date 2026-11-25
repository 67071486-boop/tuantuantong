"""工作室频道的**公开读** API（匿名，2026-10-23，`/api/channel/…`）。

覆盖计划 §2 的判据：

- 匿名可读 profile / 列表 / 详情；草稿在列表里消失、详情「查不到」（业务 404）；
- 阅读数只在公开详情 +1（管理端 GET 不计数，见 `test_channel_articles_api`）；
- 跨租户拿不到（两个租户各造文章，用域名客户端；判据只认**租户内唯一、且两租户取值不同**
  的业务值 —— ⛔ 不用主键，两边的自增序列各自从 1 开始）；
- `page_size` 默认 10、上限 50；
- `public` 平面回 404（不是 500）。

⚠️ 夹具值一律**合成值**（PII 守卫扫 `src/**`）。
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context, tenant_context

from apps.business.models import ChannelArticle, ChannelProfile
from apps.platform.models import Tenant
from apps.platform.provisioning import provision_tenant

DOMAIN_A = "tenant.test.com"
SCHEMA_A = "test"
NAME_A = "甲频道工作室"

DOMAIN_B = "channel-iso-b.test.local"
SCHEMA_B = "channel_iso_b"
NAME_B = "乙频道工作室"

URL_PROFILE = "/api/channel/profile/"
URL_LIST = "/api/channel/articles/"


def detail_url(pk) -> str:
    return f"/api/channel/articles/{pk}/"


def _published(title, *, intro="", cover_key="", when=None, content_html=""):
    return ChannelArticle.objects.create(
        title=title,
        intro=intro,
        cover_key=cover_key,
        content_html=content_html,
        status=ChannelArticle.Status.PUBLISHED,
        published_at=when or timezone.now(),
    )


class ChannelPublicReadTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        with schema_context("public"):
            Tenant.objects.filter(pk=self.tenant.pk).update(name=NAME_A)
        self.tenant.name = NAME_A
        self.anon = Client(SERVER_NAME=DOMAIN_A)

    def test_profile_is_readable_anonymously(self):
        ChannelProfile.objects.create(pk=1, intro="这里是工作室简介")
        ChannelArticle.objects.create(title="草稿不计")
        _published("已发布一")
        _published("已发布二")
        res = self.anon.get(URL_PROFILE)
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(set(data), {"name", "intro", "article_count", "avatar_text"})
        self.assertEqual(data["name"], NAME_A)
        self.assertEqual(data["intro"], "这里是工作室简介")
        self.assertEqual(data["article_count"], 2, "公开端只数已发布")
        self.assertEqual(data["avatar_text"], "甲")

    def test_profile_without_a_row_is_still_200(self):
        """从没维护过简介 ⇒ 空串（⛔ 不是 500、也不是 404）。"""
        res = self.anon.get(URL_PROFILE)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["intro"], "")
        self.assertFalse(ChannelProfile.objects.exists(), "公开 GET 不该写库")

    def test_list_only_returns_published_articles(self):
        draft = ChannelArticle.objects.create(title="仅草稿")
        _published("已发布甲", when=timezone.now() - timezone.timedelta(minutes=5))
        _published("已发布乙", when=timezone.now())
        res = self.anon.get(URL_LIST)
        self.assertEqual(res.status_code, 200)
        payload = res.json()["data"]
        self.assertEqual(payload["total"], 2)
        self.assertEqual([i["title"] for i in payload["items"]], ["已发布乙", "已发布甲"])
        self.assertEqual(
            set(payload["items"][0]), {"id", "title", "intro", "cover_url", "published_at"}
        )
        self.assertNotIn(draft.pk, [i["id"] for i in payload["items"]])

    def test_list_page_size_default_is_ten_and_max_is_fifty(self):
        for index in range(12):
            _published(f"文章{index:02d}")
        default = self.anon.get(URL_LIST).json()["data"]
        self.assertEqual(default["page_size"], 10)
        self.assertEqual(len(default["items"]), 10)
        self.assertEqual(default["total"], 12)

        capped = self.anon.get(f"{URL_LIST}?page_size=999").json()["data"]
        self.assertEqual(capped["page_size"], 50)
        self.assertEqual(len(capped["items"]), 12)

        second = self.anon.get(f"{URL_LIST}?page=2&page_size=10").json()["data"]
        self.assertEqual(len(second["items"]), 2)
        self.assertEqual(second["page"], 2)

    def test_detail_is_readable_anonymously_with_blocks(self):
        article = _published(
            "带正文",
            intro="摘要",
            content_html="<p>第一段<strong>加粗</strong></p><h2>小标题</h2>",
        )
        res = self.anon.get(detail_url(article.pk))
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(set(data), {"id", "title", "cover_url", "published_at", "blocks", "studio_name"})
        self.assertEqual(data["title"], "带正文")
        self.assertEqual(data["studio_name"], NAME_A)
        self.assertEqual([b["type"] for b in data["blocks"]], ["p", "h2"])
        self.assertEqual(data["blocks"][0]["spans"][1]["text"], "加粗")
        self.assertTrue(data["blocks"][0]["spans"][1]["bold"])

    def test_list_cover_gets_the_ci_thumbnail_and_detail_stays_original(self):
        """列表封面拼 CI 缩图参数（数据万象 CI，业务方 2026-11-09）；详情封面 / 正文插图保持原图。

        本机形态下 URL 是相对 blob 路径（无 CI）——判据打在**参数串**上：
        列表侧带 `imageMogr2/thumbnail/300x/format/jpg/rquality/80`（`?` 拼），两侧详情不带。
        """
        from apps.core.storage import THUMBNAIL_QUERY

        key = f"{SCHEMA_A}/users/1/article-media.abc.png"
        article = _published(
            "有封面有图",
            cover_key=key,
            content_html=f'<p><img data-key="{key}"></p>',
        )
        list_cover = self.anon.get(URL_LIST).json()["data"]["items"][0]["cover_url"]
        detail = self.anon.get(detail_url(article.pk)).json()["data"]

        self.assertTrue(list_cover, "夹具没造对：封面 key 应能签出链接")
        self.assertTrue(list_cover.endswith(f"?{THUMBNAIL_QUERY}"))
        self.assertTrue(detail["cover_url"], "夹具没造对：详情封面应能签出链接")
        self.assertNotIn("imageMogr2", detail["cover_url"])
        for block in detail["blocks"]:
            if block["type"] == "img":
                self.assertNotIn("imageMogr2", block["url"])

    def test_detail_image_key_is_resolved_to_a_url(self):
        key = f"{SCHEMA_A}/users/1/article-media.abc.png"
        article = _published("有图", content_html=f'<p><img data-key="{key}"></p>')
        blocks = self.anon.get(detail_url(article.pk)).json()["data"]["blocks"]
        self.assertEqual(blocks[0]["type"], "img")
        self.assertEqual(blocks[0]["key"], key)
        self.assertTrue(blocks[0]["url"], "图片 key 必须换成读链接")
        self.assertNotEqual(blocks[0]["url"], key)

    def test_detail_never_returns_raw_html(self):
        article = _published("无 HTML", content_html="<p><script>alert(1)</script>正文</p>")
        data = self.anon.get(detail_url(article.pk)).json()["data"]
        self.assertNotIn("content_html", data)
        self.assertNotIn("<script>", str(data))

    def test_draft_detail_is_business_404(self):
        draft = ChannelArticle.objects.create(title="草稿详情")
        res = self.anon.get(detail_url(draft.pk))
        # 本仓的业务 404 = HTTP 200 + `code=1002`（`apps/core/exceptions.py` 的统一口径）。
        self.assertEqual(res.json()["code"], 1002)
        self.assertEqual(res.json()["msg"], "记录不存在或已被删除")

    def test_missing_detail_looks_exactly_like_a_draft(self):
        """草稿与不存在必须**同码同文案**（不泄露存在性）。"""
        draft = ChannelArticle.objects.create(title="草稿 vs 不存在")
        draft_res = self.anon.get(detail_url(draft.pk))
        missing_res = self.anon.get(detail_url(draft.pk + 9999))
        self.assertEqual(draft_res.json(), missing_res.json())

    def test_read_count_increments_only_on_public_detail(self):
        article = _published("数阅读")
        self.assertEqual(ChannelArticle.objects.get(pk=article.pk).read_count, 0)
        self.anon.get(detail_url(article.pk))
        self.anon.get(detail_url(article.pk))
        self.assertEqual(ChannelArticle.objects.get(pk=article.pk).read_count, 2)
        # 草稿那次「查不到」不计数（它连自增那一行都没命中）。
        draft = ChannelArticle.objects.create(title="草稿不计数")
        self.anon.get(detail_url(draft.pk))
        self.assertEqual(ChannelArticle.objects.get(pk=draft.pk).read_count, 0)

    def test_invalid_bearer_token_does_not_401_the_public_read(self):
        """令牌过期 / 无效时 App 仍能看到频道（DRF 认证层不许先挡掉公开读）。

        ⚠️ 业务方裁定：移动端 `apiGet` 只要本地有令牌就会带
        `Authorization: Bearer …`；若公开端点保留默认认证类，过期令牌会在认证层先 401
        ⇒「未登录也能看频道」变成「令牌一过期整页加载失败」。
        ⇒ 三个公开视图都显式 `authentication_classes = []`（与 `xgj_push_views` 同口径）。
        """
        article = _published("带坏令牌也要能看")
        auth = {"HTTP_AUTHORIZATION": "Bearer this-token-does-not-exist"}
        listed = self.anon.get(URL_LIST, **auth)
        self.assertEqual(listed.status_code, 200, listed.content[:300])
        self.assertEqual(listed.json()["data"]["total"], 1)

        detail = self.anon.get(detail_url(article.pk), **auth)
        self.assertEqual(detail.status_code, 200, detail.content[:300])
        self.assertEqual(detail.json()["data"]["title"], "带坏令牌也要能看")

        profile = self.anon.get(URL_PROFILE, **auth)
        self.assertEqual(profile.status_code, 200, profile.content[:300])

    def test_unpublishing_hides_the_article_from_the_public_list(self):
        article = _published("即将下架")
        self.assertEqual(self.anon.get(URL_LIST).json()["data"]["total"], 1)
        ChannelArticle.objects.filter(pk=article.pk).update(
            status=ChannelArticle.Status.DRAFT
        )
        self.assertEqual(self.anon.get(URL_LIST).json()["data"]["total"], 0)
        self.assertEqual(self.anon.get(detail_url(article.pk)).json()["code"], 1002)


class ChannelCrossTenantTests(TenantTestCase):
    """跨租户：两个域名客户端各看各的（判据只看业务值，不看主键）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name=NAME_B, domain=DOMAIN_B
        )

    def setUp(self):
        connection.set_tenant(self.tenant)
        with schema_context("public"):
            Tenant.objects.filter(pk=self.tenant.pk).update(name=NAME_A)
        self.tenant.name = NAME_A

    # -- 夹具 ---------------------------------------------------------------
    def _seed(self):
        """A 两篇、B 一篇 ⇒ A 的目标 pk（第 2 篇）必然 ≥ 2 > B 的最大 pk（1）。

        ⚠️ 两个 schema 的主键序列各自从 1 开始 ⇒ **必须故意错开**，否则「B 拿 A 的 pk」
        会命中 B 自己的行、返回 200（那不是隔离失效，是夹具没造对）。这一点由用例自证。
        """
        with tenant_context(self.tenant):
            _published("仅属于甲的第一篇")
            target_a = _published("仅属于甲的第二篇")
        with tenant_context(self.tenant_b):
            article_b = _published("仅属于乙的第一篇")
        self.assertNotEqual(target_a.pk, article_b.pk, "夹具没造对：两租户的目标 pk 撞了")
        return target_a, article_b

    def _get(self, url, domain):
        try:
            return Client(SERVER_NAME=domain).get(url)
        finally:
            connection.set_tenant(self.tenant)

    # -- 判据 ---------------------------------------------------------------
    def test_each_domain_sees_only_its_own_articles(self):
        _target_a, _article_b = self._seed()
        assert self.tenant.name != self.tenant_b.name
        titles_a = [i["title"] for i in self._get(URL_LIST, DOMAIN_A).json()["data"]["items"]]
        titles_b = [i["title"] for i in self._get(URL_LIST, DOMAIN_B).json()["data"]["items"]]
        self.assertEqual(sorted(titles_a), ["仅属于甲的第一篇", "仅属于甲的第二篇"])
        self.assertEqual(titles_b, ["仅属于乙的第一篇"])

    def test_another_tenants_detail_is_not_reachable(self):
        target_a, _article_b = self._seed()
        res = self._get(detail_url(target_a.pk), DOMAIN_B)
        self.assertEqual(res.json()["code"], 1002, "B 不该拿得到 A 的文章")
        # 反向对照：B 自己的那篇在 B 的域名下拿得到（证明上面不是「整个域名都 404」）。
        b_list = self._get(URL_LIST, DOMAIN_B).json()["data"]["items"]
        b_detail = self._get(detail_url(b_list[0]["id"]), DOMAIN_B)
        self.assertEqual(b_detail.json()["code"], 0)
        self.assertEqual(b_detail.json()["data"]["title"], "仅属于乙的第一篇")

    def test_profile_shows_its_own_studio_name(self):
        assert self.tenant.name != self.tenant_b.name
        name_a = self._get(URL_PROFILE, DOMAIN_A).json()["data"]["name"]
        name_b = self._get(URL_PROFILE, DOMAIN_B).json()["data"]["name"]
        self.assertEqual(name_a, NAME_A)
        self.assertEqual(name_b, NAME_B)


class ChannelPublicPlaneTests(TenantTestCase):
    """`public` 平面没有 `business_channelarticle` 表 ⇒ 三个端点都必须 404，不是 500。"""

    def setUp(self):
        super().setUp()
        from apps.platform.bootstrap import PLATFORM_DOMAIN, ensure_public_tenant

        ensure_public_tenant(PLATFORM_DOMAIN)
        self.anon = Client(SERVER_NAME=PLATFORM_DOMAIN)

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def test_all_three_endpoints_return_404_on_the_public_plane(self):
        for url in (URL_PROFILE, URL_LIST, detail_url(1)):
            with self.subTest(url=url):
                res = self.anon.get(url)
                self.assertEqual(res.status_code, 404, res.content[:300])
                self.assertEqual(res.json()["msg"], "当前平面没有此功能")
