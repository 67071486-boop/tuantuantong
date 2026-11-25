"""首页二维码横幅（D-0262，2026-10-19 实装）的接口与判据。

关键口径（`apps/business/api/qr_code_views.py` 的模块 docstring 是权威）：
- 读列表 = 登录即可；**按持点分档**——持 `home.qr_manage` 见全部（含停用），不持只见启用；
- 写（POST / PUT / PATCH / DELETE）与直传票据 = 登录 + `home.qr_manage`；
- `qq_bot` / `wechat_mp` 每租户至多一条：API 层回 `ErrorCode.CONFLICT`（1001），
  库层 `UniqueConstraint(condition=…)` 兜底；
- 删除是**硬删**（照 `SystemNotice` 先例），存储对象不跟着删（孤儿对象）；
- `media_key` 五条校验：本租户（403 越权）、`home-qr` 种类段、对象存在、非 0 字节、魔数。

夹具值一律**合成值**（真值守卫只扫 `src/**`）。
"""
from unittest import mock

from django.db import connection
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import Game, HomeQrCode
from apps.core.storage import Presigned, get_storage
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

NODE = "home.qr_manage"
URL_LIST = "/api/business/home/qr-codes/"
URL_TICKET = "/api/business/home/qr-codes/media/ticket/"

#: 单租户用例里的本租户 key（种类段 = `home-qr`、首段 = 本租户 schema）。
KEY_A = "test/users/11/home-qr.111122223333.png"
#: 单租户用例里的**第二把本租户 key**（同租户、同种类段，只是随机段不同）。
KEY_LOCAL_B = "test/users/11/home-qr.aaaabbbbcccc.png"
#: 租户乙命名空间下的 key（**只**给跨租户隔离用例当判据值——在本租户用例里它是越权形状）。
KEY_B = "iso_qr_b/users/22/home-qr.aaaabbbbcccc.png"

PNG = b"\x89PNG\r\n\x1a\n" + b"home-qr-body"


def detail_url(pk) -> str:
    return f"/api/business/home/qr-codes/{pk}/"


def _put_image(key: str) -> None:
    get_storage().put_bytes(key, PNG, content_type="image/png")


class HomeQrApiTests(TenantTestCase):
    """权限分档 + CRUD + 唯一约束 + 排序（单租户内）。"""

    def setUp(self):
        self.manager = make_logged_in_client(
            username="qr-manager", groups=("人事",), nodes=(NODE,)
        )
        self.viewer = make_logged_in_client(username="qr-viewer")
        self.game_a = Game.objects.create(name="横幅游戏甲")
        self.game_b = Game.objects.create(name="横幅游戏乙")
        _put_image(KEY_A)

    # ── 权限：无节点写 ⇒ 403；持节点 CRUD 全通过；无节点 GET 只见 enabled ──
    def test_write_without_node_is_403_and_ticket_without_node_is_403(self):
        for method, path, payload in (
            ("post", URL_LIST, {"kind": "qq_bot", "media_key": KEY_A}),
            ("patch", detail_url(1), {"is_enabled": False}),
            ("delete", detail_url(1), None),
            ("post", URL_TICKET, {"ext": "png"}),
        ):
            sender = getattr(self.viewer, method)
            response = sender(path, payload, content_type="application/json")
            self.assertEqual(response.status_code, 403, (method, path, response.data))
        self.assertFalse(HomeQrCode.objects.exists())

    def test_manager_crud_roundtrip_is_hard_delete(self):
        body = self.manager.post(
            URL_LIST,
            {"kind": "qq_group", "game_id": self.game_a.pk, "media_key": KEY_A},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        row = HomeQrCode.objects.get()
        self.assertEqual(row.kind, "qq_group")
        self.assertEqual(row.game_id, self.game_a.pk)
        self.assertEqual(row.created_by, "qr-manager")
        self.assertTrue(row.is_enabled)

        enabled = self.manager.patch(
            detail_url(row.pk), {"is_enabled": False}, content_type="application/json"
        ).json()
        self.assertEqual(enabled["code"], 0)
        row.refresh_from_db()
        self.assertFalse(row.is_enabled)

        sorted_row = self.manager.put(
            detail_url(row.pk),
            {
                "kind": "qq_group",
                "game_id": self.game_b.pk,
                "media_key": KEY_A,
                "sort_order": 3,
                "is_enabled": True,
            },
            content_type="application/json",
        ).json()
        self.assertEqual(sorted_row["code"], 0, sorted_row)
        row.refresh_from_db()
        self.assertEqual(row.sort_order, 3)
        self.assertEqual(row.game_id, self.game_b.pk)
        self.assertTrue(row.is_enabled)

        deleted = self.manager.delete(detail_url(row.pk)).json()
        self.assertEqual(deleted["code"], 0)
        # 硬删（照 SystemNotice 先例）：行真的没了。
        self.assertFalse(HomeQrCode.objects.filter(pk=row.pk).exists())

    def test_viewer_sees_only_enabled_manager_sees_all(self):
        visible = HomeQrCode.objects.create(kind="qq_bot", media_key=KEY_A)
        hidden = HomeQrCode.objects.create(kind="qq_group", game=self.game_a, media_key=KEY_A)
        hidden.is_enabled = False
        hidden.save()

        seen = self.viewer.get(URL_LIST).json()["data"]["items"]
        self.assertEqual([row["media_key"] for row in seen], [KEY_A])
        self.assertEqual(seen[0]["kind"], "qq_bot")
        self.assertTrue(seen[0]["is_enabled"])
        # 判据打在**数据库终态**上：那一行还活着、只是被可见性收掉了（⛔ 不是被删）。
        self.assertTrue(HomeQrCode.objects.filter(pk=hidden.pk, is_enabled=False).exists())

        managed = self.manager.get(URL_LIST).json()["data"]["items"]
        self.assertEqual(len(managed), 2)
        self.assertEqual(
            sorted(row["is_enabled"] for row in managed), [False, True]
        )

    def test_list_returns_directly_usable_media_url(self):
        HomeQrCode.objects.create(kind="qq_bot", media_key=KEY_A)
        item = self.viewer.get(URL_LIST).json()["data"]["items"][0]
        # `<img src>` 可直接用：本机后端签的是它自己的 blob 端点（非空、且指向那个 key）。
        self.assertTrue(item["media_url"])
        self.assertIn("media/blob/", item["media_url"])

    def test_list_preserves_original_qr_url_with_private_revalidation(self):
        HomeQrCode.objects.create(kind="qq_bot", media_key=KEY_A, created_by="qr-manager")
        with mock.patch("apps.business.api.qr_code_views.get_storage") as get:
            get.return_value.presign_get.return_value = Presigned("https://cos.example.invalid/qr.png?sig=test", 5000)
            body = self.viewer.get(URL_LIST).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["items"][0]["media_url"], "https://cos.example.invalid/qr.png?sig=test")
        get.return_value.presign_get.assert_called_once_with(KEY_A, cache_preview=True)

    def test_payload_carries_game_name(self):
        HomeQrCode.objects.create(kind="qq_group", game=self.game_a, media_key=KEY_A)
        HomeQrCode.objects.create(kind="qq_bot", media_key=KEY_A)
        items = self.manager.get(URL_LIST).json()["data"]["items"]
        by_kind = {row["kind"]: row for row in items}
        self.assertEqual(by_kind["qq_group"]["game_name"], "横幅游戏甲")
        self.assertIsNone(by_kind["qq_bot"]["game_name"])

    # ── 唯一约束：第二张 qq_bot 被拒（业务码，不是 500）──────────────────
    def test_second_qq_bot_is_conflict_1001(self):
        HomeQrCode.objects.create(kind="qq_bot", media_key=KEY_A)
        _put_image(KEY_LOCAL_B)
        body = self.manager.post(
            URL_LIST, {"kind": "qq_bot", "media_key": KEY_LOCAL_B}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 1001, body)
        self.assertIn("已存在", body["msg"])
        # 判据打在库上：仍然只有一张。
        self.assertEqual(HomeQrCode.objects.filter(kind="qq_bot").count(), 1)

    def test_switching_kind_to_occupied_singleton_is_conflict(self):
        HomeQrCode.objects.create(kind="qq_bot", media_key=KEY_A)
        group = HomeQrCode.objects.create(kind="qq_group", game=self.game_a, media_key=KEY_A)
        # 换种类必须**同时清掉游戏**（`qq_bot` 不关联游戏是 API 层的配对判据）——
        # 只改 kind 不清游戏 ⇒ 1000（入参配对错），不是 1001。
        game_kept = self.manager.patch(
            detail_url(group.pk), {"kind": "qq_bot"}, content_type="application/json"
        ).json()
        self.assertEqual(game_kept["code"], 1000, game_kept)
        group.refresh_from_db()
        self.assertEqual(group.kind, "qq_group")

        # 配对合法（游戏已清）但另一张 `qq_bot` 已占位 ⇒ 业务码 1001，群码原样。
        occupied = self.manager.patch(
            detail_url(group.pk),
            {"kind": "qq_bot", "game_id": None},
            content_type="application/json",
        ).json()
        self.assertEqual(occupied["code"], 1001, occupied)
        group.refresh_from_db()
        self.assertEqual(group.kind, "qq_group")

        # 正面哨兵：没有占位冲突时，换种类 + 清游戏真的成功。
        group2 = HomeQrCode.objects.create(kind="qq_group", game=self.game_b, media_key=KEY_A)
        ok = self.manager.patch(
            detail_url(group2.pk),
            {"kind": "wechat_mp", "game_id": None},
            content_type="application/json",
        ).json()
        self.assertEqual(ok["code"], 0, ok)
        group2.refresh_from_db()
        self.assertEqual(group2.kind, "wechat_mp")
        self.assertIsNone(group2.game_id)

    def test_wechat_mp_also_singleton_but_qq_group_free(self):
        HomeQrCode.objects.create(kind="wechat_mp", media_key=KEY_A)
        _put_image(KEY_LOCAL_B)
        first = self.manager.post(
            URL_LIST,
            {"kind": "qq_group", "game_id": self.game_a.pk, "media_key": KEY_LOCAL_B},
            content_type="application/json",
        ).json()
        self.assertEqual(first["code"], 0, first)
        second = self.manager.post(
            URL_LIST,
            {
                "kind": "qq_group",
                "game_id": self.game_b.pk,
                "media_key": KEY_LOCAL_B,
                "sort_order": 1,
            },
            content_type="application/json",
        ).json()
        self.assertEqual(second["code"], 0, second)
        self.assertEqual(HomeQrCode.objects.filter(kind="qq_group").count(), 2)

    # ── kind × game 配对（API 层 1000；库层 CheckConstraint 同批保险）─────
    def test_group_without_game_and_singleton_with_game_are_1000(self):
        _put_image(KEY_LOCAL_B)
        body = self.manager.post(
            URL_LIST, {"kind": "qq_group", "media_key": KEY_LOCAL_B}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("游戏", body["msg"])
        body = self.manager.post(
            URL_LIST,
            {"kind": "qq_bot", "game_id": self.game_a.pk, "media_key": KEY_LOCAL_B},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(HomeQrCode.objects.count(), 0)

    def test_db_constraint_rejects_bad_pairing(self):
        """库层 `CheckConstraint` 是**兜底**：绕过 API 直写畸形配对必须响亮地炸。

        ⚠️ 每条 `IntegrityError` 都要包在自己的 `transaction.atomic()` 里：
        不包的话，PostgreSQL 会把**整个测试事务**打成 aborted，后面每条 ORM 都
        `TransactionManagementError`（那是探针自己的用法错，不是被测系统的行为）。
        """
        from django.db import transaction
        from django.db.models import ProtectedError
        from django.db.utils import IntegrityError

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                HomeQrCode.objects.create(kind="qq_group", media_key=KEY_A)
        HomeQrCode.objects.create(kind="qq_bot", media_key=KEY_A)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                HomeQrCode.objects.create(
                    kind="wechat_mp", game=self.game_a, media_key=KEY_A
                )
        # FK 是 PROTECT：游戏被群码引用时硬删必须被挡（`EnvelopeModelViewSet.destroy`
        # 会把它翻译成 1001，这里钉的是模型层语义）。
        HomeQrCode.objects.create(kind="qq_group", game=self.game_a, media_key=KEY_A)
        with self.assertRaises(ProtectedError):
            self.game_a.delete()

    # ── 媒体校验：他人租户的 key / 不存在的 key / 非 home-qr 种类都拒 ──────
    def test_media_key_validation_matrix(self):
        foreign = "victim_tenant/users/11/home-qr.eeeeffff0000.png"
        cases = [
            # (payload, 期望 code 或状态码)
            ({"kind": "qq_bot", "media_key": ""}, 1000),
            ({"kind": "qq_bot", "media_key": foreign}, 403),
            ({"kind": "qq_bot", "media_key": "test/orders/1/abc.png"}, 1000),
            ({"kind": "qq_bot", "media_key": "test/users/11/avatar.eeeeffff0000.png"}, 1000),
            ({"kind": "qq_bot", "media_key": "test/users/11/home-qr.missing000000.png"}, 1000),
        ]
        for payload, expected in cases:
            response = self.manager.post(URL_LIST, payload, content_type="application/json")
            actual = response.status_code if expected == 403 else response.json()["code"]
            self.assertEqual(actual, expected, payload)
        self.assertFalse(HomeQrCode.objects.exists())
        # 越租户的 key 连一行都不该落（判据打在库上）。
        self.assertEqual(HomeQrCode.objects.filter(media_key=foreign).count(), 0)

    def test_zero_byte_and_non_image_objects_are_rejected(self):
        empty_key = "test/users/11/home-qr.000000000001.png"
        text_key = "test/users/11/home-qr.000000000002.png"
        get_storage().put_bytes(empty_key, b"", content_type="image/png")
        get_storage().put_bytes(text_key, b"hello not an image", content_type="image/png")
        for key in (empty_key, text_key):
            body = self.manager.post(
                URL_LIST, {"kind": "qq_bot", "media_key": key}, content_type="application/json"
            ).json()
            self.assertEqual(body["code"], 1000, (key, body))
            self.assertIn(body["msg"], {"图片无效或为空", "只支持 jpg / jpeg / png"})
        self.assertFalse(HomeQrCode.objects.exists())

    # ── 排序：两张群码换序后 GET 顺序稳定 ────────────────────────────────
    def test_sort_order_is_stable_after_swap(self):
        first = HomeQrCode.objects.create(
            kind="qq_group", game=self.game_a, media_key=KEY_A, sort_order=1
        )
        second = HomeQrCode.objects.create(
            kind="qq_group", game=self.game_b, media_key=KEY_A, sort_order=2
        )

        def names():
            return [
                row["game_name"]
                for row in self.viewer.get(URL_LIST).json()["data"]["items"]
            ]

        self.assertEqual(names(), ["横幅游戏甲", "横幅游戏乙"])

        # 换序（照前端「上移/下移」的形状：两行各 PUT 一次对方的 sort_order）。
        swapped_first = self.manager.patch(
            detail_url(first.pk), {"sort_order": 2}, content_type="application/json"
        ).json()
        swapped_second = self.manager.patch(
            detail_url(second.pk), {"sort_order": 1}, content_type="application/json"
        ).json()
        self.assertEqual(swapped_first["code"], 0, swapped_first)
        self.assertEqual(swapped_second["code"], 0, swapped_second)
        self.assertEqual(names(), ["横幅游戏乙", "横幅游戏甲"])

        # 同 sort_order 按 id 稳定（Meta.ordering 的第二把钥匙）：第三张挤进同一个档位。
        third = HomeQrCode.objects.create(
            kind="qq_group", game=self.game_a, media_key=KEY_A, sort_order=1
        )
        rows = self.viewer.get(URL_LIST).json()["data"]["items"]
        self.assertEqual(
            [row["game_name"] for row in rows],
            ["横幅游戏乙", "横幅游戏甲", "横幅游戏甲"],
        )
        # `third`（甲）排在 `second`（乙，同档位但 id 更小）之后。
        self.assertEqual(rows[1]["id"], third.pk)

    # ── 直传票据：签出的 key 必须落在「本租户 + 本用户 + home-qr」这一格 ────
    def test_ticket_issues_home_qr_key_for_the_caller(self):
        body = self.manager.post(URL_TICKET, {"ext": "png"}, content_type="application/json").json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["expires_in"], 600)
        self.assertTrue(
            body["data"]["key"].startswith("test/users/"),
            body["data"]["key"],
        )
        self.assertRegex(body["data"]["key"], r"^test/users/\d+/home-qr\.[0-9a-f]{12}\.png$")
        self.assertTrue(body["data"]["upload_url"])

    def test_ticket_rejects_bad_ext_and_garbage_body(self):
        body = self.manager.post(
            URL_TICKET, {"ext": "gif"}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "只支持 jpg / jpeg / png")
        # 请求体不是对象：过序列化器 ⇒ 1000，不是 500（E-0010 的「这类请求」判据）。
        response = self.manager.post(
            URL_TICKET, ["png"], content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)


class HomeQrIsolationTests(TenantTestCase):
    """跨租户：以租户 A 的凭据访问 / 改 / 删 B 的行必须失败（AGENTS §4 / §5-G）。

    ⚠️ 跨租户的否定断言以「租户内唯一、且本例两租户取值不同的业务值」为判据
    （本文件用 `media_key` 的两个字面量），⛔ 不用主键；pk 只当**入参**，造数时
    故意让两租户的 pk 错开并由用例自证。
    ⚠️ 请求一律发在 `tenant_context(...)` 块**之外** + 显式 `SERVER_NAME`
    （同 `tests/test_feedback_isolation.py` 的口径）。
    """

    DOMAIN_A = "tenant.test.com"
    DOMAIN_B = "qr-iso-b.test.local"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name="iso_qr_b", name="二维码隔离租户乙", domain=cls.DOMAIN_B
        )

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.storage = get_storage()
        _put_image(KEY_A)

    def _seed(self):
        """A 两行（占位的 `wechat_mp` + 目标 `qq_bot`）、B 一行 ⇒ A 的目标 pk（2）
        **必然大于** B 的最大 pk（1），由用例自证（同 feedback 隔离测试的手法）。"""
        with tenant_context(self.tenant):
            admin_a = make_logged_in_client(
                domain=self.DOMAIN_A, username="qr-iso-a", groups=("人事",), nodes=(NODE,)
            )
            HomeQrCode.objects.create(kind="wechat_mp", media_key=KEY_A)
            row_a = HomeQrCode.objects.create(kind="qq_bot", media_key=KEY_A)
        with tenant_context(self.tenant_b):
            _put_image(KEY_B)
            admin_b = make_logged_in_client(
                domain=self.DOMAIN_B, username="qr-iso-b", groups=("人事",), nodes=(NODE,)
            )
            row_b = HomeQrCode.objects.create(kind="qq_bot", media_key=KEY_B)
        self.assertNotEqual(
            row_a.pk, row_b.pk, "夹具没造对：两租户目标 pk 撞了 ⇒ 隔离测不到"
        )
        return row_a.pk, row_b.pk, admin_a, admin_b

    def test_other_tenants_rows_are_invisible_and_untouchable(self):
        pk_a, pk_b, admin_a, admin_b = self._seed()

        try:
            body = admin_b.get(URL_LIST, SERVER_NAME=self.DOMAIN_B).data
        finally:
            connection.set_tenant(self.tenant)
        # 判据打在业务值上：B 的列表只有 B 的对象名，没有 A 的。
        self.assertEqual(body["code"], 0)
        self.assertEqual([row["media_key"] for row in body["data"]["items"]], [KEY_B])

        # B 拿 A 的 pk 改 / 删 ⇒ 「记录不存在」业务码，且 A 那行一个字没变。
        try:
            patched = admin_b.patch(
                detail_url(pk_a),
                {"is_enabled": False},
                content_type="application/json",
                SERVER_NAME=self.DOMAIN_B,
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.data["code"], 1002, patched.data)
        try:
            deleted = admin_b.delete(detail_url(pk_a), SERVER_NAME=self.DOMAIN_B)
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.data["code"], 1002, deleted.data)

        # ⚠️ 判据打在业务值 + 种类上：A 的目标行（qq_bot）一个字没变——
        #    `media_key` 单独不唯一（A 的占位 `wechat_mp` 与它同名），⛔ 不能裸 `get(media_key=…)`。
        with tenant_context(self.tenant):
            row = HomeQrCode.objects.get(media_key=KEY_A, kind="qq_bot")
            self.assertTrue(row.is_enabled)
        with tenant_context(self.tenant_b):
            self.assertEqual(HomeQrCode.objects.count(), 1)

        # 反向（A 打 B 的 pk）同样 404 语义，且 B 那行没变。
        try:
            patched_a = admin_a.patch(
                detail_url(pk_b),
                {"is_enabled": False},
                content_type="application/json",
                SERVER_NAME=self.DOMAIN_A,
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(patched_a.data["code"], 1002, patched_a.data)
        with tenant_context(self.tenant_b):
            self.assertTrue(HomeQrCode.objects.get(media_key=KEY_B).is_enabled)

    def test_positive_control_each_tenant_manages_its_own_row(self):
        """非空哨兵：两个租户**各自**能改 / 删自己的行（否则上面的拒绝可能只是「接口坏了」）。"""
        pk_a, pk_b, admin_a, admin_b = self._seed()
        try:
            ok_b = admin_b.patch(
                detail_url(pk_b), {"is_enabled": False},
                content_type="application/json", SERVER_NAME=self.DOMAIN_B,
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(ok_b.data["code"], 0, ok_b.data)
        ok_a = admin_a.delete(detail_url(pk_a), SERVER_NAME=self.DOMAIN_A)
        self.assertEqual(ok_a.data["code"], 0, ok_a.data)
        with tenant_context(self.tenant):
            # A 的目标行（qq_bot）真删了；占位的 wechat_mp 原样（硬删只删目标那一行）。
            self.assertEqual(HomeQrCode.objects.filter(kind="qq_bot").count(), 0)
            self.assertEqual(HomeQrCode.objects.filter(kind="wechat_mp").count(), 1)
        with tenant_context(self.tenant_b):
            self.assertFalse(HomeQrCode.objects.get(media_key=KEY_B).is_enabled)

    def test_cross_tenant_media_key_is_rejected_and_foreign_bytes_untouched(self):
        """B 拿 A 命名空间下的 key 建横幅 ⇒ 403、B 零新增，且 **A 的对象原样还在**。"""
        pk_a, pk_b, admin_a, admin_b = self._seed()
        try:
            response = admin_b.post(
                URL_LIST,
                {"kind": "qq_bot", "media_key": KEY_A},
                content_type="application/json",
                SERVER_NAME=self.DOMAIN_B,
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["code"], 403)
        self.assertEqual(response.data["msg"], "二维码图片对象名不属于当前租户")
        with tenant_context(self.tenant_b):
            self.assertEqual(HomeQrCode.objects.count(), 1)
        # A 的对象一个字节都没少（本端点没有任何删除路径）。
        self.assertIsNotNone(self.storage.head(KEY_A))
