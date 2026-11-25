"""头像 / 微信收款码的接口用例（spec §5.2 / §7）。

⚠️ 断言打在**真实出口**上：档案表的列、磁盘上的对象（含**旧对象被删**）、以及关键判据的 403。
"""
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context

from apps.platform.bootstrap import PLATFORM_DOMAIN, ensure_public_tenant

from apps.accounts.models import StaffProfile
from apps.core.storage import get_storage
from tests.p5_auth_helpers import make_logged_in_client

EDIT_NODE = "my_profile.edit"
JPEG = b"\xff\xd8\xff" + b"avatar-body"
PNG = b"\x89PNG\r\n\x1a\n" + b"qr-body"


class ProfileMediaTests(TenantTestCase):
    def setUp(self):
        self.client = make_logged_in_client(
            username="media-me", groups=("系统管理员",), nodes=(EDIT_NODE,)
        )
        self.user = User.objects.get(username="media-me")
        self.storage = get_storage()
        self.url_ticket = "/api/auth/profile/media/avatar/ticket"
        self.url_claim = "/api/auth/profile/media/avatar"

    def _ticket(self, kind="avatar", ext="jpg"):
        return self.client.post(
            f"/api/auth/profile/media/{kind}/ticket", {"ext": ext}, format="json"
        )

    def _upload(self, key, body):
        self.storage.put_bytes(key, body, content_type="image/jpeg")

    # ── 签发 ────────────────────────────────────────────────────────
    def test_ticket_returns_key_and_upload_url(self):
        resp = self._ticket()
        self.assertEqual(resp.data["code"], 0)
        key = resp.data["data"]["key"]
        # ⚠️ 钉**真实租户前缀**（`connection.schema_name`）：初版写成
        #    `key.startswith("media-me/") or "/users/" in key` —— 左半永远 False（schema 是 `test`）、
        #    右半由 `build_user_key` 的构造必然 True ⇒ **恒真判据**，租户判据改坏也不会红
        # 。
        self.assertTrue(key.startswith(f"{connection.schema_name}/users/{self.user.pk}/"))
        self.assertIn("/api/media/blob/", resp.data["data"]["upload_url"])
        # ⚠️ 签发**不写列**：没传完时列上不该指向一个不存在的对象。
        #    注意夹具 `make_logged_in_client` 默认**已经建了档案**，所以判据是「列空」而不是「没有档案行」。
        profile = StaffProfile.objects.get(user=self.user)
        self.assertEqual(profile.avatar_key, "")

    def test_ticket_refuses_a_kind_outside_the_whitelist(self):
        resp = self._ticket(kind="passport")
        # ⛔ 白名单外 ⇒ `_field` 抛 Http404，信封按本仓口径翻成 **HTTP 200 + 业务码 1002**
                #    （⛔ 不是 HTTP 404——本行第一版写错过一次）
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["code"], 1002)

    def test_ticket_refuses_disallowed_extension(self):
        resp = self._ticket(ext="gif")
        self.assertEqual(resp.data["code"], 1000)

    # ── 认领 ────────────────────────────────────────────────────────
    def test_claim_writes_the_column_and_replaces_the_old_object(self):
        # 第一次
        first = self._ticket().data["data"]["key"]
        self._upload(first, JPEG)
        self.assertEqual(self.client.post(self.url_claim, {"key": first}, format="json").data["code"], 0)
        profile = StaffProfile.objects.get(user=self.user)
        self.assertEqual(profile.avatar_key, first)
        self.assertIsNotNone(self.storage.head(first))

        # 第二次：列换成新的，**旧对象必须被删**
        second = self._ticket(ext="png").data["data"]["key"]
        self._upload(second, PNG)
        self.assertEqual(self.client.post(self.url_claim, {"key": second}, format="json").data["code"], 0)
        profile.refresh_from_db()
        self.assertEqual(profile.avatar_key, second)
        self.assertIsNone(self.storage.head(first))

    def test_claim_refuses_bytes_that_are_not_an_image(self):
        key = self._ticket().data["data"]["key"]
        self._upload(key, b"GIF89a-not-an-image")
        resp = self.client.post(self.url_claim, {"key": key}, format="json")
        self.assertEqual(resp.data["code"], 1000)
        self.assertFalse(StaffProfile.objects.filter(user=self.user, avatar_key=key).exists())
        self.assertIsNone(self.storage.head(key))

    def test_claim_refuses_another_users_key(self):
        other = make_logged_in_client(
            username="media-other", groups=("客服",), nodes=(EDIT_NODE,)
        )
        resp = other.post("/api/auth/profile/media/avatar/ticket", {"ext": "jpg"}, format="json")
        foreign_key = resp.data["data"]["key"]
        self._upload(foreign_key, JPEG)

        # 我拿**别人**签发的 key 来认领 ⇒ 必须拒
        resp = self.client.post(self.url_claim, {"key": foreign_key}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(StaffProfile.objects.filter(user=self.user, avatar_key=foreign_key).exists())

    def test_claim_refuses_dotdot_escaped_key(self):
        """⚠️ 前缀**必须用真的租户名**起头：初版写 `media-me/...`，于是它被「首段 ≠ 本租户」拒掉，
        与 `..` 判据**无关**——把 `key_belongs_to_tenant` 的 `..` 拒绝删掉这条仍绿
        （Batch A 评审指出）。现在唯一能拒它的就是那条 `..` 规则。"""
        escaped = (
            f"{connection.schema_name}/users/{self.user.pk}/"
            "../../other-tenant/users/1/avatar.x.jpg"
        )
        resp = self.client.post(self.url_claim, {"key": escaped}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_claim_requires_the_object_to_be_uploaded_first(self):
        key = self._ticket().data["data"]["key"]
        resp = self.client.post(self.url_claim, {"key": key}, format="json")
        self.assertEqual(resp.data["code"], 1000)

    # ── 载荷 ────────────────────────────────────────────────────────
    def test_payload_exposes_signed_urls_and_no_keys(self):
        key = self._ticket().data["data"]["key"]
        self._upload(key, JPEG)
        self.client.post(self.url_claim, {"key": key}, format="json")

        data = self.client.get("/api/auth/profile").data["data"]["profile"]
        self.assertIn("/api/media/blob/", data["avatar_url"])
        self.assertIsNone(data["wechat_qr_url"])          # 没传过就是 None
        self.assertNotIn("avatar_key", data)               # ⛔ 载荷不回对象名

    # ── 顶栏头像同步线：`/api/auth/{login,me}` 的 `data.user.avatar_url` ────
    def test_user_payload_avatar_url_is_none_without_an_uploaded_avatar(self):
        """没传过 ⇒ `None`（⛔ 不是 `""`、不是 500）。壳层据此回落生成的色板头像。"""
        user = self.client.get("/api/auth/me").data["data"]["user"]
        self.assertIsNone(user["avatar_url"])

    def test_user_payload_avatar_url_is_the_signed_link_of_my_own_avatar(self):
        """传过 ⇒ 本人档案那一个对象的**短时效读链接**（取值与「我的资料」同源）。

        ⚠️ 链接是**合成形状**（本机后端的 `/api/media/blob/<key>`），⛔ 不是真 URL。
        """
        key = f"{connection.schema_name}/users/{self.user.pk}/avatar.test.png"
        self._upload(key, PNG)
        self.assertEqual(
            self.client.post(self.url_claim, {"key": key}, format="json").data["code"], 0
        )
        user = self.client.get("/api/auth/me").data["data"]["user"]
        self.assertEqual(user["avatar_url"], f"/api/media/blob/{key}")

    # ── 权限与平面 ──────────────────────────────────────────────────
    def test_node_is_required(self):
        client = make_logged_in_client(username="media-nobody", groups=("财务",))
        resp = client.post(self.url_ticket, {"ext": "jpg"}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_is_401(self):
        from django.test import Client

        resp = Client(SERVER_NAME="tenant.test.com").post(
            self.url_ticket, {"ext": "jpg"}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 401)

    def test_kind_whitelist_covers_both_columns(self):
        for kind, field in (("avatar", "avatar_key"), ("wechat-qr", "wechat_qr_key")):
            key = self._ticket(kind=kind, ext="png").data["data"]["key"]
            self._upload(key, PNG)
            resp = self.client.post(
                f"/api/auth/profile/media/{kind}", {"key": key}, format="json"
            )
            self.assertEqual(resp.data["code"], 0, msg=f"{kind} 认领失败：{resp.data}")
            profile = StaffProfile.objects.get(user=self.user)
            self.assertEqual(getattr(profile, field), key)


class ProfileMediaSizeLimitTests(TenantTestCase):
    """业务方 2026-09-25 裁定：头像 / 微信收款码上限 **10 MB**（订单截图仍是 18 MiB）。

    ⚠️ 边界判据用**字面量** `10 * 1024 * 1024`，⛔ 不引用 `MAX_USER_MEDIA_BYTES`：
    两侧同源的话，把常量改成 1 MB 这两条仍绿（本仓反复踩过的「自指断言」）。
    ⚠️ 这条闸的**权威点**在认领端点（浏览器直传不经过我们服务器 ⇒ 签发端点拿不到大小）。
    """

    #: 10 MiB 的**字面值**（业务方口径里的「10 MB」= 本仓常量 10 * 1024 * 1024）
    TEN_MIB = 10 * 1024 * 1024

    def setUp(self):
        self.client = make_logged_in_client(
            username="media-size", groups=("系统管理员",), nodes=(EDIT_NODE,)
        )
        self.user = User.objects.get(username="media-size")
        self.storage = get_storage()

    def _ticket_and_upload(self, kind, body):
        key = self.client.post(
            f"/api/auth/profile/media/{kind}/ticket", {"ext": "jpg"}, format="json"
        ).data["data"]["key"]
        self.storage.put_bytes(key, body, content_type="image/jpeg")
        return key, self.client.post(
            f"/api/auth/profile/media/{kind}", {"key": key}, format="json"
        )

    def test_exactly_10mb_avatar_is_accepted(self):
        body = b"\xff\xd8\xff" + b"0" * (self.TEN_MIB - 3)     # 恰好 10 MiB
        key, resp = self._ticket_and_upload("avatar", body)
        self.assertEqual(resp.data["code"], 0, resp.data)
        self.assertTrue(StaffProfile.objects.filter(user=self.user, avatar_key=key).exists())

    def test_one_byte_over_10mb_is_rejected_and_cleaned_up(self):
        body = b"\xff\xd8\xff" + b"0" * (self.TEN_MIB - 2)     # 10 MiB + 1 字节
        key, resp = self._ticket_and_upload("avatar", body)
        self.assertEqual(resp.data["code"], 1000, resp.data)
        self.assertFalse(StaffProfile.objects.filter(user=self.user, avatar_key=key).exists())
        self.assertIsNone(self.storage.head(key))

    def test_zero_byte_object_is_rejected_and_cleaned_up(self):
        """0 字节对象在**认领端明确拒收**（2026-09-25 复评 Minor-3）：`media_blob_views` 的
        docstring 写着「空图片永远不合法」，此前认领端只判 `size > MAX`；现在补上一条与超限
        **同码同形状**（`code=1000`、先 `delete` 对象）的判据，文案与超限分开。

        ⚠️ **为什么断言 `msg`**：不加这条判据时，0 字节对象仍会被**魔数嗅探**拒掉（读不到魔数），
        但报的是「只支持 jpg / jpeg / png」——所以「被拒 + 不留行」这半并不会因删掉
        `size <= 0` 而变红。判据必须钉在**两种拒绝的区分点**上（对外文案）才能自己的话算数。

        变异：删掉 `profile_views` 里 `if size <= 0:` 那三行 ⇒ 走嗅探分支 ⇒ `msg` 变成
        「只支持 jpg / jpeg / png」⇒ 本条以 msg 不等变红（已实测）。
        """
        key, resp = self._ticket_and_upload("avatar", b"")
        self.assertEqual(resp.data["code"], 1000, resp.data)
        self.assertEqual(resp.data["msg"], "图片无效或为空")
        self.assertFalse(StaffProfile.objects.filter(user=self.user, avatar_key=key).exists())
        self.assertIsNone(self.storage.head(key))

    def test_the_same_10mb_ceiling_applies_to_the_wechat_qr(self):
        """收款码与头像**同一条**闸（不是只有头像收紧）。"""
        body = b"\xff\xd8\xff" + b"0" * (self.TEN_MIB - 2)
        key, resp = self._ticket_and_upload("wechat-qr", body)
        self.assertEqual(resp.data["code"], 1000, resp.data)
        self.assertFalse(StaffProfile.objects.filter(user=self.user, wechat_qr_key=key).exists())
        self.assertIsNone(self.storage.head(key))


class ProfileMediaOnThePublicPlaneTests(TenantTestCase):
    """平台平面（`public`）没有 `StaffProfile` ⇒ 明确 404（spec §5.6；brief Step 1②）。

    骨架照 `tests/test_my_profile_api.py::MyProfileOnThePublicPlaneTests`。
    """

    def setUp(self):
        super().setUp()
        ensure_public_tenant(PLATFORM_DOMAIN)
        with schema_context("public"):
            User.objects.create_superuser(
                username="media-pub-super", email="", password="pw-media-pub"
            )
        self.client = Client(SERVER_NAME=PLATFORM_DOMAIN)
        login = self.client.post(
            "/api/auth/login",
            data={"username": "media-pub-super", "password": "pw-media-pub"},
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200, login.content[:200])

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def test_authenticated_media_endpoints_are_404_not_500(self):
        for url in ("/api/auth/profile/media/avatar/ticket", "/api/auth/profile/media/avatar"):
            res = self.client.post(url, {"ext": "jpg"}, content_type="application/json")
            self.assertEqual({url: res.status_code}, {url: 404})
            self.assertEqual({url: res.json()["msg"]}, {url: "当前平面没有此功能"})
