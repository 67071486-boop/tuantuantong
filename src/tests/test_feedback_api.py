"""问题反馈接口用例（spec `2026-10-08-feedback-design.md` §9 的判据 1/2/4/5/6/7）。

⚠️ 本文件的夹具值一律是**合成值**（PII 守卫扫 `src/**`）：正文、备注、昵称都自造。

⚠️ **每个 client 用不同的组**：夹具把节点挂在「该用户所属 Group 对应的 `Role`」上，而 `Role` 是
**组级共享**的——同一组里再建第二个用户会**继承**前一个用户挂上的节点（「无权限」用例会假失败）；
只给 `nodes` 不给 `groups` ⇒ 循环体一次都不进 ⇒ 一个节点都没挂上（症状是 403）。
本文件因此按角色分组：`客服` / `客服主管` / `代练员` / `系统管理员`。

⚠️ 「响应是不是失败信封」一律看 `response.data["code"]`，⛔ 不看 `response.data`
（`api_response()` 永远返回信封 ⇒ 那样写是**恒真判据**，E-0063）。

⚠️ 断言打在**真实出口**上：数据库行（`Feedback` / `FeedbackImage` / `OperationLog`）与
存储层对象（`get_storage().head(key)`），⛔ 不断言 logger。
"""
import json
from datetime import datetime, timezone as dt_timezone

from django.contrib.auth.models import User
from django.db import connection
from django.db.models.deletion import ProtectedError
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import Feedback, FeedbackImage
from apps.core.storage import get_storage
from tests.p5_auth_helpers import make_logged_in_client

NODE = "home.feedback"
TENANT_DOMAIN = "tenant.test.com"

URL_LIST = "/api/business/feedback/"
URL_TICKET = "/api/business/feedback/media/ticket/"


def detail_url(pk) -> str:
    return f"/api/business/feedback/{pk}/"


#: 真的图片字节（魔数判据：JPEG `FF D8 FF` / PNG `89 50 4E 47 0D 0A 1A 0A`）。
JPEG = b"\xff\xd8\xff" + b"feedback-jpeg-body"
PNG = b"\x89PNG\r\n\x1a\n" + b"feedback-png-body"
#: **非**图片：改名成 `.png` 的文本（spec §9 判据 5 的形状）。
TEXT_AS_PNG = b"this is not an image at all"


def _client(username, group, *nodes):
    """一个已登录 client：`group` 决定它属于哪个组（⛔ 每个 client 换一个组，见模块 docstring）。"""
    return make_logged_in_client(
        domain=TENANT_DOMAIN, username=username, groups=(group,), nodes=nodes
    )


class FeedbackTestCase(TenantTestCase):
    """公共夹具：本租户 schema 名 + 四个角色各一个 client。"""

    def setUp(self):
        self.schema = connection.schema_name
        self.storage = get_storage()
        # 普通员工（持节点）：客服、客服主管各一个。
        self.client_a = _client("fb-reporter-a", "客服", NODE)
        self.user_a = User.objects.get(username="fb-reporter-a")
        self.client_b = _client("fb-reporter-b", "客服主管", NODE)
        self.user_b = User.objects.get(username="fb-reporter-b")
        # 管理员（持「系统管理员」职位 ⇒ `is_tenant_admin` 为真）。
        self.admin = _client("fb-admin", "系统管理员", NODE)
        self.admin_user = User.objects.get(username="fb-admin")
        # 持**别的**节点的普通员工：用来证节点门真的是门（他只持 order.list，不持本点）。
        self.outsider = _client("fb-outsider", "代练员", "order.list")

    def my_key(self, user, name="feedback.aaaabbbbcccc.png") -> str:
        """本租户 + 该用户目录下的一个对象名（形状与 `build_user_key` 同源）。"""
        return f"{self.schema}/users/{user.pk}/{name}"

    def ticket_key(self, ext="png") -> str:
        body = self.client_a.post(
            URL_TICKET, {"ext": ext}, content_type="application/json"
        ).data
        return body["data"]["key"]

    def upload(self, key, body=PNG) -> None:
        self.storage.put_bytes(key, body, content_type="image/png")

    def submit(self, content="测试反馈正文", keys=(), client=None):
        return (client or self.client_a).post(
            URL_LIST,
            {"content": content, "image_keys": list(keys)},
            content_type="application/json",
        )


class FeedbackNodeGateTests(FeedbackTestCase):
    """节点门：三个端点都吃 `home.feedback`，没这个点一律 403（fail-closed）。"""

    def test_list_without_the_node_is_403(self):
        resp = self.outsider.get(URL_LIST)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data["code"], 403)
        self.assertEqual(resp.data["msg"], "当前职位没有此操作所需的权限节点")

    def test_ticket_without_the_node_is_403(self):
        resp = self.outsider.post(URL_TICKET, {"ext": "png"}, content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data["code"], 403)

    def test_submit_without_the_node_is_403_and_writes_nothing(self):
        resp = self.outsider.post(
            URL_LIST, {"content": "越权提交", "image_keys": []}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Feedback.objects.count(), 0)

    def test_anonymous_is_401(self):
        from django.test import Client

        resp = Client(SERVER_NAME=TENANT_DOMAIN).get(URL_LIST)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.data["code"], 401)


class FeedbackTicketTests(FeedbackTestCase):
    """`POST /api/business/feedback/media/ticket/`（spec §4.1）。"""

    def test_ticket_returns_a_key_under_my_own_directory(self):
        body = self.client_a.post(
            URL_TICKET, {"ext": "png"}, content_type="application/json"
        ).data
        self.assertEqual(body["code"], 0)
        key = body["data"]["key"]
        # 前缀钉「本租户 + 本用户 + feedback 段」（⛔ 不是「含 users 子串」那种恒真判据）。
        self.assertTrue(key.startswith(f"{self.schema}/users/{self.user_a.pk}/feedback."))
        self.assertRegex(key, rf"^{self.schema}/users/\d+/feedback\.[0-9a-f]{{12}}\.png$")
        # 本机实现的 presign 指向后端自己的 blob 端点。
        self.assertEqual(body["data"]["upload_url"], f"/api/media/blob/{key}")
        self.assertEqual(body["data"]["expires_in"], 600)
        # ⛔ 只签发、不写库。
        self.assertEqual(Feedback.objects.count(), 0)
        self.assertEqual(FeedbackImage.objects.count(), 0)

    def test_ticket_normalizes_the_extension(self):
        body = self.client_a.post(
            URL_TICKET, {"ext": ".PNG"}, content_type="application/json"
        ).data
        self.assertEqual(body["code"], 0)
        self.assertTrue(body["data"]["key"].endswith(".png"))

    def test_ticket_refuses_a_disallowed_extension(self):
        """不在 `jpg`/`jpeg`/`png` 里的扩展名 ⇒ `1000` + **与既有媒体线同一句**文案。

        ⚠️ 三条形状走**同一句话**（⛔ 不新增第二种说法）：白名单外、空串、**超长**。
        空串走的是视图里 `ALLOWED_EXTS` 那条判据（`ext` 去点、小写后不在白名单里），
        ⛔ 不是「缺少参数」——这与 `MyProfileMediaTicketView` 的口径一致。
        """
        for ext in ("gif", "exe", "svg", "", "pnggggggggggg"):
            with self.subTest(ext=ext):
                body = self.client_a.post(
                    URL_TICKET, {"ext": ext}, content_type="application/json"
                ).data
                self.assertEqual(body["code"], 1000)
                self.assertEqual(body["msg"], "只支持 jpg / jpeg / png")

    def test_ticket_requires_the_extension_field(self):
        body = self.client_a.post(URL_TICKET, {}, content_type="application/json").data
        self.assertEqual(body["code"], 1000)
        self.assertEqual(body["msg"], "缺少图片格式")


class FeedbackSubmitTests(FeedbackTestCase):
    """`POST /api/business/feedback/`（spec §4.3 / §9 的判据 4/5/6）。"""

    def test_submit_without_images_writes_one_row(self):
        resp = self.submit(content="点发单价格报错")
        self.assertEqual(resp.data["code"], 0)
        row = Feedback.objects.get()
        self.assertEqual(resp.data["data"], {"id": row.pk})
        self.assertEqual(row.content, "点发单价格报错")
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.admin_note, "")
        self.assertEqual(row.created_by_id, self.user_a.pk)
        self.assertIsNone(row.handled_by_id)
        self.assertIsNone(row.handled_at)
        self.assertEqual(FeedbackImage.objects.count(), 0)

    def test_submit_with_three_images_writes_three_rows(self):
        keys = []
        for index in range(3):
            key = self.ticket_key()
            self.upload(key, PNG + bytes([index]))
            keys.append(key)
        resp = self.submit(content="带三张图", keys=keys)
        self.assertEqual(resp.data["code"], 0)
        row = Feedback.objects.get()
        self.assertEqual(
            [image.object_key for image in row.images.all()], keys
        )

    def test_submit_with_four_images_is_refused_and_writes_nothing(self):
        """判据 6：4 张 ⇒ 被拒，`Feedback` 表**零新增行**（连带那张图的行也没有）。"""
        keys = []
        for _ in range(4):
            key = self.ticket_key()
            self.upload(key)
            keys.append(key)
        resp = self.submit(content="四张图", keys=keys)
        self.assertEqual(resp.data["code"], 1000)
        self.assertEqual(resp.data["msg"], "一条反馈最多上传3张截图")
        self.assertEqual(Feedback.objects.count(), 0)
        self.assertEqual(FeedbackImage.objects.count(), 0)
        # spec §4.3 第 6 条：这一次涉及的对象也要清掉（否则留下四个孤儿）。
        for key in keys:
            self.assertIsNone(self.storage.head(key), key)

    def test_submit_refuses_blank_content(self):
        for content in ("", "   ", "\n\t "):
            with self.subTest(content=repr(content)):
                resp = self.submit(content=content)
                self.assertEqual(resp.data["code"], 1000)
                self.assertEqual(resp.data["msg"], "请填写反馈内容")
                self.assertEqual(Feedback.objects.count(), 0)

    def test_submit_refuses_a_key_under_another_users_directory(self):
        """别的用户目录下的 key ⇒ 403、不落库，且那个对象**仍然在**（⛔ 不许被删）。

        ⚠️ **这条判据是本线改准过的**（红队 + 总指挥复跑实测的跨用户删除原语）：旧口径
        （spec 原文 §9 判据 4）要求把别人的对象**删掉**，那会把提交端点变成「任何持
        `home.feedback` 的人都能删本租户任意对象」的口子——别人的头像 / 微信收款码 /
        订单截图都在射程内，而那些 key 在页面上是回显的。
        ⇒ 现在只清**调用者自己**的反馈截图（`feedback_views.is_my_feedback_key`），
        误传的别人的 key 原样留着（代价：可能留一个孤儿对象）。
        """
        foreign_key = self.my_key(self.user_b, "feedback.deadbeef0000.png")
        self.upload(foreign_key)
        self.assertIsNotNone(self.storage.head(foreign_key))   # 自证对象真的在

        resp = self.submit(content="带上别人的图", keys=[foreign_key])

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data["code"], 403)
        self.assertEqual(resp.data["msg"], "截图对象名不属于当前用户")
        self.assertEqual(Feedback.objects.count(), 0)
        self.assertEqual(FeedbackImage.objects.count(), 0)
        self.assertIsNotNone(
            self.storage.head(foreign_key),
            "别人的对象被删掉了——清理判据又放宽回「本租户」了（跨用户删除原语）",
        )

    def test_cleanup_drops_only_my_own_feedback_objects(self):
        """**阳性对照 + 阴性对照**：同一个请求里，只有「本人的反馈截图」被清掉，别的都不动。

        没有阳性那一半，上面那条可以靠「清理整个失效」满足——那是一次静默回归（孤儿对象
        没人收、也没人会发现）。三类对象**都放进请求里**（不放进请求的 key 本来就不会被遍历，
        那样断言是恒真的）：

        | 请求里的对象 | 期望 |
        |---|---|
        | 本人的反馈截图（`<schema>/users/<我>/feedback.…`） | **被删**（阳性对照） |
        | **本人的头像**（`<schema>/users/<我>/avatar.…`，同前缀、不同种类段） | 保留 |
        | 别人的反馈截图（`<schema>/users/<别人>/feedback.…`） | 保留 |

        ⚠️ 第二行是红队那条缺陷的钉子（旧判据只到「本租户」⇒ 连**自己的头像**都会被删）；
        别人的对象那一条见 `tests/test_feedback_isolation.py` 的跨租户用例（key 也在请求里）。
        """
        mine = self.ticket_key()
        self.upload(mine)
        my_avatar = self.my_key(self.user_a, "avatar.444455556666.png")
        self.upload(my_avatar)
        someone_elses = self.my_key(self.user_b, "feedback.111122223333.png")
        self.upload(someone_elses)
        for key in (mine, my_avatar, someone_elses):
            self.assertIsNotNone(self.storage.head(key), key)   # 自证三个对象都在

        # 3 个 key（去重后仍是 3 ⇒ 不触发数量判据）；第 1 个不是本人的反馈截图
        # ⇒ 走 403 分支，而清理就在这条路径上发生（这正是缺陷的形状：先清理、再 403）。
        resp = self.submit(content="混着来", keys=[my_avatar, mine, someone_elses])

        self.assertEqual(resp.status_code, 403, resp.data)
        self.assertEqual(Feedback.objects.count(), 0)
        self.assertEqual(FeedbackImage.objects.count(), 0)
        self.assertIsNone(self.storage.head(mine), "本人的反馈截图应当被清掉（阳性对照）")
        self.assertIsNotNone(self.storage.head(my_avatar), "本人的头像不许被删（同前缀不同段）")
        self.assertIsNotNone(self.storage.head(someone_elses), "别人的反馈截图不许被删")

    def test_a_rejected_batch_drops_every_own_object_in_it(self):
        """**整批一起清**：本租户自己的 2 张有效图 + 1 张自己的坏图 ⇒ 零新增行，**两张都清掉**。

        ⚠️ 评审 I-3：只带一个 key 的用例钉不到这条主路径——把清理改成「只删坏的那一个」
        或「漏删」都不会红。这里三个 key（去重后仍是 3）走的是**魔数**拒绝分支。
        """
        first = self.ticket_key(ext="png")
        self.upload(first)
        second = self.ticket_key(ext="jpg")
        self.upload(second, JPEG)
        bad = self.ticket_key(ext="png")
        self.upload(bad, TEXT_AS_PNG)          # 自己的图、但不是图片
        for key in (first, second, bad):
            self.assertIsNotNone(self.storage.head(key), key)

        resp = self.submit(content="三张里有一张是坏的", keys=[first, second, bad])

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["code"], 1000)
        self.assertEqual(resp.data["msg"], "只支持 jpg / jpeg / png")
        self.assertEqual(Feedback.objects.count(), 0)
        self.assertEqual(FeedbackImage.objects.count(), 0)
        self.assertIsNone(self.storage.head(first), "自己这一批的第一张没被清")
        self.assertIsNone(self.storage.head(second), "自己这一批的第二张没被清")
        self.assertIsNone(self.storage.head(bad))

    def test_a_rejected_batch_keeps_the_foreign_object(self):
        """同一批里混一个**别人的**对象 ⇒ 自己的三张全清、**别人的原样还在**、零新增行。

        ⚠️ 这条走的是**数量**拒绝分支（4 个 key），正好覆盖「清理发生在**另一条**拒绝路径
        上」这种漏点（评审 I-4：每条拒绝路径都要么都清、要么都不清）。
        """
        mine_keys = []
        for index in range(3):
            key = self.ticket_key()
            self.upload(key, PNG + bytes([index]))
            mine_keys.append(key)
        someone_elses = self.my_key(self.user_b, "feedback.aaaabbbbcccc.png")
        self.upload(someone_elses)

        resp = self.submit(content="三张自己的加一张别人的", keys=[*mine_keys, someone_elses])

        self.assertEqual(resp.data["code"], 1000)
        self.assertEqual(resp.data["msg"], "一条反馈最多上传3张截图")
        self.assertEqual(Feedback.objects.count(), 0)
        self.assertEqual(FeedbackImage.objects.count(), 0)
        for key in mine_keys:
            self.assertIsNone(self.storage.head(key), key)
        self.assertIsNotNone(
            self.storage.head(someone_elses),
            "别人的对象被删掉了——清理判据又放宽回「本租户」了（跨用户删除原语）",
        )

    def test_the_same_key_twice_lands_one_row(self):
        """同一次提交里重复同一个 key ⇒ **一行**（去重保持顺序）。

        不去重会落两行同 key，而列表载荷会把同一张图渲染两遍——页面上看不出这是重复数据。
        """
        key = self.ticket_key()
        self.upload(key)
        resp = self.submit(content="同一张图给两次", keys=[key, key])
        self.assertEqual(resp.data["code"], 0)
        row = Feedback.objects.get()
        self.assertEqual([image.object_key for image in row.images.all()], [key])
        self.assertIsNotNone(self.storage.head(key))

    def test_four_identical_keys_are_not_treated_as_four_images(self):
        """去重**先于**数量判据：4 个相同的 key 是**一张图**，不按「4 张」拒。"""
        key = self.ticket_key()
        self.upload(key)
        resp = self.submit(content="四个相同", keys=[key, key, key, key])
        self.assertEqual(resp.data["code"], 0, resp.data)
        self.assertEqual(FeedbackImage.objects.count(), 1)

    def test_submit_refuses_a_key_that_was_never_uploaded(self):
        key = self.ticket_key()
        resp = self.submit(content="图没传上来", keys=[key])
        self.assertEqual(resp.data["code"], 1000)
        self.assertEqual(resp.data["msg"], "截图还没上传成功")
        self.assertEqual(Feedback.objects.count(), 0)

    def test_submit_refuses_non_image_bytes_renamed_to_png(self):
        """判据 5：改名成 `.png` 的文本 ⇒ 被拒（**魔数**判据，⛔ 不看客户端 `Content-Type`）。"""
        key = self.ticket_key(ext="png")
        self.upload(key, TEXT_AS_PNG)
        resp = self.submit(content="假图", keys=[key])
        self.assertEqual(resp.data["code"], 1000)
        self.assertEqual(resp.data["msg"], "只支持 jpg / jpeg / png")
        self.assertEqual(Feedback.objects.count(), 0)
        self.assertIsNone(self.storage.head(key))

    def test_submit_refuses_an_empty_object(self):
        key = self.ticket_key()
        self.upload(key, b"")
        self.assertEqual(self.storage.head(key), (0, "image/png"))   # 自证：0 字节对象真的在
        resp = self.submit(content="空图", keys=[key])
        self.assertEqual(resp.data["code"], 1000)
        self.assertEqual(resp.data["msg"], "图片无效或为空")
        self.assertEqual(Feedback.objects.count(), 0)
        self.assertIsNone(self.storage.head(key))

    def test_submit_refuses_an_image_one_byte_over_10mb(self):
        """单张上限 = **10 MB**（业务方 2026-10-08 裁定）：10 MiB + 3 字节 ⇒ 拒。"""
        key = self.ticket_key(ext="jpg")
        self.upload(key, b"\xff\xd8\xff" + b"0" * (10 * 1024 * 1024))
        self.assertGreater(self.storage.head(key)[0], 10 * 1024 * 1024)   # 自证超限
        resp = self.submit(content="超大图", keys=[key])
        self.assertEqual(resp.data["code"], 1000)
        self.assertEqual(resp.data["msg"], "图片超过 10 MB")
        self.assertEqual(Feedback.objects.count(), 0)
        self.assertIsNone(self.storage.head(key))


class FeedbackListTests(FeedbackTestCase):
    """`GET /api/business/feedback/`（spec §4.2 / §9 的判据 1）。"""

    def setUp(self):
        super().setUp()
        self.row_a = Feedback.objects.create(content="测试反馈甲", created_by=self.user_a)
        self.row_b = Feedback.objects.create(content="测试反馈乙", created_by=self.user_b)

    def test_non_admin_sees_only_her_own_row(self):
        body = self.client_a.get(URL_LIST).data
        self.assertEqual(body["code"], 0)
        self.assertEqual(
            [item["content"] for item in body["data"]["items"]], ["测试反馈甲"]
        )

    def test_the_other_non_admin_sees_only_her_own_row(self):
        """反方向对照：同一个断言换个人再判一次（防「恰好都看得见」被读成隔离）。"""
        body = self.client_b.get(URL_LIST).data
        self.assertEqual(
            [item["content"] for item in body["data"]["items"]], ["测试反馈乙"]
        )

    def test_admin_sees_both_rows(self):
        """反例对照：管理员看得见**两个不同用户**各自那一行。

        ⚠️ 判据钉的是 **(正文, 提交人显示名) 两列**，⛔ 不只是条数（评审 I-5）：
        只钉 `total` / 长度的话，「管理员也被收窄成只看自己」这类误改在**两行数据**下
        条数照样是 2、**根本不会红**。两列一起钉才证明这两行来自**两个人**。
        """
        body = self.admin.get(URL_LIST).data
        self.assertEqual(
            sorted(
                (item["content"], item["created_by_name"])
                for item in body["data"]["items"]
            ),
            sorted(
                [("测试反馈乙", "fb-reporter-b"), ("测试反馈甲", "fb-reporter-a")]
            ),
        )
        self.assertEqual(body["data"]["total"], 2)

    def test_row_payload_shape(self):
        self.user_a.first_name = "测试昵称甲"
        self.user_a.save(update_fields=["first_name"])
        key = self.my_key(self.user_a, "feedback.abcd1234ef56.png")
        self.upload(key)
        FeedbackImage.objects.create(feedback=self.row_a, object_key=key)

        item = self.client_a.get(URL_LIST).data["data"]["items"][0]

        self.assertEqual(item["id"], self.row_a.pk)
        self.assertEqual(item["content"], "测试反馈甲")
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["status_label"], "未处理")
        self.assertEqual(item["admin_note"], "")
        self.assertEqual(item["created_by_name"], "测试昵称甲")
        self.assertRegex(item["created_at"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertIsNone(item["handled_by_name"])
        self.assertIsNone(item["handled_at"])
        self.assertEqual(
            item["images"], [{"key": key, "url": f"/api/media/blob/{key}"}]
        )

    def test_created_at_is_rendered_in_the_project_timezone(self):
        """E-0238：提交/处理时间是**项目时区**（`+08:00`）的墙钟，不是库里的 UTC。

        根因：桌面反馈页按「串已是 +08:00」切片显示（`feedback.ts::formatTime`），
        后端此前裸 `.isoformat()` 出 UTC 串 ⇒ 页面整整**差 8 小时**（业务方报）。
        判据打在**页面拿到的载荷**上：UTC 01:00 必须渲染成 09:00+08:00。
        """
        self.submit()
        Feedback.objects.all().update(
            created_at=datetime(2026, 10, 12, 1, 0, 0, tzinfo=dt_timezone.utc)
        )
        Feedback.objects.all().update(
            handled_at=datetime(2026, 10, 12, 3, 0, 0, tzinfo=dt_timezone.utc)
        )
        item = self.admin.get(URL_LIST).data["data"]["items"][0]
        self.assertEqual(item["created_at"], "2026-10-12T09:00:00+08:00")
        self.assertEqual(item["handled_at"], "2026-10-12T11:00:00+08:00")


class FeedbackHandleTests(FeedbackTestCase):
    """`PATCH /api/business/feedback/<int:pk>/`（spec §4.4 / §8 / §9 的判据 2）。"""

    def setUp(self):
        super().setUp()
        self.row = Feedback.objects.create(content="测试反馈丙", created_by=self.user_b)

    def patch(self, pk, payload, client=None):
        return (client or self.admin).patch(
            detail_url(pk), payload, content_type="application/json"
        )

    def test_non_admin_is_403_and_the_row_is_untouched(self):
        """判据 2：非管理员 ⇒ **403**，且库里那行**一个字没变**（断言数据库行，不断言 logger）。"""
        resp = self.patch(self.row.pk, {"status": "handled", "admin_note": "越权改"}, self.client_a)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data["code"], 403)
        self.assertEqual(resp.data["msg"], "只有系统管理员可以处理反馈")

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "pending")
        self.assertEqual(self.row.admin_note, "")
        self.assertIsNone(self.row.handled_by_id)
        self.assertIsNone(self.row.handled_at)
        # 而且**一条日志都没写**（副作用打在真实出口上）。
        self.assertEqual(OperationLog.objects.filter(action="feedback.handle").count(), 0)

    def test_admin_marks_handled_and_the_submitter_sees_it(self):
        resp = self.patch(self.row.pk, {"status": "handled", "admin_note": "已记录，下版修复"})
        self.assertEqual(resp.data["code"], 0)
        self.assertEqual(resp.data["data"], {"id": self.row.pk, "status": "handled"})

        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "handled")
        self.assertEqual(self.row.admin_note, "已记录，下版修复")
        self.assertEqual(self.row.handled_by_id, self.admin_user.pk)
        self.assertIsNotNone(self.row.handled_at)

        # spec §4.2：**提交人自己**那一行也照常带着备注与处理人。
        item = self.client_b.get(URL_LIST).data["data"]["items"][0]
        self.assertEqual(item["status_label"], "已处理")
        self.assertEqual(item["admin_note"], "已记录，下版修复")
        self.assertEqual(item["handled_by_name"], "fb-admin")
        self.assertIsNotNone(item["handled_at"])

    def test_handle_writes_one_operation_log_row(self):
        self.patch(self.row.pk, {"status": "handled", "admin_note": "备注乙"})
        log = OperationLog.objects.get(action="feedback.handle")
        self.assertEqual(log.log_type, 40)
        self.assertEqual(log.actor_id, self.admin_user.pk)
        self.assertEqual(log.target_kind, "feedback")
        self.assertEqual(log.target_id, str(self.row.pk))
        self.assertEqual(log.to_value, "已处理")
        self.assertEqual(log.reason, "备注乙")
        self.assertEqual(log.summary, f"【fb-admin】处理了问题反馈 {self.row.pk}，处理状态：已处理（原因：备注乙）")

    def test_reverting_to_pending_clears_the_operator_and_the_timestamp(self):
        self.patch(self.row.pk, {"status": "handled", "admin_note": "先标已处理"})
        resp = self.patch(self.row.pk, {"status": "pending"})
        self.assertEqual(resp.data["data"], {"id": self.row.pk, "status": "pending"})
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "pending")
        self.assertIsNone(self.row.handled_by_id)
        self.assertIsNone(self.row.handled_at)
        # 只改状态时备注**不动**（两个字段各自独立，spec §4.4）。
        self.assertEqual(self.row.admin_note, "先标已处理")

    def test_admin_can_write_the_note_without_changing_the_status(self):
        resp = self.patch(self.row.pk, {"admin_note": "只写备注"})
        self.assertEqual(resp.data["code"], 0)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "pending")
        self.assertEqual(self.row.admin_note, "只写备注")

    def test_patch_with_no_field_is_refused(self):
        resp = self.patch(self.row.pk, {})
        self.assertEqual(resp.data["code"], 1000)
        self.assertEqual(resp.data["msg"], "请至少给出 status 或 admin_note")
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "pending")

    def test_patch_with_an_unknown_status_is_refused(self):
        resp = self.patch(self.row.pk, {"status": "closed"})
        self.assertEqual(resp.data["code"], 1000)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "pending")

    def test_patch_on_a_missing_pk_is_404(self):
        resp = self.patch(9999, {"status": "handled"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["code"], 1002)
        self.assertEqual(resp.data["msg"], "记录不存在或已被删除")


class FeedbackProtectionTests(FeedbackTestCase):
    """两个外键的删除语义（spec §3 / §9 的判据 7）。"""

    def test_hard_deleting_a_reporter_raises_protected_error(self):
        """判据 7：硬删一个提过反馈的用户 ⇒ `ProtectedError`（不是静默成功、不是 500）。"""
        row = Feedback.objects.create(content="删除保护用例", created_by=self.user_a)
        with self.assertRaises(ProtectedError):
            self.user_a.delete()
        self.assertTrue(Feedback.objects.filter(pk=row.pk).exists())

    def test_deleting_the_handler_keeps_the_row_and_nulls_the_operator(self):
        """`handled_by` 是 `SET_NULL`：处理人走人 ⇒ 反馈留着、处理人清空（它是审计痕迹）。"""
        row = Feedback.objects.create(content="处理人走人", created_by=self.user_b)
        self.patch(row, {"status": "handled"})
        self.admin_user.delete()
        row.refresh_from_db()
        self.assertEqual(row.status, "handled")
        self.assertIsNone(row.handled_by_id)

    def patch(self, row, payload):
        return self.admin.patch(
            detail_url(row.pk), payload, content_type="application/json"
        )


class FeedbackMalformedInputTests(FeedbackTestCase):
    """畸形入参**不 500**（`AGENTS.md` §5-E：判据是「这类请求还会不会 500」）。"""

    #: 各种「不是对象」的 JSON 体：数组 / 字符串 / 数字 / null / 对象里套对象。
    BAD_BODIES = ("[1, 2]", '"just-a-string"', "5", "null", '{"content": {"a": 1}}')

    def _post_raw(self, url, raw):
        return self.client_a.post(url, data=raw, content_type="application/json")

    def test_malformed_bodies_do_not_500(self):
        for raw in self.BAD_BODIES:
            with self.subTest(url="list", raw=raw):
                resp = self._post_raw(URL_LIST, raw)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.data["code"], 1000)
            with self.subTest(url="ticket", raw=raw):
                resp = self._post_raw(URL_TICKET, raw)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.data["code"], 1000)

    def test_malformed_image_keys_do_not_500(self):
        for raw in ("[1, 2]", '"not-an-array"', '{"a": 1}', "[[]]", "[null]"):
            with self.subTest(image_keys=raw):
                body = json.dumps({"content": "畸形截图清单", "image_keys": json.loads(raw)})
                resp = self.client_a.post(
                    URL_LIST, body, content_type="application/json"
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.data["code"], 1000)
                self.assertEqual(Feedback.objects.count(), 0)

    def test_malformed_patch_bodies_do_not_500(self):
        row = Feedback.objects.create(content="畸形 PATCH", created_by=self.user_a)
        for raw in ("[1, 2]", '"handled"', "null", '{"status": {"a": 1}}', '{"status": 3}'):
            with self.subTest(raw=raw):
                resp = self.admin.patch(
                    detail_url(row.pk), raw, content_type="application/json"
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.data["code"], 1000)
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")

    def test_malformed_query_parameters_do_not_500(self):
        """列表端点不吃查询参数 ⇒ 畸形参数照旧 200 + 信封（结构网 `test_no_route_500` 同款）。

        ⚠️ 语料与那张结构网的 `MALFORMED_QUERY` 同一口径：**语法合法、值畸形**。
        """
        resp = self.client_a.get(
            URL_LIST,
            {"game": "abc", "page": "abc", "status": "BOGUS", "is_urgent": "maybe"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["code"], 0)
