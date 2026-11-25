"""游戏 logo 的上传链与读面（`feat/game-logo`，2026-10-20）。

链 = 取票（`POST …/logo/ticket`）→ 客户端 `PUT <upload_url>` → 认领（`POST …/logo`），
读面 = `GET /api/business/games/` 每行的 `logo_url`（本机是相对路径、要登录态）。

判据口径（`AGENTS.md` §5-G）：
- 断言打在**接口响应的真实出口**上：`logo_url` 从列表响应里取（⛔ 不直接读库拼）；
  审计断言打在 `OperationLog` 的**数据库行**上。
- 上限用**字面量** `10 * 1024 * 1024`（⛔ 不引用 `MAX_USER_MEDIA_BYTES`：两侧同源的话
  把常量改成 1 MB 这类用例仍绿，是自创的假绿）。
- 跨租户的否定断言以「两租户取值不同的对象名前缀」为判据（⛔ 不用主键指认另一租户的行）。
- 夹具值一律**合成值**（真值守卫只扫 `src/**`）。

⚠️ **端点数说明**：本线真正受 `game.add_edit` 节点保护的是**三个**端点——取票、本机直传
（`PUT /api/media/blob/<key>`，登录即可，key 只能由取票拿到）、认领。`GET /api/business/games/`
是**只读、登录即可**（网页端与打手 App 都要能渲染游戏列表）⇒ 它**不该**因为缺节点而 403；
本文件有一条正向对照钉住这一点。
"""
import pathlib
import tempfile
from io import BytesIO
from unittest import mock

from django.db import connection
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context
from PIL import Image

from apps.audit.models import OperationLog
from apps.business.models import Game
from apps.core.storage import Presigned, build_game_logo_key, get_storage
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

#: 游戏管理的写节点（本线**不新开节点**，见 `apps/business/api/game_logo_views.py`）。
NODE = "game.add_edit"

TENANT_DOMAIN = "tenant.test.com"

#: 合成的 JPEG / PNG 头（真值守卫只扫 `src/**`）；魔数判据只看前几个字节。
JPEG = b"\xff\xd8\xff" + b"game-logo-body"
PNG = b"\x89PNG\r\n\x1a\n" + b"game-logo-body"
#: 10 MiB 的**字面值**（业务方裁定的头像口径，logo 照抄）。
TEN_MIB = 10 * 1024 * 1024


def ticket_url(game_id) -> str:
    return f"/api/business/games/{game_id}/logo/ticket"


def claim_url(game_id) -> str:
    return f"/api/business/games/{game_id}/logo"


class _TempMediaRootMixin:
    """把 `MEDIA_ROOT` 指到临时目录（同 `tests/test_media_storage.py` 的同名 mixin）。

    ⚠️ 不覆盖的话，`get_storage()` 会往仓库根 `.tools/media` 里写文件——那是本机数据，
    测试不该动它。
    """

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        patcher.enable()
        self.addCleanup(patcher.disable)


class GameLogoApiTests(_TempMediaRootMixin, TenantTestCase):
    """单租户内的完整链路、读面、校验矩阵、权限与软删。"""

    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="logo-游戏甲")
        self.client = make_logged_in_client(
            username="logo-manager", groups=("配置组",), nodes=(NODE,)
        )
        self.storage = get_storage()

    # ── 工具 ────────────────────────────────────────────────────────
    def _ticket(self, game_id=None, ext="jpg"):
        return self.client.post(
            ticket_url(self.game.pk if game_id is None else game_id),
            {"ext": ext},
            content_type="application/json",
        )

    def _upload(self, upload_url, body=JPEG, content_type="image/jpeg"):
        return self.client.put(upload_url, data=body, content_type=content_type)

    def _claim(self, key, game_id=None):
        return self.client.post(
            claim_url(self.game.pk if game_id is None else game_id),
            {"key": key},
            content_type="application/json",
        )

    def _logo_row(self, game_id=None):
        body = self.client.get("/api/business/games/?page_size=200").json()
        self.assertEqual(body["code"], 0, body)
        target = self.game.pk if game_id is None else game_id
        return next(row for row in body["data"]["items"] if row["id"] == target)

    # ── 完整链路：取票 → 直传 → 认领 → 列表出 logo_url ──────────────────
    def test_ticket_upload_claim_then_the_list_exposes_a_usable_logo_url(self):
        ticket = self._ticket().json()
        self.assertEqual(ticket["code"], 0, ticket)
        key = ticket["data"]["key"]
        upload_url = ticket["data"]["upload_url"]
        # 形状：本租户 + **这一款游戏** + `logo.` 段（对象名不落库时也必须稳定）。
        self.assertTrue(
            key.startswith(f"{connection.schema_name}/games/{self.game.pk}/logo."), key
        )
        self.assertIn("/api/media/blob/", upload_url)
        self.assertEqual(ticket["data"]["expires_in"], 600)
        # 取票**不写库**：签发了但没传完，列上不该指向一个不存在的对象。
        self.game.refresh_from_db()
        self.assertEqual(self.game.logo_key, "")

        put = self._upload(upload_url)
        self.assertEqual(put.status_code, 200)
        self.assertIsNotNone(self.storage.head(key))

        claim = self._claim(key).json()
        self.assertEqual(claim["code"], 0, claim)
        self.assertEqual(claim["data"]["key"], key)
        self.game.refresh_from_db()
        self.assertEqual(self.game.logo_key, key)

        # ⭐ 判据从**接口响应**里取 url（不是直接读库自己拼）。
        row = self._logo_row()
        logo_url = row["logo_url"]
        self.assertTrue(logo_url, "有 key 时 logo_url 不能是空串")
        self.assertIn(key, logo_url)
        # 本机存储的链接是相对路径、要登录态；它必须真的能取到图（端到端）。
        image = self.client.get(logo_url)
        self.assertEqual(image.status_code, 200)
        # 本机 blob 端点是 `FileResponse` ⇒ 用 `streaming_content` 取体（没有 `.content`）。
        self.assertEqual(b"".join(image.streaming_content), JPEG)

    def test_a_game_without_a_logo_returns_an_empty_string_not_null(self):
        row = self._logo_row()
        self.assertEqual(row["logo_url"], "")
        self.assertIsNotNone(row["logo_url"])

    def test_local_transparent_logo_remains_readable_through_existing_logo_url(self):
        buf = BytesIO()
        Image.new("RGBA", (2, 2), (255, 0, 0, 0)).save(buf, format="PNG")
        ticket = self._ticket(ext="png").json()["data"]
        self.assertEqual(self._upload(ticket["upload_url"], buf.getvalue(), "image/png").status_code, 200)
        self.assertEqual(self._claim(ticket["key"]).json()["code"], 0)
        response = self.client.get(self._logo_row()["logo_url"])
        self.assertEqual(response.status_code, 200)
        result = Image.open(BytesIO(b"".join(response.streaming_content))).convert("RGBA")
        self.assertEqual(result.getpixel((0, 0))[3], 0)

    def test_game_list_keeps_existing_logo_field_and_requests_private_thumbnail(self):
        key = f"{connection.schema_name}/games/{self.game.pk}/logo.synthetic.png"
        self.game.logo_key = key
        self.game.save(update_fields=["logo_key"])
        with mock.patch("apps.core.storage.get_storage") as get:
            get.return_value.presign_get.return_value = Presigned("https://cos.example.invalid/logo.png?sig=test", 5000)
            row = self._logo_row()
        self.assertEqual(row["logo_url"], "https://cos.example.invalid/logo.png?sig=test&imageMogr2/thumbnail/300x300%3E/ignore-error/1")
        get.return_value.presign_get.assert_called_once_with(key, cache_preview=True)

    def test_png_ticket_is_accepted_and_the_extension_round_trips(self):
        key = self._ticket(ext="png").json()["data"]["key"]
        self.assertTrue(key.endswith(".png"), key)
        self._upload(f"/api/media/blob/{key}", PNG, "image/png")
        self.assertEqual(self._claim(key).json()["code"], 0)
        self.game.refresh_from_db()
        self.assertEqual(self.game.logo_key, key)

    # ── 取票的入参校验 ──────────────────────────────────────────────
    def test_ticket_rejects_disallowed_or_missing_extensions(self):
        for ext, expected_msg in (
            ("gif", "只支持 jpg / jpeg / png"),
            ("", "只支持 jpg / jpeg / png"),
            # 缺字段是**入参形状**错（序列化器的 `required`），文案与白名单外分开。
            (None, "缺少图片格式"),
        ):
            payload = {} if ext is None else {"ext": ext}
            body = self.client.post(
                ticket_url(self.game.pk), payload, content_type="application/json"
            ).json()
            self.assertEqual(body["code"], 1000, (ext, body))
            self.assertEqual(body["msg"], expected_msg, ext)

    def test_ticket_rejects_a_garbage_body_without_a_500(self):
        """请求体是数组（不是对象）时必须是 1000（⛔ 不是 500）——序列化器挡在第一跳。"""
        response = self.client.post(
            ticket_url(self.game.pk), ["png"], content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000, response.content[:200])
        # 连 JSON 都不合法的体是**传输层**错（400），⛔ 也不是 500。
        broken = self.client.post(
            ticket_url(self.game.pk), "jpg", content_type="application/json"
        )
        self.assertEqual(broken.status_code, 400)
        self.assertEqual(broken.json()["code"], 400)

    # ── 认领的校验矩阵 ──────────────────────────────────────────────
    def test_claim_refuses_bytes_that_are_not_an_image_and_cleans_up(self):
        key = self._ticket().json()["data"]["key"]
        self.storage.put_bytes(key, b"GIF89a-not-an-image", content_type="image/png")
        body = self._claim(key).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "只支持 jpg / jpeg / png")
        self.game.refresh_from_db()
        self.assertEqual(self.game.logo_key, "")
        # 照头像线的清理口径：被拒的对象删掉（只可能是本租户本游戏的对象）。
        self.assertIsNone(self.storage.head(key))

    def test_claim_refuses_a_zero_byte_object_and_cleans_up(self):
        key = self._ticket().json()["data"]["key"]
        self.storage.put_bytes(key, b"", content_type="image/jpeg")
        body = self._claim(key).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "图片无效或为空")
        self._assert_cleaned_up(key)

    def test_claim_refuses_one_byte_over_10mb_and_cleans_up(self):
        """上限 = **10 MiB**（字面量）：比它多 1 字节就拒，且对象被删、列不写。"""
        key = self._ticket().json()["data"]["key"]
        body = JPEG + b"0" * (TEN_MIB + 1 - len(JPEG))
        self.assertEqual(len(body), TEN_MIB + 1)
        self.storage.put_bytes(key, body, content_type="image/jpeg")
        response = self._claim(key).json()
        self.assertEqual(response["code"], 1000, response)
        self.assertEqual(response["msg"], "图片超过 10 MB")
        self._assert_cleaned_up(key)

    def test_exactly_10mb_is_accepted(self):
        """边界另一侧：恰好 10 MiB 必须放行（否则「多 1 字节拒」可能只是「什么都拒」）。"""
        key = self._ticket().json()["data"]["key"]
        body = JPEG + b"0" * (TEN_MIB - len(JPEG))
        self.assertEqual(len(body), TEN_MIB)
        self.storage.put_bytes(key, body, content_type="image/jpeg")
        response = self._claim(key).json()
        self.assertEqual(response["code"], 0, response)
        self.game.refresh_from_db()
        self.assertEqual(self.game.logo_key, key)

    def test_claim_requires_the_object_to_be_uploaded_first(self):
        key = self._ticket().json()["data"]["key"]
        body = self._claim(key).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "图片还没上传成功")

    def test_claim_rejects_a_key_for_another_game_in_the_same_tenant(self):
        """A 游戏的 key 认领到 B 游戏 ⇒ 1000（否则 B 的图标会静默变成 A 的图）。"""
        other = Game.objects.create(name="logo-游戏乙")
        key = self._ticket().json()["data"]["key"]
        self._upload(f"/api/media/blob/{key}")
        self.assertIsNotNone(self.storage.head(key))

        body = self._claim(key, game_id=other.pk).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "图片对象名格式不正确")
        other.refresh_from_db()
        self.assertEqual(other.logo_key, "")
        # 形状不对**不删对象**（它可能正是 A 游戏刚传好的图，删了就误伤）。
        self.assertIsNotNone(self.storage.head(key))

    def test_claim_rejects_a_dotdot_escaped_key(self):
        escaped = (
            f"{connection.schema_name}/games/{self.game.pk}/"
            f"../../victim-tenant/games/1/logo.abc.jpg"
        )
        response = self._claim(escaped)
        self.assertEqual(response.status_code, 403)
        self.game.refresh_from_db()
        self.assertEqual(self.game.logo_key, "")

    # ── 替换：旧对象在写库成功之后删掉 ─────────────────────────────────
    def test_replacing_a_logo_deletes_the_previous_object(self):
        first = self._ticket().json()["data"]["key"]
        self._upload(f"/api/media/blob/{first}")
        self.assertEqual(self._claim(first).json()["code"], 0)

        second = self._ticket(ext="png").json()["data"]["key"]
        self._upload(f"/api/media/blob/{second}", PNG, "image/png")
        self.assertEqual(self._claim(second).json()["code"], 0)

        self.game.refresh_from_db()
        self.assertEqual(self.game.logo_key, second)
        self.assertIsNone(self.storage.head(first))

    # ── 审计：认领成功落一条；取票 / 同 key 重提交不落 ─────────────────
    def test_claim_writes_one_audit_row_and_ticket_writes_none(self):
        before = OperationLog.objects.count()
        key = self._ticket().json()["data"]["key"]
        self.assertEqual(OperationLog.objects.count(), before, "取票不写操作日志")

        self._upload(f"/api/media/blob/{key}")
        self.assertEqual(self._claim(key).json()["code"], 0)
        rows = OperationLog.objects.filter(
            action="system.game_update", target_kind="game", target_id=str(self.game.pk)
        )
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.target_name, self.game.name)
        self.assertEqual(row.actor, "logo-manager")
        self.assertIn(self.game.name, row.summary)

        # 同 key 重提交 = 什么都没发生（R27.2）⇒ 不写第二行。
        self.assertEqual(self._claim(key).json()["code"], 0)
        self.assertEqual(rows.count(), 1)

    # ── 权限：三个受节点保护的端点缺节点 ⇒ 403；读列表仍是登录即可 ────────
    def test_without_the_node_the_write_endpoints_are_403_but_the_list_still_reads(self):
        key = self._ticket().json()["data"]["key"]
        self.storage.put_bytes(key, JPEG, content_type="image/jpeg")

        viewer = make_logged_in_client(username="logo-viewer", groups=("客服",))
        for label, response in (
            ("ticket", viewer.post(
                ticket_url(self.game.pk), {"ext": "jpg"}, content_type="application/json"
            )),
            ("claim", viewer.post(
                claim_url(self.game.pk), {"key": key}, content_type="application/json"
            )),
        ):
            self.assertEqual(response.status_code, 403, (label, response.content[:200]))
        # ⛔ 读面**不是**节点门（网页端 / 打手 App 都要能渲染游戏列表）：缺节点也必须能读，
        #    只是列表里的 `logo_url` 仍按配置返回。
        listing = viewer.get("/api/business/games/?page_size=200")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["code"], 0)
        # 未认领 ⇒ 列上仍是空串（403 的认领没有副作用）。
        self.game.refresh_from_db()
        self.assertEqual(self.game.logo_key, "")

    # ── 软删：不能给已删除的游戏换图标（与列表口径同一条）───────────────
    def test_a_soft_deleted_game_cannot_get_or_claim_a_logo(self):
        deleted = Game.objects.create(name="logo-已删游戏", deleted_at=timezone.now())
        body = self.client.post(
            ticket_url(deleted.pk), {"ext": "jpg"}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 1002, body)

        key = build_game_logo_key(connection.schema_name, deleted.pk, "jpg")
        self.storage.put_bytes(key, JPEG, content_type="image/jpeg")
        claim = self.client.post(
            claim_url(deleted.pk), {"key": key}, content_type="application/json"
        ).json()
        self.assertEqual(claim["code"], 1002, claim)
        deleted.refresh_from_db()
        self.assertEqual(deleted.logo_key, "")
        # 被判「记录不存在」时**不碰对象**（这个 key 可能是别处正在用的对象）。
        self.assertIsNotNone(self.storage.head(key))

    # ── 小工具 ─────────────────────────────────────────────────────
    def _assert_cleaned_up(self, key):
        """被拒之后：列仍是空串、对象已被删（照头像线的清理口径）。"""
        self.game.refresh_from_db()
        self.assertEqual(self.game.logo_key, "")
        self.assertIsNone(self.storage.head(key))


class GameLogoIsolationTests(_TempMediaRootMixin, TenantTestCase):
    """跨租户：A 的 key 在 B 下认领必须 403（`AGENTS.md` §4 / §5-G）。

    ⚠️ 请求一律发在 `tenant_context(...)` 块**之外** + 显式 `SERVER_NAME`（同
    `tests/test_feedback_isolation.py` / `tests/test_home_qr_codes.py` 的口径）。
    ⚠️ 判据用两个**字面量前缀**（两租户各自的 schema 名），⛔ 不用主键指认另一租户的行。
    """

    DOMAIN_A = TENANT_DOMAIN
    DOMAIN_B = "game-logo-iso-b.test.local"
    SCHEMA_B = "iso_logo_b"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=cls.SCHEMA_B,
            name="logo隔离租户乙",
            domain=cls.DOMAIN_B,
        )

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        # 本租户 schema 名在 setUp 里取一次：请求回来时连接可能已被中间件切到别的租户
        # （`TenantMainMiddleware.process_request` 会 `set_tenant`），⛔ 不在断言里现取。
        self.schema_a = connection.schema_name

    def _seed(self):
        """A、B 各一款游戏 + 各自的持节点账号；返回 `(game_a, game_b, admin_a, admin_b)`。"""
        with tenant_context(self.tenant):
            game_a = Game.objects.create(name="logo隔离甲")
            admin_a = make_logged_in_client(
                domain=self.DOMAIN_A, username="logo-iso-a", groups=("人事",), nodes=(NODE,)
            )
        with tenant_context(self.tenant_b):
            game_b = Game.objects.create(name="logo隔离乙")
            admin_b = make_logged_in_client(
                domain=self.DOMAIN_B, username="logo-iso-b", groups=("人事",), nodes=(NODE,)
            )
        # ⚠️ 两租户的主键可能撞（各自 schema 独立计数）——**这没关系、也不要修**：
        # 请求里的 `<id>` 只在**该请求所属租户**里解析，判据落在 key 前缀（两个 schema 名）上，
        # ⛔ 不用主键指认「另一租户的那一行」（`AGENTS.md` §5-G）。
        return game_a, game_b, admin_a, admin_b

    def test_tenant_b_cannot_claim_tenant_as_logo_key(self):
        game_a, game_b, admin_a, admin_b = self._seed()
        try:
            ticket = admin_a.post(
                ticket_url(game_a.pk), {"ext": "jpg"}, content_type="application/json"
            ).json()
            self.assertEqual(ticket["code"], 0, ticket)
            key_a = ticket["data"]["key"]
            # 前缀判据：A 的 key 起头是 A 的 schema 名（与 B 的不同）。
            self.assertTrue(key_a.startswith(f"{self.schema_a}/games/"), key_a)
            put = admin_a.put(
                ticket["data"]["upload_url"], data=JPEG, content_type="image/jpeg"
            )
            self.assertEqual(put.status_code, 200)
            self.assertIsNotNone(get_storage().head(key_a))

            # B 拿 A 命名空间下的 key 认领 **B 自己的**游戏 ⇒ 403。
            response = admin_b.post(
                claim_url(game_b.pk), {"key": key_a}, content_type="application/json"
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(response.status_code, 403, response.content[:200])
        self.assertEqual(response.json()["code"], 403)
        self.assertEqual(response.json()["msg"], "图片对象名不属于当前租户")

        with tenant_context(self.tenant_b):
            self.assertEqual(Game.objects.get(pk=game_b.pk).logo_key, "")
        with tenant_context(self.tenant):
            self.assertEqual(Game.objects.get(pk=game_a.pk).logo_key, "")
            # A 的对象一个字节都没少（拒绝路径没有删除动作）。
            self.assertIsNotNone(get_storage().head(key_a))

    def test_positive_control_each_tenant_claims_its_own_key(self):
        """非空哨兵：两租户**各自**能认领自己的 key（否则上面的 403 可能只是接口坏了）。"""
        game_a, game_b, admin_a, admin_b = self._seed()

        def _claim_own(admin, game):
            ticket = admin.post(
                ticket_url(game.pk), {"ext": "png"}, content_type="application/json"
            ).json()
            self.assertEqual(ticket["code"], 0, ticket)
            key = ticket["data"]["key"]
            put = admin.put(ticket["data"]["upload_url"], data=PNG, content_type="image/png")
            self.assertEqual(put.status_code, 200)
            claim = admin.post(
                claim_url(game.pk), {"key": key}, content_type="application/json"
            ).json()
            self.assertEqual(claim["code"], 0, claim)
            return key

        try:
            key_b = _claim_own(admin_b, game_b)
        finally:
            connection.set_tenant(self.tenant)
        key_a = _claim_own(admin_a, game_a)
        self.assertTrue(key_b.startswith(f"{self.SCHEMA_B}/games/"), key_b)
        self.assertTrue(key_a.startswith(f"{self.schema_a}/games/"), key_a)
        self.assertNotEqual(key_a.split("/")[0], key_b.split("/")[0])
        with tenant_context(self.tenant_b):
            self.assertEqual(Game.objects.get(pk=game_b.pk).logo_key, key_b)
        with tenant_context(self.tenant):
            self.assertEqual(Game.objects.get(pk=game_a.pk).logo_key, key_a)
