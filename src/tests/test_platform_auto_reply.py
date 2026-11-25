"""交易平台自动回复规则接口（T13，2026-10-27）。

覆盖：CRUD、`trigger` 取值校验、`content` 必填 / 上限、图片字段口径（`image_key` 不下发）、
权限 403 / 未登录 401、租户隔离、审计留痕（含「纯 no-op 不写留痕」与「正文不进日志」）。

## 判据口径（照本仓既有约定）

- **写操作断言数据库终态**（`AGENTS.md` §5-G）：不看前端提示、不看响应里的对象，
  改完直接读 `PlatformAutoReplyRule` 的行。
- **跨租户的否定断言用业务值、不用主键**（`tests/test_feedback_isolation.py` 的口径）：
  两个租户的 `content` 字面量不同；主键只当入参，且夹具自证两边错开。
- 夹具值一律**合成值**（PII 守卫扫 `src/**`）。
- ⚠️ **时机码值以证据件为准**：`0` = 接单后 / `10` = 申请验收后
  （`public/replica/system-platformautoreply.html:98-101` 的 `value`）。
  下面写的是**字面量**（不从被测枚举派生），否则「码值被改错」时本网跟着一起错。
"""
import json
import pathlib
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.models import Game, PlatformAutoReplyRule
from apps.core.storage import PLATFORM_REPLY_KEY_SEGMENT as PLATFORM_REPLY_SEGMENT
from apps.core.storage import get_storage
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

NODE = "system.platform_auto_reply"
URL_LIST = "/api/business/platform-auto-replies/"

#: 证据件 `html:98-101` 的两个时机码（**字面量**，见模块 docstring）。
TRIGGER_AFTER_ACCEPT = 0
TRIGGER_AFTER_REVIEW_REQUEST = 10

#: 租户 A = `TenantTestCase` 的 `test` schema（与既有隔离测试同口径）。
DOMAIN_A = "tenant.test.com"
#: 租户 B 的域名：后缀 `.test.local` 在 `.env` 的 `DJANGO_ALLOWED_HOSTS` 白名单里。
DOMAIN_B = "platform-auto-reply-iso-b.test.local"
SCHEMA_B = "platform_auto_reply_iso_b"


def detail_url(pk) -> str:
    return f"{URL_LIST}{pk}/"


class PlatformAutoReplyApiTests(TenantTestCase):
    def setUp(self):
        # 跨域名请求不会在响应结束时还原 schema（同 `tests/test_feedback_isolation.py` 的说明），
        # 所以每条用例开头把连接显式钉回租户 A。
        connection.set_tenant(self.tenant)
        self.game = Game.objects.create(name="合成游戏甲")
        self.client = make_logged_in_client(
            username="par-user", groups=("客服",), nodes=(NODE,)
        )

    # ── 小工具 ──────────────────────────────────────────────────────
    def _get(self, url=URL_LIST, client=None):
        return (client or self.client).get(url)

    def _post(self, payload, client=None):
        return (client or self.client).post(
            URL_LIST, payload, content_type="application/json"
        )

    def _patch(self, pk, payload, client=None, domain=DOMAIN_A):
        return (client or self.client).patch(
            detail_url(pk),
            payload,
            content_type="application/json",
            SERVER_NAME=domain,
        )

    def _delete(self, pk, client=None, domain=DOMAIN_A):
        return (client or self.client).delete(detail_url(pk), SERVER_NAME=domain)

    def _payload(self, **overrides):
        payload = {
            "platform": 10,
            "game": self.game.pk,
            "trigger": TRIGGER_AFTER_ACCEPT,
            "content": "合成话术：您好，已接单。",
            "is_enabled": True,
        }
        payload.update(overrides)
        return payload

    # ── 1. CRUD：建 → 读列表 → 改 → 删，断言数据库终态 ─────────────────
    def test_crud_round_trip_checks_database_final_state(self):
        created = self._post(self._payload())
        self.assertEqual(created.data["code"], 0, created.data)
        row = PlatformAutoReplyRule.objects.get()
        self.assertEqual(row.platform, 10)
        self.assertEqual(row.game_id, self.game.pk)
        self.assertEqual(row.trigger, TRIGGER_AFTER_ACCEPT)
        self.assertEqual(row.content, "合成话术：您好，已接单。")
        self.assertTrue(row.is_enabled)
        self.assertEqual(row.image_key, "")

        listed = self._get()
        self.assertEqual(listed.data["code"], 0, listed.data)
        self.assertEqual(len(listed.data["data"]), 1)
        item = listed.data["data"][0]
        self.assertEqual(item["id"], row.pk)
        self.assertEqual(item["platform"], 10)
        self.assertEqual(item["platform_label"], "代练通")
        self.assertEqual(item["game_id"], self.game.pk)
        self.assertEqual(item["game_name"], "合成游戏甲")
        self.assertEqual(item["trigger"], TRIGGER_AFTER_ACCEPT)
        self.assertEqual(item["trigger_name"], "接单后")
        self.assertEqual(item["content"], "合成话术：您好，已接单。")
        self.assertTrue(item["is_enabled"])
        self.assertFalse(item["has_image"])

        patched = self._patch(row.pk, {"content": "合成话术：改过了。", "is_enabled": False})
        self.assertEqual(patched.data["code"], 0, patched.data)
        row.refresh_from_db()
        self.assertEqual(row.content, "合成话术：改过了。")
        self.assertFalse(row.is_enabled)

        deleted = self._delete(row.pk)
        self.assertEqual(deleted.data["code"], 0, deleted.data)
        self.assertEqual(PlatformAutoReplyRule.objects.count(), 0)

    def test_list_orders_by_sort_order_then_id(self):
        """列表顺序 = `Meta.ordering`（`sort_order`，同值按主键）。

        `sort_order` **不在接口字段里**（证据件的表格与弹层都没有它）⇒ 用例直接改库。
        """
        first = PlatformAutoReplyRule.objects.create(
            trigger=TRIGGER_AFTER_ACCEPT, content="先建但排队尾", sort_order=9
        )
        second = PlatformAutoReplyRule.objects.create(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST, content="后建但排前", sort_order=1
        )
        ids = [item["id"] for item in self._get().data["data"]]
        self.assertEqual(ids, [second.pk, first.pk])

    def test_put_is_not_exposed(self):
        """页面只用 `PATCH`；`PUT` 不在这条端点的能力面上（405 + 信封）。"""
        created = self._post(self._payload())
        pk = created.data["data"]["id"]
        resp = self.client.put(detail_url(pk), self._payload(), content_type="application/json")
        self.assertEqual(resp.status_code, 405, resp.content[:300])

    # ── 2. 限定游戏：`null` = 全部游戏；软删除的游戏拒收 ───────────────
    def test_game_is_optional_and_null_means_all_games(self):
        created = self._post(self._payload(game=None))
        self.assertEqual(created.data["code"], 0, created.data)
        row = PlatformAutoReplyRule.objects.get()
        self.assertIsNone(row.game_id)
        item = self._get().data["data"][0]
        self.assertIsNone(item["game_id"])
        # 「全部游戏」这句是**页面文案**，后端不回（一处口径一个地方）。
        self.assertEqual(item["game_name"], "")

    def test_soft_deleted_game_is_rejected(self):
        deleted_game = Game.objects.create(name="已删游戏", deleted_at=timezone.now())
        resp = self._post(self._payload(game=deleted_game.pk))
        self.assertEqual(resp.data["code"], 1000, resp.data)
        self.assertIn("限定游戏", json.dumps(resp.data, ensure_ascii=False))
        self.assertEqual(PlatformAutoReplyRule.objects.count(), 0)

    def test_unknown_game_is_rejected_with_a_field_named_message(self):
        resp = self._post(self._payload(game=987654))
        self.assertEqual(resp.data["code"], 1000, resp.data)
        self.assertIn("限定游戏", json.dumps(resp.data, ensure_ascii=False))
        self.assertEqual(PlatformAutoReplyRule.objects.count(), 0)

    # ── 3. 时机取值校验：只收 0 / 10 ────────────────────────────────
    def test_trigger_accepts_only_the_two_supported_codes(self):
        for code in (TRIGGER_AFTER_ACCEPT, TRIGGER_AFTER_REVIEW_REQUEST):
            resp = self._post(self._payload(trigger=code, content=f"话术-{code}"))
            self.assertEqual(resp.data["code"], 0, resp.data)
        self.assertEqual(
            sorted(PlatformAutoReplyRule.objects.values_list("trigger", flat=True)),
            [TRIGGER_AFTER_ACCEPT, TRIGGER_AFTER_REVIEW_REQUEST],
        )

        # 证据件里存在、但本版**不做**的两个时机（15 提交异常后 / 20 申请撤销后）
        # + 一个纯粹的坏值：一律 1000 且带字段名文案。
        before = PlatformAutoReplyRule.objects.count()
        for bad in (15, 20, 99):
            resp = self._post(self._payload(trigger=bad))
            self.assertEqual(resp.data["code"], 1000, (bad, resp.data))
            self.assertIn("回复时机", json.dumps(resp.data, ensure_ascii=False))
        # 缺字段 / 字符串 / null 也不许 500。
        for payload in (
            {k: v for k, v in self._payload().items() if k != "trigger"},
            self._payload(trigger="after_accept"),
            self._payload(trigger=None),
        ):
            resp = self._post(payload)
            self.assertEqual(resp.data["code"], 1000, resp.data)
            self.assertIn("回复时机", json.dumps(resp.data, ensure_ascii=False))
        self.assertEqual(PlatformAutoReplyRule.objects.count(), before)

    # ── 4. 正文必填 / 上限 200 ──────────────────────────────────────
    def test_content_is_required_and_capped_at_200(self):
        for payload in (
            {k: v for k, v in self._payload().items() if k != "content"},
            self._payload(content=""),
            self._payload(content="   "),
            self._payload(content=None),
        ):
            resp = self._post(payload)
            self.assertEqual(resp.data["code"], 1000, resp.data)
            self.assertIn("回复内容", json.dumps(resp.data, ensure_ascii=False))

        too_long = self._post(self._payload(content="话" * 201))
        self.assertEqual(too_long.data["code"], 1000, too_long.data)
        self.assertIn("回复内容", json.dumps(too_long.data, ensure_ascii=False))
        self.assertEqual(PlatformAutoReplyRule.objects.count(), 0)

        # 边界：恰好 200 个字必须收（证据件 maxlength=200），且首尾空白被去掉。
        exact = self._post(self._payload(content=("   " + "话" * 200 + "   ")))
        self.assertEqual(exact.data["code"], 0, exact.data)
        self.assertEqual(len(PlatformAutoReplyRule.objects.get().content), 200)

    # ── 5. 图片：只写对象名，读侧只回 has_image / image_url ───────────
    def _put_object(self, key: str) -> None:
        """真的往存储里放一个对象（否则写侧的 `head()` 会按「不存在」拒）。"""
        storage = get_storage()
        storage.put_bytes(key, b"\x89PNG\r\n\x1a\nsynthetic", content_type="image/png")
        self.addCleanup(storage.delete, key)

    def _own_key(self, name: str = "synthetic.png") -> str:
        """本租户前缀下、**本页上传端点形状**的对象名（`build_platform_reply_key` 的形态）。

        ⚠️ B1）起写侧多了一道形状闸：只收 `<schema>/platform-auto-replies/…`
        ⇒ 夹具必须用这个前缀（原先是 `<schema>/reply/…`）。
        """
        return f"{connection.schema_name}/{PLATFORM_REPLY_SEGMENT}/{name}"

    def test_image_key_is_write_only_and_read_side_answers_has_image(self):
        key = self._own_key()
        self._put_object(key)
        created = self._post(self._payload(image_key=key))
        self.assertEqual(created.data["code"], 0, created.data)
        row = PlatformAutoReplyRule.objects.get()
        self.assertEqual(row.image_key, key)

        item = self._get().data["data"][0]
        self.assertTrue(item["has_image"])
        # `image_url` 是**现签**的链接（本机存储形态是相对路径 `/api/media/blob/<key>`），
        # 断言它指得回那个对象名即可；⛔ 不把签名当成稳定值钉死。
        self.assertIn("synthetic.png", item["image_url"])
        # 对象名**不以 `image_key` 这个键下发**（读侧只给 has_image / image_url）。
        # ⚠️ 本机存储的 `image_url` 里**含**对象名（`/api/media/blob/<key>`）——那不算泄漏
        # 对象名这个判据，判据是「有没有 `image_key` 这个字段」。
        dumped = json.dumps(item, ensure_ascii=False)
        self.assertNotIn('"image_key"', dumped)

        # 清空图片是一次明确的写操作（空串）：落库为空、读侧 has_image=False、**不查对象**。
        cleared = self._patch(row.pk, {"image_key": ""})
        self.assertEqual(cleared.data["code"], 0, cleared.data)
        row.refresh_from_db()
        self.assertEqual(row.image_key, "")
        self.assertFalse(self._get().data["data"][0]["has_image"])

    def test_image_key_from_another_tenant_prefix_is_rejected(self):
        """**跨租户护栏**（评审 I1）：别的租户前缀的对象名一律拒、且**不落库**。

        生产是共享 COS 桶 ⇒ 放行就等于「持本节点的人能给别人的对象签读链接」。
        ⚠️ 这里**故意**把对象真的放进去（用对方前缀）—— 判的是**前缀闸**，不是「对象不存在」。
        """
        foreign_key = "another-tenant/reply/synthetic.png"
        self._put_object(foreign_key)

        created = self._post(self._payload(image_key=foreign_key))
        self.assertEqual(created.data["code"], 1000, created.data)
        self.assertIn("图片", json.dumps(created.data, ensure_ascii=False))
        self.assertIn("不属于本租户", json.dumps(created.data, ensure_ascii=False))
        self.assertEqual(PlatformAutoReplyRule.objects.count(), 0)

        # PATCH 同样拦：先建一条干净的，再试图改成别人的对象 ⇒ 1000 且原值不变。
        clean = self._post(self._payload())
        self.assertEqual(clean.data["code"], 0, clean.data)
        pk = clean.data["data"]["id"]
        patched = self._patch(pk, {"image_key": foreign_key})
        self.assertEqual(patched.data["code"], 1000, patched.data)
        self.assertIn("不属于本租户", json.dumps(patched.data, ensure_ascii=False))
        self.assertEqual(PlatformAutoReplyRule.objects.get(pk=pk).image_key, "")

    def test_image_key_must_point_to_an_existing_object(self):
        """对象不存在 ⇒ 拒绝（评审 I1 的第二道；也顺手消掉「has_image=true 但链接 404」）。"""
        resp = self._post(self._payload(image_key=self._own_key("missing.png")))
        self.assertEqual(resp.data["code"], 1000, resp.data)
        self.assertIn("图片对象不存在", json.dumps(resp.data, ensure_ascii=False))
        self.assertEqual(PlatformAutoReplyRule.objects.count(), 0)

        # ── 6. 审计留痕 ────────────────────────────────────────────────
    def test_write_actions_are_audited_and_noop_is_not_logged(self):
        content = "合成话术：审计用。"
        created = self._post(self._payload(content=content, trigger=TRIGGER_AFTER_REVIEW_REQUEST))
        pk = created.data["data"]["id"]

        create_log = OperationLog.objects.get(action="system.platform_auto_reply_create")
        self.assertEqual(create_log.target_kind, "platform_reply")
        self.assertEqual(create_log.target_id, str(pk))
        self.assertEqual(create_log.target_name, "合成游戏甲 · 申请验收后")
        self.assertEqual(create_log.log_type, 40)  # LogType.SYSTEM

        # 纯 no-op 的 PATCH 不写留痕（content 与 is_enabled 都与当前值相同）。
        before = OperationLog.objects.count()
        noop = self._patch(pk, {"content": content, "is_enabled": True})
        self.assertEqual(noop.data["code"], 0, noop.data)
        self.assertEqual(OperationLog.objects.count(), before)

        # 真改 ⇒ 恰好一条 update。
        self._patch(pk, {"content": "合成话术：改过。"})
        self.assertEqual(
            OperationLog.objects.filter(action="system.platform_auto_reply_update").count(),
            1,
        )

        # 行内启用开关（页面那颗 switch 发的就是这一条 PATCH）。
        self._patch(pk, {"is_enabled": False})
        self.assertEqual(
            OperationLog.objects.filter(action="system.platform_auto_reply_update").count(),
            2,
        )

        # 删除：`target_id` 在删除前取 ⇒ 删完仍指得回那一行。
        self._delete(pk)
        destroy_log = OperationLog.objects.get(action="system.platform_auto_reply_destroy")
        self.assertEqual(destroy_log.target_id, str(pk))
        self.assertEqual(destroy_log.target_name, "合成游戏甲 · 申请验收后")

        # ⛔ 正文不进日志：任何文本列里都不许出现那句话（两条正文都搜）。
        logged = json.dumps(
            list(
                OperationLog.objects.values_list(
                    "summary", "target_name", "target_id", "field_name",
                    "from_value", "to_value", "reason",
                )
            ),
            ensure_ascii=False,
        )
        self.assertNotIn(content, logged)
        self.assertNotIn("合成话术：改过。", logged)

    # ── 7. 未授权 403 / 未登录 401 ───────────────────────────────────
    def test_unauthorized_requests_are_403_and_anonymous_is_401(self):
        created = self._post(self._payload())
        pk = created.data["data"]["id"]

        # ⚠️ 用**不加入任何组**的账号：`setUp` 那个 client 已经把本点挂到「客服」组上了，
        # 再用同一个组会让这个"无权限"账号**继承**那个节点（403 变成 200，用例假红）。
        no_node = make_logged_in_client(username="par-no-node")
        self.assertEqual(self._get(client=no_node).status_code, 403)
        self.assertEqual(self._post(self._payload(), client=no_node).status_code, 403)
        self.assertEqual(no_node.get(detail_url(pk)).status_code, 403)
        self.assertEqual(
            no_node.patch(
                detail_url(pk), {"content": "无权限改"}, content_type="application/json"
            ).status_code,
            403,
        )
        self.assertEqual(no_node.delete(detail_url(pk)).status_code, 403)

        # 零写入对照：403 的 PATCH / DELETE 没动到那一行。
        row = PlatformAutoReplyRule.objects.get(pk=pk)
        self.assertEqual(row.content, "合成话术：您好，已接单。")

        anonymous = Client(SERVER_NAME=DOMAIN_A)
        self.assertEqual(anonymous.get(URL_LIST).status_code, 401)

        # 对照：持点客户端的同样请求是通的（否则上面的 403 可能只是「端点坏了」）。
        self.assertEqual(self._get().status_code, 200)

    # ── 8. 平台：默认代练通；只收代练通 ──────────────────────────────
    def test_platform_defaults_to_dailiantong_and_only_accepts_it(self):
        payload = self._payload()
        payload.pop("platform")
        created = self._post(payload)
        self.assertEqual(created.data["code"], 0, created.data)
        self.assertEqual(PlatformAutoReplyRule.objects.get().platform, 10)

        before = PlatformAutoReplyRule.objects.count()
        # `12345` 连模型 choices 都不是（DRF 的 `invalid_choice` 收口）；
        # `30`（代练妈妈）**在 choices 里**但本版没有客户端 ⇒ 由 `validate_platform` 收口。
        # 两条都必须拒（评审 M-1：接口只收代练通，与「交易平台对接设置」那 7 个不是一回事）。
        for bad_platform in (12345, 30):
            bad = self._post(self._payload(platform=bad_platform))
            self.assertEqual(bad.data["code"], 1000, (bad_platform, bad.data))
            self.assertIn("平台", json.dumps(bad.data, ensure_ascii=False))
        # 后者要说清「只有代练通」（前者只是"不在可选范围"，没必要提客户端）。
        bad_enabled = self._post(self._payload(platform=30))
        self.assertIn("代练通", json.dumps(bad_enabled.data, ensure_ascii=False))
        self.assertEqual(PlatformAutoReplyRule.objects.count(), before)


#: 真的 PNG 魔数（`storage.IMAGE_MAGIC` 认它；后随合成字节，不是任何真实图片）。
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"synthetic-reply-image"
#: 真的 JPEG 魔数（同上；用来判「扩展名跟着**内容**走」）。
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"synthetic-reply-image"


class _TempMediaRootMixin:
    """把 `MEDIA_ROOT` 指到临时目录（同 `tests/test_game_logo_api.py` 的同名 mixin）。

    ⚠️ 不覆盖的话 `get_storage()` 会往仓库根 `.tools/media` 里写文件——那是本机数据。
    """

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        patcher.enable()
        self.addCleanup(patcher.disable)


class PlatformAutoReplyImageUploadTests(_TempMediaRootMixin, TenantTestCase):
    """规则图片上传端点（B1，2026-10-27）：服务端 multipart → 既有媒体存储。

    判据一律打在**存储终态 / 数据库行**上（`AGENTS.md` §5-G），不看提示文案的措辞以外的东西：
    - 成功：对象真的在存储里、大小对、对象名带**本租户前缀**且是规则图那一族；
    - 失败：业务码 1000 + 中文原因，且存储里**没有**多出对象；
    - 写侧：上传产出的 key 能挂到规则上（读侧 `has_image` / 现签链接）；别的形状一律拒。
    """

    UPLOAD_URL = f"{URL_LIST}upload-image/"

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.client = make_logged_in_client(
            username="par-upload-user", groups=("客服",), nodes=(NODE,)
        )

    # ── 小工具 ──────────────────────────────────────────────────────
    def _upload(self, data: bytes, *, name: str = "synthetic.png",
                content_type: str = "image/png", client=None):
        return (client or self.client).post(
            self.UPLOAD_URL,
            {"file": SimpleUploadedFile(name, data, content_type=content_type)},
        )

    def _stored_files(self) -> list[pathlib.Path]:
        """本租户的规则图片目录里现存的**文件**（判「失败时没有多出对象」）。"""
        root = pathlib.Path(self.settings_media_root()) / connection.schema_name / PLATFORM_REPLY_SEGMENT
        if not root.exists():
            return []
        return sorted(p for p in root.iterdir() if p.is_file())

    def settings_media_root(self) -> pathlib.Path:
        from django.conf import settings
        return pathlib.Path(settings.MEDIA_ROOT)

    # ── 1. 成功：对象名 / 大小 / 读链接 / 留痕 ─────────────────────────
    def test_upload_stores_the_object_under_the_tenant_prefix(self):
        resp = self._upload(PNG_BYTES, name="任意名字.dat")
        self.assertEqual(resp.data["code"], 0, resp.data)
        key = resp.data["data"]["image_key"]

        # 对象名 = `<本租户 schema>/platform-auto-replies/<随机>.png`：
        # 租户前缀是跨租户隔离的钥匙；后缀由**魔数**判出来（与文件名 `任意名字.dat` 无关）。
        self.assertTrue(
            key.startswith(f"{connection.schema_name}/{PLATFORM_REPLY_SEGMENT}/"), key
        )
        self.assertTrue(key.endswith(".png"), key)
        self.assertEqual(key.count("/"), 2, key)  # 只有 `<租户>/<段>/<文件>` 三段

        storage = get_storage()
        meta = storage.head(key)
        self.addCleanup(storage.delete, key)
        self.assertIsNotNone(meta, "上传成功却没在存储里找到对象")
        self.assertEqual(meta[0], len(PNG_BYTES))

        # 读链接是**现签**的短时效链接（本机形态指回 `/api/media/blob/<key>`）。
        self.assertIn(key, resp.data["data"]["url"])
        self.assertGreater(resp.data["data"]["expires_in"], 0)

        # 留痕：恰好一条上传动作，且**不含**对象名 / 文件名 / 内容。
        log = OperationLog.objects.get(action="system.platform_auto_reply_image_upload")
        self.assertEqual(log.log_type, 40)  # LogType.SYSTEM
        self.assertEqual(log.target_kind, "platform_reply")
        self.assertIn("上传了交易平台自动回复图片", log.summary)
        for text in (key, "任意名字.dat", PNG_BYTES.decode("latin-1")):
            self.assertNotIn(text, json.dumps(
                list(OperationLog.objects.values_list(
                    "summary", "target_name", "target_id", "field_name",
                    "from_value", "to_value", "reason",
                )),
                ensure_ascii=False,
            ))

    def test_jpeg_bytes_get_a_jpg_extension(self):
        resp = self._upload(JPEG_BYTES, name="synthetic.png", content_type="image/png")
        self.assertEqual(resp.data["code"], 0, resp.data)
        key = resp.data["data"]["image_key"]
        self.addCleanup(get_storage().delete, key)
        self.assertTrue(key.endswith(".jpg"), key)

    # ── 2. 上传出来的 key 能挂到规则上（整链） ───────────────────────
    def test_uploaded_key_can_be_attached_to_a_rule(self):
        uploaded = self._upload(PNG_BYTES)
        self.assertEqual(uploaded.data["code"], 0, uploaded.data)
        key = uploaded.data["data"]["image_key"]
        self.addCleanup(get_storage().delete, key)

        created = self.client.post(
            URL_LIST,
            {
                "platform": 10,
                "game": None,
                "trigger": TRIGGER_AFTER_ACCEPT,
                "content": "合成话术：带图规则。",
                "image_key": key,
            },
            content_type="application/json",
        )
        self.assertEqual(created.data["code"], 0, created.data)
        row = PlatformAutoReplyRule.objects.get()
        self.assertEqual(row.image_key, key)

        item = self.client.get(URL_LIST).data["data"][0]
        self.assertTrue(item["has_image"])
        self.assertIn(key, item["image_url"])
        self.assertNotIn('"image_key"', json.dumps(item, ensure_ascii=False))

    # ── 3. 拒收矩阵：类型 / 大小 / 空 / 缺字段，一律 1000 且零写入 ─────
    def test_non_image_bytes_are_rejected_without_writing_anything(self):
        resp = self._upload(b"<html>not an image</html>", name="fake.png")
        self.assertEqual(resp.data["code"], 1000, resp.data)
        self.assertIn("只支持 jpg / jpeg / png", json.dumps(resp.data, ensure_ascii=False))
        self.assertEqual(self._stored_files(), [])
        self.assertFalse(
            OperationLog.objects.filter(
                action="system.platform_auto_reply_image_upload"
            ).exists()
        )

    def test_oversize_image_is_rejected_without_writing_anything(self):
        """超过端点上限（2 MiB）⇒ 1000 + 中文原因，且零写入。

        ⚠️ 体量刻意压在 **Django 出厂请求体墙（2.5 MiB）以下**：超过那道墙的 multipart 请求在
        进视图前就被 `NonDirectUploadBodyLimitMiddleware` 回 400 空体（E-0095）⇒ 那测的就不是
        本端点了。端点的上限必须小于墙，用户才看得到中文原因。
        """
        too_big = PNG_BYTES + b"0" * (2 * 1024 * 1024)
        resp = self._upload(too_big)
        self.assertEqual(resp.data["code"], 1000, resp.data)
        self.assertIn("2 MB", json.dumps(resp.data, ensure_ascii=False))
        self.assertEqual(self._stored_files(), [])

    def test_empty_missing_or_wrong_field_is_rejected(self):
        empty = self._upload(b"")
        self.assertEqual(empty.data["code"], 1000, empty.data)
        self.assertIn("图片无效或为空", json.dumps(empty.data, ensure_ascii=False))

        missing = self.client.post(self.UPLOAD_URL, {})
        self.assertEqual(missing.data["code"], 1000, missing.data)
        self.assertIn("请选择要上传的图片", json.dumps(missing.data, ensure_ascii=False))

        # 字段名不对（页面契约是 `file`）⇒ 同一个「请选择」文案，⛔ 不 500。
        wrong_field = self.client.post(
            self.UPLOAD_URL,
            {"image": SimpleUploadedFile("a.png", PNG_BYTES, content_type="image/png")},
        )
        self.assertEqual(wrong_field.data["code"], 1000, wrong_field.data)
        self.assertEqual(self._stored_files(), [])

    # ── 4. 权限：同页面节点；未登录 401 ─────────────────────────────
    def test_upload_requires_the_page_node_and_login(self):
        no_node = make_logged_in_client(username="par-upload-no-node")
        self.assertEqual(
            self._upload(PNG_BYTES, client=no_node).status_code, 403,
        )
        anonymous = Client(SERVER_NAME=DOMAIN_A)
        self.assertEqual(
            anonymous.post(
                self.UPLOAD_URL,
                {"file": SimpleUploadedFile("a.png", PNG_BYTES, content_type="image/png")},
            ).status_code,
            401,
        )
        self.assertEqual(self._stored_files(), [])

    # ── 5. 写侧形状闸：只收本页上传产出的那一族 ─────────────────────
    def test_rule_write_rejects_a_tenant_owned_key_outside_the_upload_family(self):
        """同租户、真实存在、但**不是**规则图那一族（例如订单截图）⇒ 拒、且不落库。

        理由：`presign_get` 会把它签成可读链接 ⇒ 放行等于让规则页成为「任意本租户对象」
        的读取入口。
        """
        foreign_shape = f"{connection.schema_name}/orders/synthetic-order/x.png"
        storage = get_storage()
        storage.put_bytes(foreign_shape, PNG_BYTES, content_type="image/png")
        self.addCleanup(storage.delete, foreign_shape)

        resp = self.client.post(
            URL_LIST,
            {
                "platform": 10,
                "game": None,
                "trigger": TRIGGER_AFTER_ACCEPT,
                "content": "合成话术：坏图。",
                "image_key": foreign_shape,
            },
            content_type="application/json",
        )
        self.assertEqual(resp.data["code"], 1000, resp.data)
        self.assertIn("图片", json.dumps(resp.data, ensure_ascii=False))
        self.assertEqual(PlatformAutoReplyRule.objects.count(), 0)

        # 对照：确属上传族的 key 能挂上（否则上面的 1000 可能只是写侧坏了）。
        uploaded = self._upload(PNG_BYTES)
        key = uploaded.data["data"]["image_key"]
        self.addCleanup(storage.delete, key)
        ok = self.client.post(
            URL_LIST,
            {
                "platform": 10,
                "game": None,
                "trigger": TRIGGER_AFTER_ACCEPT,
                "content": "合成话术：好图。",
                "image_key": key,
            },
            content_type="application/json",
        )
        self.assertEqual(ok.data["code"], 0, ok.data)


class PlatformAutoReplyIsolationTests(TenantTestCase):
    """以租户 A 的凭据访问 B 的数据必须失败（`AGENTS.md` §4 的强制测试）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="自动回复隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        connection.set_tenant(self.tenant)

    def _seed(self):
        """两个租户各造自己的行 + 各自的登录 client，返回 `(pk_a, pk_b, client_a, client_b)`。

        A 造两行、B 造一行 ⇒ A 的目标主键必然大于 B 的最大主键；用例自己断言这件事
        （⛔ 不靠推测——两个 schema 的主键序列各自从 1 开始）。
        """
        with tenant_context(self.tenant):
            PlatformAutoReplyRule.objects.create(
                trigger=TRIGGER_AFTER_ACCEPT, content="A-ONLY-1"
            )
            row_a = PlatformAutoReplyRule.objects.create(
                trigger=TRIGGER_AFTER_REVIEW_REQUEST, content="A-ONLY-2"
            )
            client_a = make_logged_in_client(
                domain=DOMAIN_A, username="par-iso-a", groups=("客服",), nodes=(NODE,)
            )
        with tenant_context(self.tenant_b):
            row_b = PlatformAutoReplyRule.objects.create(
                trigger=TRIGGER_AFTER_ACCEPT, content="B-ONLY-1"
            )
            client_b = make_logged_in_client(
                domain=DOMAIN_B, username="par-iso-b", groups=("客服",), nodes=(NODE,)
            )
        self.assertNotEqual(
            row_a.pk, row_b.pk,
            "夹具没造对：两租户的主键撞了 ⇒ 下面的跨租户用例测不到隔离",
        )
        return row_a.pk, row_b.pk, client_a, client_b

    def _contents_in(self, tenant):
        with tenant_context(tenant):
            return sorted(PlatformAutoReplyRule.objects.values_list("content", flat=True))

    def test_each_tenant_lists_only_its_own_rows(self):
        _pk_a, _pk_b, client_a, client_b = self._seed()

        contents_a = [item["content"] for item in client_a.get(URL_LIST).data["data"]]
        contents_b = [item["content"] for item in client_b.get(URL_LIST).data["data"]]
        self.assertEqual(contents_a, ["A-ONLY-1", "A-ONLY-2"])
        self.assertEqual(contents_b, ["B-ONLY-1"])

    def test_tenant_b_cannot_patch_or_delete_tenant_a_rows(self):
        pk_a, _pk_b, _client_a, client_b = self._seed()

        try:
            patched = client_b.patch(
                detail_url(pk_a),
                {"content": "跨租户改"},
                content_type="application/json",
                SERVER_NAME=DOMAIN_B,
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(patched.status_code, 200, patched.content[:300])
        self.assertEqual(patched.data["code"], 1002, patched.data)

        try:
            deleted = client_b.delete(detail_url(pk_a), SERVER_NAME=DOMAIN_B)
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(deleted.status_code, 200, deleted.content[:300])
        self.assertEqual(deleted.data["code"], 1002, deleted.data)

        # 判据打在**业务值**上（不是主键）：A 的两行原样还在，B 那边也没多出行。
        self.assertEqual(self._contents_in(self.tenant), ["A-ONLY-1", "A-ONLY-2"])
        self.assertEqual(self._contents_in(self.tenant_b), ["B-ONLY-1"])

    def test_positive_control_each_tenant_can_edit_its_own_row(self):
        """非空哨兵：两个租户各自改自己那行都能成功（否则上面的 1002 可能只是接口坏了）。"""
        pk_a, pk_b, client_a, client_b = self._seed()

        try:
            resp_a = client_a.patch(
                detail_url(pk_a),
                {"content": "A 自己改"},
                content_type="application/json",
                SERVER_NAME=DOMAIN_A,
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(resp_a.data["code"], 0, resp_a.data)

        try:
            resp_b = client_b.patch(
                detail_url(pk_b),
                {"content": "B 自己改"},
                content_type="application/json",
                SERVER_NAME=DOMAIN_B,
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(resp_b.data["code"], 0, resp_b.data)

        self.assertEqual(sorted(self._contents_in(self.tenant)), sorted(["A-ONLY-1", "A 自己改"]))
        self.assertEqual(self._contents_in(self.tenant_b), ["B 自己改"])


class PlatformAutoReplyUniquenessTests(TenantTestCase):
    """⭐ 一键抛单 B2（2026-10-27）：同一 `(平台, 游戏, 时机)` 只能一条（含停用、含全部游戏）。

    两道都要钉：接口层给**带字段名的中文错误**（业务码 1000），库层**真的**有唯一约束
    （绕过接口直接建第二条必须 `IntegrityError`）。
    """

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.game = Game.objects.create(name="合成唯一性游戏")
        self.other_game = Game.objects.create(name="合成唯一性游戏二")
        self.client = make_logged_in_client(
            username="par-unique-user", groups=("客服",), nodes=(NODE,)
        )

    def _post(self, **overrides):
        payload = {
            "platform": 10,
            "game": self.game.pk,
            "trigger": TRIGGER_AFTER_ACCEPT,
            "content": "合成话术：唯一性。",
            "is_enabled": True,
        }
        payload.update(overrides)
        return self.client.post(URL_LIST, payload, content_type="application/json")

    def test_same_platform_game_trigger_is_rejected_with_a_field_named_message(self):
        first = self._post()
        self.assertEqual(first.data["code"], 0, first.data)

        second = self._post(content="第二套话术")

        self.assertEqual(second.data["code"], 1000, second.data)
        self.assertIn("只能有一条", json.dumps(second.data, ensure_ascii=False))
        self.assertIn("时机", json.dumps(second.data, ensure_ascii=False))
        self.assertEqual(PlatformAutoReplyRule.objects.count(), 1)

    def test_a_disabled_rule_still_occupies_the_combination(self):
        """停用的也算 ⇒ 「停用一条再配一条」不能绕过（计划 §2.2）。"""
        disabled = self._post(is_enabled=False)
        self.assertEqual(disabled.data["code"], 0, disabled.data)

        second = self._post(is_enabled=True)

        self.assertEqual(second.data["code"], 1000, second.data)
        self.assertEqual(PlatformAutoReplyRule.objects.count(), 1)

    def test_all_games_null_combination_is_also_unique(self):
        first = self._post(game=None)
        self.assertEqual(first.data["code"], 0, first.data)

        second = self._post(game=None, trigger=TRIGGER_AFTER_ACCEPT)

        self.assertEqual(second.data["code"], 1000, second.data)
        self.assertEqual(PlatformAutoReplyRule.objects.filter(game__isnull=True).count(), 1)

    def test_a_different_game_or_trigger_is_allowed(self):
        self.assertEqual(self._post().data["code"], 0)
        # 另一款游戏：允许。
        self.assertEqual(self._post(game=self.other_game.pk).data["code"], 0)
        # 同一游戏、另一个时机：允许。
        self.assertEqual(
            self._post(trigger=TRIGGER_AFTER_REVIEW_REQUEST).data["code"], 0
        )
        # 全部游戏 + 另一个时机：允许（NULL 只与同触发时机的那条比）。
        self.assertEqual(
            self._post(game=None, trigger=TRIGGER_AFTER_REVIEW_REQUEST).data["code"],
            0,
        )
        self.assertEqual(PlatformAutoReplyRule.objects.count(), 4)

    def test_patching_into_an_existing_combination_is_rejected_and_keeps_the_row(self):
        first = self._post()
        pk = first.data["data"]["id"]
        second = self._post(trigger=TRIGGER_AFTER_REVIEW_REQUEST)
        second_pk = second.data["data"]["id"]

        response = self.client.patch(
            detail_url(second_pk),
            {"trigger": TRIGGER_AFTER_ACCEPT},
            content_type="application/json",
        )

        self.assertEqual(response.data["code"], 1000, response.data)
        self.assertIn("只能有一条", json.dumps(response.data, ensure_ascii=False))
        self.assertEqual(
            PlatformAutoReplyRule.objects.get(pk=second_pk).trigger,
            TRIGGER_AFTER_REVIEW_REQUEST,
        )
        self.assertEqual(PlatformAutoReplyRule.objects.get(pk=pk).trigger, TRIGGER_AFTER_ACCEPT)

    def test_the_database_rejects_a_second_row_even_without_the_api(self):
        """库层判据：绕过接口直接建第二条必须 `IntegrityError`（⛔ 只靠应用层查一遍不够）。"""
        base = PlatformAutoReplyRule.objects.create(
            platform=10,
            game=self.game,
            trigger=TRIGGER_AFTER_ACCEPT,
            content="第一条",
        )
        self.assertEqual(base.platform, 10)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PlatformAutoReplyRule.objects.create(
                    platform=10,
                    game=self.game,
                    trigger=TRIGGER_AFTER_ACCEPT,
                    content="第二条",
                    is_enabled=False,
                )

        # `game IS NULL` 走 `NULLS NOT DISTINCT`：第二条同样必须被拒。
        PlatformAutoReplyRule.objects.create(
            platform=10, game=None, trigger=TRIGGER_AFTER_ACCEPT, content="全部游戏一"
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PlatformAutoReplyRule.objects.create(
                    platform=10,
                    game=None,
                    trigger=TRIGGER_AFTER_ACCEPT,
                    content="全部游戏二",
                )
