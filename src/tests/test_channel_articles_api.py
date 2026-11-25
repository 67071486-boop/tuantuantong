"""频道文章的管理端 API（2026-10-23，`/api/business/articles/…` + `/api/business/channel-profile/`）。

覆盖计划 §2 的判据：节点门（无节点 403 / 有节点可 CRUD）、草稿与已发布、发布 / 下架、
`published_at` 只写一次、保存即生效、正文清洗、图片归属（同租户他人可引用 / 跨租户拒）、
媒体认领的四道校验、频道简介 ≤200。

⚠️ 断言全部打在**字面量**上（E-0024）：中文文案逐字比较，⛔ 不从生产模块 import 期望值。
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.business.models import ChannelArticle, ChannelProfile
from apps.core.storage import MAX_USER_MEDIA_BYTES, build_user_key, get_storage
from tests.p5_auth_helpers import make_logged_in_client

SCHEMA_A = "test"
DOMAIN_A = "tenant.test.com"
NODE = "system.article_manage"

URL_LIST = "/api/business/articles/"
URL_TICKET = "/api/business/articles/media/ticket"
URL_CLAIM = "/api/business/articles/media/claim"
URL_PROFILE = "/api/business/channel-profile/"

#: 合法的 PNG 头 + 一点内容（魔数嗅探只读前 8 字节）。
PNG = b"\x89PNG\r\n\x1a\n" + b"channel-article-body"
#: 认不出魔数的字节。
NOT_IMAGE = b"this is definitely not a png or jpeg"


def detail_url(pk) -> str:
    return f"/api/business/articles/{pk}/"


def publish_url(pk) -> str:
    return f"/api/business/articles/{pk}/publish/"


def unpublish_url(pk) -> str:
    return f"/api/business/articles/{pk}/unpublish/"


class _Base(TenantTestCase):
    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.storage = get_storage()
        self._keys = []

    def tearDown(self):
        for key in self._keys:
            try:
                self.storage.delete(key)
            except Exception:  # noqa: BLE001 —— 清理失败不该让用例红
                pass
        super().tearDown()

    # -- 夹具 ---------------------------------------------------------------
    def admin(self, username="chan-admin", *, nodes=(NODE,), domain=DOMAIN_A):
        """登录客户端。⚠️ `groups` 必须给：`nodes` 是挂到**该用户已有 Group 对应的 Role** 上的，
        没有 Group 就一条节点都挂不上（症状是 403，看起来像判据写错了）。"""
        return make_logged_in_client(
            domain=domain, username=username, groups=("人事",), nodes=nodes
        )

    def make_key(self, user_id, ext="png") -> str:
        """在本租户 / 指定用户名下造一个**真实对象**，返回 key（用完自动删）。"""
        key = build_user_key(SCHEMA_A, user_id, "article-media", ext)
        self._keys.append(key)
        return key

    def put(self, key, data=PNG):
        self.storage.put_bytes(key, data)

    def create_article(self, client, **payload):
        body = {"title": "默认标题", **payload}
        res = client.post(URL_LIST, body, content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content[:400])
        return res.json()["data"]

    def code(self, res):
        return res.json()["code"]

    def msg(self, res):
        return res.json()["msg"]


class ArticleNodeGateTests(_Base):
    """整个集合（**含读**）都在 `system.article_manage` 节点门上。"""

    def test_every_admin_endpoint_is_403_without_the_node(self):
        client = self.admin("chan-no-node", nodes=())
        article = ChannelArticle.objects.create(title="占位")
        cases = [
            ("get", URL_LIST),
            ("post", URL_LIST),
            ("get", detail_url(article.pk)),
            ("patch", detail_url(article.pk)),
            ("delete", detail_url(article.pk)),
            ("post", publish_url(article.pk)),
            ("post", unpublish_url(article.pk)),
            ("post", URL_TICKET),
            ("post", URL_CLAIM),
            ("get", URL_PROFILE),
            ("put", URL_PROFILE),
        ]
        for method, url in cases:
            with self.subTest(method=method, url=url):
                res = getattr(client, method)(
                    url, {}, content_type="application/json"
                )
                self.assertEqual(res.status_code, 403, res.content[:300])

    def test_holder_can_read_the_list(self):
        client = self.admin("chan-with-node")
        res = client.get(URL_LIST)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.code(res), 0)
        self.assertEqual(res.json()["data"]["items"], [])


class ArticleCrudTests(_Base):
    def setUp(self):
        super().setUp()
        self.client = self.admin()

    def test_create_defaults_to_draft_and_records_author(self):
        data = self.create_article(self.client, title="第一篇", intro="摘要")
        self.assertEqual(data["status"], "draft")
        self.assertIsNone(data["published_at"])
        self.assertEqual(data["created_by"], "chan-admin")
        self.assertEqual(data["read_count"], 0)
        self.assertEqual(ChannelArticle.objects.get(pk=data["id"]).status, "draft")

    def test_create_with_published_writes_published_at(self):
        data = self.create_article(self.client, title="首发", status="published")
        self.assertEqual(data["status"], "published")
        self.assertIsNotNone(data["published_at"])

    def test_list_only_this_tenants_articles_and_returns_the_declared_fields(self):
        self.create_article(self.client, title="甲")
        self.create_article(self.client, title="乙")
        res = self.client.get(URL_LIST)
        payload = res.json()["data"]
        self.assertEqual(payload["total"], 2)
        self.assertEqual([i["title"] for i in payload["items"]], ["乙", "甲"])
        self.assertEqual(
            set(payload["items"][0]),
            {
                "id", "title", "intro", "status", "cover_url", "read_count",
                "published_at", "created_at", "updated_at", "created_by",
            },
        )

    def test_detail_returns_content_html_and_media(self):
        key = self.make_key(999)
        html = f'<p>正文<img data-key="{key}"></p>'
        article = self.create_article(
            self.client, title="有图", content_html=html, cover_key=key
        )
        res = self.client.get(detail_url(article["id"]))
        data = res.json()["data"]
        self.assertEqual(data["content_html"], html)
        self.assertIn(key, data["media"])
        self.assertIn(key, data["cover_url"])
        self.assertIn(key, data["content_html"])

    def test_patch_saves_immediately(self):
        article = self.create_article(self.client, title="旧标题")
        res = self.client.patch(
            detail_url(article["id"]),
            {"title": "新标题", "intro": "新摘要"},
            content_type="application/json",
        )
        self.assertEqual(self.code(res), 0)
        row = ChannelArticle.objects.get(pk=article["id"])
        self.assertEqual(row.title, "新标题")
        self.assertEqual(row.intro, "新摘要")

    def test_delete_removes_the_row(self):
        article = self.create_article(self.client, title="要删")
        res = self.client.delete(detail_url(article["id"]))
        self.assertEqual(self.code(res), 0)
        self.assertFalse(ChannelArticle.objects.filter(pk=article["id"]).exists())
        res = self.client.get(detail_url(article["id"]))
        # 本仓的业务 404 = HTTP 200 + `code=1002`（见 `apps/core/exceptions.py`）。
        self.assertEqual(self.code(res), 1002)

    def test_publish_then_unpublish_keeps_published_at(self):
        article = self.create_article(self.client, title="发布后下架")
        first = self.client.post(publish_url(article["id"])).json()["data"]
        self.assertEqual(first["status"], "published")
        published_at = first["published_at"]
        self.assertIsNotNone(published_at)

        second = self.client.post(unpublish_url(article["id"])).json()["data"]
        self.assertEqual(second["status"], "draft")
        self.assertEqual(second["published_at"], published_at, "下架不许清历史值")

        third = self.client.post(publish_url(article["id"])).json()["data"]
        self.assertEqual(third["status"], "published")
        self.assertEqual(third["published_at"], published_at, "再次发布不许覆盖首次时刻")

    def test_patch_status_published_sets_it_once(self):
        article = self.create_article(self.client, title="PATCH 发布")
        first = self.client.patch(
            detail_url(article["id"]), {"status": "published"}, content_type="application/json"
        ).json()["data"]
        self.assertIsNotNone(first["published_at"])
        # 已是已发布 ⇒ 再 PATCH 保存不覆盖
        second = self.client.patch(
            detail_url(article["id"]),
            {"status": "published", "title": "改个名"},
            content_type="application/json",
        ).json()["data"]
        self.assertEqual(second["published_at"], first["published_at"])

    def test_patch_status_back_to_draft_keeps_published_at(self):
        article = self.create_article(self.client, title="回草稿", status="published")
        data = self.client.patch(
            detail_url(article["id"]), {"status": "draft"}, content_type="application/json"
        ).json()["data"]
        self.assertEqual(data["status"], "draft")
        self.assertEqual(data["published_at"], article["published_at"])

    def test_admin_get_does_not_bump_read_count(self):
        article = self.create_article(self.client, title="不计数", status="published")
        self.client.get(detail_url(article["id"]))
        self.client.get(detail_url(article["id"]))
        self.assertEqual(ChannelArticle.objects.get(pk=article["id"]).read_count, 0)


class ArticleValidationTests(_Base):
    def setUp(self):
        super().setUp()
        self.client = self.admin("chan-validate")

    def test_title_is_required_with_a_chinese_message(self):
        res = self.client.post(URL_LIST, {"intro": "x"}, content_type="application/json")
        self.assertEqual(self.code(res), 1000)
        self.assertEqual(self.msg(res), "请输入文章标题")
        res = self.client.post(URL_LIST, {"title": "   "}, content_type="application/json")
        self.assertEqual(self.msg(res), "请输入文章标题")

    def test_title_length_cap(self):
        res = self.client.post(
            URL_LIST, {"title": "字" * 81}, content_type="application/json"
        )
        self.assertEqual(self.code(res), 1000)
        self.assertEqual(self.msg(res), "标题不能超过 80 个字")

    def test_intro_length_cap(self):
        res = self.client.post(
            URL_LIST, {"title": "ok", "intro": "字" * 201}, content_type="application/json"
        )
        self.assertEqual(self.code(res), 1000)
        self.assertEqual(self.msg(res), "摘要不能超过 200 个字")

    def test_content_length_cap(self):
        res = self.client.post(
            URL_LIST,
            {"title": "ok", "content_html": "a" * 100001},
            content_type="application/json",
        )
        self.assertEqual(self.code(res), 1000)
        self.assertEqual(self.msg(res), "正文不能超过 100000 个字")

    def test_status_must_be_draft_or_published(self):
        res = self.client.post(
            URL_LIST, {"title": "ok", "status": "archived"}, content_type="application/json"
        )
        self.assertEqual(self.code(res), 1000)
        self.assertEqual(self.msg(res), "状态只能是草稿或已发布")


class ArticleContentTests(_Base):
    """正文清洗 + 图片归属（同租户他人可引用 / 跨租户拒）。"""

    def setUp(self):
        super().setUp()
        self.client = self.admin("chan-content-a")
        other = self.admin("chan-content-b")
        self.other = get_user_model().objects.get(username="chan-content-b")
        self.other_client = other

    def test_script_and_onerror_are_stripped_on_save(self):
        article = self.create_article(
            self.client,
            title="脏 HTML",
            content_html=(
                '<p onclick="x()">正文</p><script>alert(1)</script>'
                '<p><a href="javascript:alert(2)">链接</a></p>'
            ),
        )
        row = ChannelArticle.objects.get(pk=article["id"])
        self.assertEqual(row.content_html, "<p>正文</p><p>链接</p>")

    def test_colleague_in_the_same_tenant_may_reference_the_image(self):
        """业务方 2026-10-23 口径：同租户第二个管理员保存同一篇不许失败。"""
        key = self.make_key(self.other.pk)
        self.put(key)
        cover_res = self.client.post(
            URL_LIST,
            {"title": "同事的图当封面", "cover_key": key},
            content_type="application/json",
        )
        self.assertEqual(self.code(cover_res), 0, cover_res.content[:300])

        body_res = self.client.post(
            URL_LIST,
            {"title": "同事的图在正文", "content_html": f'<p><img data-key="{key}"></p>'},
            content_type="application/json",
        )
        self.assertEqual(self.code(body_res), 0, body_res.content[:300])
        self.assertIn(
            f'data-key="{key}"',
            ChannelArticle.objects.get(pk=body_res.json()["data"]["id"]).content_html,
        )

    def test_cover_key_from_another_tenant_is_rejected(self):
        foreign = build_user_key("other-tenant", 1, "article-media", "png")
        res = self.client.post(
            URL_LIST, {"title": "跨租户封面", "cover_key": foreign},
            content_type="application/json",
        )
        self.assertEqual(self.code(res), 1000)
        self.assertEqual(self.msg(res), "封面图片不属于本工作室")

    def test_cover_key_of_another_kind_is_rejected(self):
        # 头像的 key 不是文章图片（种类段必须是 `article-media.`）。
        res = self.client.post(
            URL_LIST,
            {"title": "头像当封面", "cover_key": f"{SCHEMA_A}/users/1/avatar.abc.png"},
            content_type="application/json",
        )
        self.assertEqual(self.code(res), 1000)
        self.assertEqual(self.msg(res), "封面图片不属于本工作室")

    def test_body_image_from_another_tenant_is_rejected(self):
        foreign = build_user_key("other-tenant", 1, "article-media", "png")
        res = self.client.post(
            URL_LIST,
            {"title": "跨租户正文图", "content_html": f'<p><img data-key="{foreign}"></p>'},
            content_type="application/json",
        )
        self.assertEqual(self.code(res), 1000)
        self.assertEqual(self.msg(res), "正文图片不属于本工作室")


class ArticleMediaTests(_Base):
    def setUp(self):
        super().setUp()
        self.client = self.admin("chan-media")
        self.user = get_user_model().objects.get(username="chan-media")
        self.other = self.admin("chan-media-other")
        self.other_user = get_user_model().objects.get(username="chan-media-other")

    def test_ticket_issues_a_key_under_the_current_user(self):
        res = self.client.post(
            URL_TICKET, {"ext": "png"}, content_type="application/json"
        )
        self.assertEqual(self.code(res), 0, res.content[:300])
        data = res.json()["data"]
        self.assertTrue(data["key"].startswith(f"{SCHEMA_A}/users/{self.user.pk}/article-media."))
        self.assertTrue(data["key"].endswith(".png"))
        self.assertTrue(data["upload_url"])
        self.assertGreater(data["expires_in"], 0)

    def test_ticket_rejects_other_extensions(self):
        res = self.client.post(
            URL_TICKET, {"ext": "gif"}, content_type="application/json"
        )
        self.assertEqual(self.code(res), 1000)
        self.assertEqual(self.msg(res), "只支持 jpg / jpeg / png")
        # 失败信封里**不该有 key / upload_url**（⛔ 不签发 .gif 的对象名）。
        self.assertIsNone(res.json()["data"])

    def test_claim_own_uploaded_image(self):
        key = self.make_key(self.user.pk)
        self.put(key)
        res = self.client.post(URL_CLAIM, {"key": key}, content_type="application/json")
        self.assertEqual(self.code(res), 0, res.content[:300])
        self.assertEqual(res.json()["data"]["key"], key)
        self.assertIn(key, res.json()["data"]["url"])

    def test_claim_another_users_key_in_the_same_tenant_is_403(self):
        key = self.make_key(self.other_user.pk)
        self.put(key)
        res = self.client.post(URL_CLAIM, {"key": key}, content_type="application/json")
        self.assertEqual(res.status_code, 403)
        # ⛔ 不属于自己的对象**不许删**（否则成了跨用户的删除原语）。
        self.assertIsNotNone(self.storage.head(key))

    def test_claim_a_cross_tenant_key_is_403(self):
        foreign = build_user_key("other-tenant", self.user.pk, "article-media", "png")
        res = self.client.post(URL_CLAIM, {"key": foreign}, content_type="application/json")
        self.assertEqual(res.status_code, 403)

    def test_claim_rejects_zero_byte_object_and_deletes_it(self):
        key = self.make_key(self.user.pk)
        self.put(key, b"")
        res = self.client.post(URL_CLAIM, {"key": key}, content_type="application/json")
        self.assertEqual(self.code(res), 1000)
        self.assertEqual(self.msg(res), "图片无效或为空")
        self.assertIsNone(self.storage.head(key), "被拒的空对象应当清理掉")

    def test_claim_rejects_oversized_object_and_deletes_it(self):
        key = self.make_key(self.user.pk)
        self.put(key, b"\x89PNG\r\n\x1a\n" + b"\0" * MAX_USER_MEDIA_BYTES)
        res = self.client.post(URL_CLAIM, {"key": key}, content_type="application/json")
        self.assertEqual(self.code(res), 1000)
        self.assertEqual(self.msg(res), "图片超过 10 MB")
        self.assertIsNone(self.storage.head(key))

    def test_claim_rejects_non_image_magic_and_deletes_it(self):
        key = self.make_key(self.user.pk)
        self.put(key, NOT_IMAGE)
        res = self.client.post(URL_CLAIM, {"key": key}, content_type="application/json")
        self.assertEqual(self.code(res), 1000)
        self.assertEqual(self.msg(res), "只支持 jpg / jpeg / png")
        self.assertIsNone(self.storage.head(key))

    def test_claim_rejects_a_key_that_was_never_uploaded(self):
        key = self.make_key(self.user.pk)
        res = self.client.post(URL_CLAIM, {"key": key}, content_type="application/json")
        self.assertEqual(self.code(res), 1000)
        self.assertEqual(self.msg(res), "图片还没上传成功")


class ChannelProfileTests(_Base):
    def setUp(self):
        super().setUp()
        self.client = self.admin("chan-profile")

    def test_get_returns_tenant_name_and_article_count(self):
        ChannelArticle.objects.create(title="一")
        ChannelArticle.objects.create(title="二", status=ChannelArticle.Status.PUBLISHED)
        res = self.client.get(URL_PROFILE)
        data = res.json()["data"]
        self.assertEqual(set(data), {"name", "intro", "article_count", "total_count"})
        self.assertEqual(data["intro"], "")
        # ⚠️ 业务方裁定：`article_count` **只数已发布**，与公开端**同义**
        #    （网页端右侧的手机预览显示的就是这个数）；总篇数另给 `total_count`。
        self.assertEqual(data["article_count"], 1)
        self.assertEqual(data["total_count"], 2)

    def test_put_saves_the_intro(self):
        res = self.client.put(
            URL_PROFILE, {"intro": "你好，这里是工作室"}, content_type="application/json"
        )
        self.assertEqual(self.code(res), 0)
        self.assertEqual(res.json()["data"]["intro"], "你好，这里是工作室")
        self.assertEqual(ChannelProfile.objects.get(pk=1).intro, "你好，这里是工作室")
        self.assertEqual(self.client.get(URL_PROFILE).json()["data"]["intro"], "你好，这里是工作室")

    def test_put_can_clear_the_intro(self):
        ChannelProfile.objects.create(pk=1, intro="旧简介")
        res = self.client.put(URL_PROFILE, {"intro": ""}, content_type="application/json")
        self.assertEqual(self.code(res), 0)
        self.assertEqual(ChannelProfile.objects.get(pk=1).intro, "")

    def test_put_rejects_intro_over_200(self):
        res = self.client.put(
            URL_PROFILE, {"intro": "字" * 201}, content_type="application/json"
        )
        self.assertEqual(self.code(res), 1000)
        self.assertEqual(self.msg(res), "简介不能超过 200 个字")
        self.assertFalse(ChannelProfile.objects.filter(pk=1).exists())

    def test_name_is_derived_and_never_stored(self):
        res = self.client.put(
            URL_PROFILE,
            {"name": "伪造的工作室名", "intro": "简介"},
            content_type="application/json",
        )
        self.assertEqual(self.code(res), 0)
        # `name` 不在可写列里 ⇒ 落库的只有 intro。
        self.assertEqual(ChannelProfile.objects.get(pk=1).intro, "简介")
