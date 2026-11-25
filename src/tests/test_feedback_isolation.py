"""问题反馈的跨租户隔离（spec `2026-10-08-feedback-design.md` §9 判据 3；
`AGENTS.md` §4 硬规则：任何新增查询面都要有一条「以租户 A 的凭据访问 B 的数据必须失败」）。

⚠️ **跨租户的否定断言一律以「租户内唯一、且本例两租户取值不同的业务值」为判据**
（本文件用 `Feedback.content` 的两个字面量），⛔ **不是主键**：两个 schema 的主键序列**各自从 1
开始**，拿 A 的 pk 去 B 里查会命中 **B 自己的行** ⇒ 主键判据恒真或恒假。

⚠️ 主键在本文件里只当**入参**用（`PATCH` 的 URL 就要 pk），所以造数时**故意让两个租户的
pk 错开**（A 两行、B 一行）并在用例里**自证错开**——否则「B 拿 A 的 pk ⇒ 404」会因为 B 恰好也有
同一 pk 的行而变成 200（那不是隔离失效，是夹具没造对）。

⚠️ 请求一律发在 `tenant_context(...)` 块**之外** + 显式 `SERVER_NAME`（同
`tests/test_read_platform_order_isolation.py` 的教训）：块内发请求时 `connection.schema_name`
已经是被测租户 ⇒ 通过与否不取决于「中间件有没有按域名切 schema」。

⚠️ 夹具值一律**合成值**（PII 守卫扫 `src/**`）。
"""
from django.contrib.auth.models import User
from django.db import connection
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import Feedback
from apps.core.storage import get_storage
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

NODE = "home.feedback"
URL_LIST = "/api/business/feedback/"
URL_TICKET = "/api/business/feedback/media/ticket/"


def detail_url(pk) -> str:
    return f"/api/business/feedback/{pk}/"


#: 租户 A = `TenantTestCase` 的 `test` schema（与既有隔离测试同口径）。
DOMAIN_A = "tenant.test.com"
SCHEMA_A = "test"
#: 租户 B 的域名：后缀 `.test.local` 在 `.env` 的 `DJANGO_ALLOWED_HOSTS` 白名单里。
DOMAIN_B = "feedback-iso-b.test.local"
SCHEMA_B = "feedback_iso_b"

# ── 判据用的业务值（租户内唯一、两租户取值不同）────────────────────────────────
CONTENT_A1 = "仅属于A的反馈甲"
CONTENT_A2 = "仅属于A的反馈乙"
CONTENT_B1 = "仅属于B的反馈甲"

PNG = b"\x89PNG\r\n\x1a\n" + b"feedback-iso-body"


class FeedbackIsolationTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="反馈隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 跨域名请求不会在响应结束时还原 schema（见 `tests/test_p3b_isolation.py` 的模块说明），
        #    所以每条用例开头把连接显式钉回租户 A。
        connection.set_tenant(self.tenant)
        self.storage = get_storage()

    # ── 夹具 ────────────────────────────────────────────────────────
    def _seed(self):
        """两个租户各造自己的数据 + 各自的登录 client，返回 `(pk_a, pk_b, clients)`。

        A 两行、B 一行 ⇒ A 的目标 pk（第 2 行）**必然大于** B 的最大 pk（1）
        —— 这一点由用例自己断言（见 `_assert_pks_disjoint`），⛔ 不靠推测。
        """
        with tenant_context(self.tenant):
            user_a = User.objects.create_user(username="iso-user-a", password="pw-iso-a")
            row_a1 = Feedback.objects.create(content=CONTENT_A1, created_by=user_a)
            row_a2 = Feedback.objects.create(content=CONTENT_A2, created_by=user_a)
            admin_a = make_logged_in_client(
                domain=DOMAIN_A, username="iso-admin-a", groups=("系统管理员",), nodes=(NODE,)
            )
        with tenant_context(self.tenant_b):
            user_b = User.objects.create_user(username="iso-user-b", password="pw-iso-b")
            row_b1 = Feedback.objects.create(content=CONTENT_B1, created_by=user_b)
            admin_b = make_logged_in_client(
                domain=DOMAIN_B, username="iso-admin-b", groups=("系统管理员",), nodes=(NODE,)
            )
        self._assert_pks_disjoint(row_a2.pk, row_b1.pk)
        return row_a1.pk, row_a2.pk, row_b1.pk, admin_a, admin_b

    def _assert_pks_disjoint(self, pk_a, pk_b):
        """自证夹具造对了：两个 schema 的主键序列各自从 1 开始 ⇒ 必须**故意错开**。

        没有这条自证，`PATCH <A 的 pk>` 在 B 里可能命中 **B 自己的行** ⇒ 返回 200，
        而那是**正确行为**被误读成「隔离失效」（或反过来，恒 404 的假绿）。
        """
        self.assertNotEqual(
            pk_a, pk_b,
            "夹具没造对：两租户的目标 pk 撞了 ⇒ 下面「B 拿 A 的 pk」那条测不到隔离",
        )

    def _patch(self, client, pk, payload, server_name):
        """在指定域名上 PATCH，**结束后把连接钉回租户 A**。"""
        try:
            return client.patch(
                detail_url(pk), payload, content_type="application/json",
                SERVER_NAME=server_name,
            )
        finally:
            connection.set_tenant(self.tenant)

    def _contents_in(self, tenant):
        """该租户 schema 里现有反馈的正文（**跨租户判据只认它**）。"""
        with tenant_context(tenant):
            return sorted(Feedback.objects.values_list("content", flat=True))

    # ── 判据 3：B 拿 A 的 pk 调 PATCH ⇒ 404，且 A 那行没被改 ──────────────
    def test_patching_another_tenants_pk_is_404_and_changes_nothing(self):
        _pk_a1, pk_a2, pk_b1, _admin_a, admin_b = self._seed()

        resp = self._patch(admin_b, pk_a2, {"status": "handled", "admin_note": "跨租户改"}, DOMAIN_B)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["code"], 1002, resp.data)
        self.assertEqual(resp.data["msg"], "记录不存在或已被删除")
        # 判据打在**业务值**上（不是主键）：A 那两行的状态一个字没变。
        # ⚠️ 两侧都过同一个 `sorted`（中文按码位排，「甲」在「乙」之后）——只归一化一侧
        #    会让这条断言看起来更严、实际更脆（E-0083 同族）。
        with tenant_context(self.tenant):
            self.assertEqual(
                sorted(Feedback.objects.values_list("content", "status")),
                sorted([(CONTENT_A1, "pending"), (CONTENT_A2, "pending")]),
            )
            self.assertEqual(
                Feedback.objects.filter(admin_note="跨租户改").count(), 0
            )
        # 反方向同样没被碰：B 自己那一行也还是未处理。
        with tenant_context(self.tenant_b):
            self.assertEqual(
                list(Feedback.objects.values_list("content", "status")),
                [(CONTENT_B1, "pending")],
            )

    def test_positive_control_each_tenant_can_handle_its_own_row(self):
        """非空哨兵：两个租户**各自**能处置自己那一行（否则上面的 404 可能只是「接口坏了」）。"""
        _pk_a1, pk_a2, pk_b1, admin_a, admin_b = self._seed()

        resp_b = self._patch(admin_b, pk_b1, {"status": "handled"}, DOMAIN_B)
        self.assertEqual(resp_b.data["code"], 0, resp_b.data)
        resp_a = self._patch(admin_a, pk_a2, {"status": "handled"}, DOMAIN_A)
        self.assertEqual(resp_a.data["code"], 0, resp_a.data)

        self.assertEqual(self._contents_in(self.tenant_b), [CONTENT_B1])
        with tenant_context(self.tenant_b):
            self.assertEqual(Feedback.objects.get().status, "handled")
        with tenant_context(self.tenant):
            self.assertEqual(Feedback.objects.get(pk=pk_a2).status, "handled")
            self.assertEqual(Feedback.objects.get(pk=pk_a2).handled_by.username, "iso-admin-a")
        with tenant_context(self.tenant_b):
            self.assertEqual(Feedback.objects.get(pk=pk_b1).handled_by.username, "iso-admin-b")

    def test_each_tenant_lists_only_its_own_rows(self):
        """列表方向：B 的管理员看不到 A 的正文（判据是正文这个业务值，⛔ 不是主键）。"""
        _pk_a1, _pk_a2, _pk_b1, _admin_a, admin_b = self._seed()

        try:
            body = admin_b.get(URL_LIST, SERVER_NAME=DOMAIN_B).data
        finally:
            connection.set_tenant(self.tenant)

        self.assertEqual(body["code"], 0)
        contents = [item["content"] for item in body["data"]["items"]]
        self.assertEqual(contents, [CONTENT_B1])
        # ⚠️ 这里原先还有两条 `assertNotIn(CONTENT_A1/A2, contents)`——它们是**恒真的**
        #    （紧跟在上面的等号断言之后，contents 已经被钉成 `[CONTENT_B1]`）⇒ 已删掉，
        #    换成两条**能独立失败**的判据（`AGENTS.md` §5-G：断言必须能失败）：
        #    ① 响应里的计数与 items 一致（另一条代码路径）；
        #    ② **另一个租户的库**没被动过（与「B 的响应里有什么」完全独立的事实）。
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(
            self._contents_in(self.tenant), sorted([CONTENT_A1, CONTENT_A2])
        )

    # ── 跨租户的**对象名**：拒收，且**不碰**别的租户的字节 ─────────────────
    def test_submitting_another_tenants_media_key_neither_writes_nor_deletes(self):
        """B 拿 A 命名空间下的 key 提交 ⇒ 403、B 零新增行，且**A 的对象原样还在**。

        ⚠️ 这条钉的是清理路径的**跨租户**那一半：判据的唯一出处是
        `apps/business/api/feedback_views.py::is_my_feedback_key`（归一化 + 本人 + 反馈段）
        ⇒ 别的租户的 key 连第一道都过不了，**一个字节都不会被删**
        （`AGENTS.md` §4 / D-0137 同向；同一批的「跨用户」那一半见
        `tests/test_feedback_api.py::test_cleanup_drops_only_my_own_feedback_objects`）。
        """
        self._seed()
        with tenant_context(self.tenant):
            victim = User.objects.get(username="iso-user-a")
        victim_key = f"{SCHEMA_A}/users/{victim.pk}/feedback.111122223333.png"
        self.storage.put_bytes(victim_key, PNG, content_type="image/png")
        self.assertIsNotNone(self.storage.head(victim_key))     # 自证受害对象真的在

        with tenant_context(self.tenant_b):
            client_b = make_logged_in_client(
                domain=DOMAIN_B, username="iso-reporter-b", groups=("客服",), nodes=(NODE,)
            )
        try:
            resp = client_b.post(
                URL_LIST,
                {"content": "跨租户对象名", "image_keys": [victim_key]},
                content_type="application/json",
                SERVER_NAME=DOMAIN_B,
            )
        finally:
            connection.set_tenant(self.tenant)

        self.assertEqual(resp.status_code, 403, resp.data)
        self.assertEqual(resp.data["code"], 403)
        self.assertEqual(resp.data["msg"], "截图对象名不属于当前用户")
        # ① B 里零新增行（判据打在 B 自己的库上）
        with tenant_context(self.tenant_b):
            self.assertEqual(Feedback.objects.count(), 1)   # 只有 setUp 造的那一行
            self.assertEqual(
                Feedback.objects.filter(content="跨租户对象名").count(), 0
            )
        # ② **A 的对象一个字节都没少**（跨租户的删除原语不存在）
        self.assertIsNotNone(self.storage.head(victim_key))
        # ③ 哨兵：那个 key 确实是 A 的命名空间（⛔ 不是「恰好 B 里也没有」）
        self.assertTrue(victim_key.startswith(f"{SCHEMA_A}/users/"))
