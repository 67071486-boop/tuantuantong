"""录单识别设置：模型口径（T2）+ 两个端点（T3）+ 编辑留痕（T4）。

判据纪律（`AGENTS.md` §5）：

- 留痕断言打在**数据库行**上、期望值写**字面值**（E-0009 / E-0024）；
- 跨租户隔离用**租户内取值独有、且两租户取值不同**的关键词串，**不用主键**
  （两个 schema 的主键序列各自从 1 开始）；
- 权限 fail-closed 夹具**不用** `is_superuser=True`——那会让「无节点必须 403」失去判别力。
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.intake_parser import parse_order_text
from apps.business.intake_rules import RECOGNITION_ITEMS
from apps.business.models import RecognitionKeyword
from apps.business.recognition import rules_for_tenant
from apps.platform.provisioning import provision_tenant
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

SETTINGS_ENDPOINT = "/api/business/recognition-settings/"
PARSE_ENDPOINT = "/api/business/orders/parse/"

#: 租户 A = `TenantTestCase` 的 `test` schema（与既有隔离测试同口径）。
DOMAIN_A = "tenant.test.com"
SCHEMA_B = "rec_api_b"
DOMAIN_B = "rec-api-b.test.local"


def settings_url(item_code):
    return f"{SETTINGS_ENDPOINT}{item_code}/"


def _grant_nodes_and_login(domain, username):
    """在当前 schema 里建本线要的 3 个节点 + 角色，返回**真实登录**的 client。

    `get_or_create`（不是 `create`）：租户 A（`TenantTestCase` 的 `test` schema）只跑迁移、
    **没有目录种子**；而 B 是 `provision_tenant` 建的、`seed_tenant` 已灌过完整 RBAC 目录
    （`benchmark_id=89`/`96` 在 B 里已存在，硬 `create` 会撞唯一约束）。
    第三条 `order.add_edit`（12）只为**核心判据**服务：`POST /orders/parse/` 的准入节点。
    """
    nodes = []
    for benchmark_id, code, name, url in (
        (89, "system.auto_discern_settings", "录单自动识别设置", "/System/AutoDiscernSet"),
        (96, "system.edit_auto_discern_keywords", "编辑自动识别关键词", "/System/EditAutoDiscernSet"),
        (12, "order.add_edit", "添加和编辑订单", "/Order/AddOrder"),
    ):
        node, _ = PermissionNode.objects.get_or_create(
            benchmark_id=benchmark_id,
            defaults={
                "code": code, "name": name, "note": name, "url": url,
                "kind": NodeKind.PAGE, "level": 1,
            },
        )
        # 自证拿到的是**这一条**节点：benchmark_id 撞上别的 code 时，get_or_create 会静默
        # 返回那条错节点，随后请求以 403 失败——症状会指向「权限没给对」而不是「拿错了节点」。
        assert node.code == code, f"benchmark_id={benchmark_id} 不是 {code}：{node.code!r}"
        nodes.append(node)
    client = make_logged_in_client(domain=domain, username=username)
    role_name = "识别设置"  # ⚠️ `Role.name` 的列宽是 5，超长会 DataError。
    role = Role.objects.create(
        name=role_name, group=Group.objects.create(name=role_name)
    )
    role.permissions.set(nodes)
    User.objects.get(username=username).groups.add(role.group)
    return client


class RecognitionKeywordModelTests(TenantTestCase):
    """`RecognitionKeyword` 的字段口径（spec §4）。

    ⚠️ 判据全部钉在**列宽/唯一约束/空串语义**这三个会被人"顺手改掉"的点上。
    """

    def test_keywords_may_be_empty_and_defaults_to_empty_string(self):
        row = RecognitionKeyword.objects.create(item_code="price")
        self.assertEqual(row.keywords, "")
        # 空串是**合法覆盖**（= 清空该项），不是"没填"——所以它必须能存下来、读回来是空串。
        row.refresh_from_db()
        self.assertEqual(row.keywords, "")

    def test_item_code_is_unique(self):
        RecognitionKeyword.objects.create(item_code="price", keywords="价格")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RecognitionKeyword.objects.create(item_code="price", keywords="总价")

    def test_keywords_column_width_is_255(self):
        """列宽是 255：端点按**模型元数据**取这个宽度，两处必须同源（spec §5.2）。"""
        self.assertEqual(
            RecognitionKeyword._meta.get_field("keywords").max_length, 255
        )


class RecognitionSettingApiTests(TenantTestCase):
    """GET 14 行 + PATCH 改一行；权限、校验、留痕与「改词驱动解析」的核心判据。

    夹具**照 `tests/test_operation_log_api.py::LogApiTestCase`**：只建本线要的节点
    （不 `seed_catalog()`——那会在每个用例的 `setUp` 里多跑几百次 upsert），
    再给一个真实登录态挂上它们。
    """

    def setUp(self):
        self.client = _grant_nodes_and_login(DOMAIN_A, "recognition-admin")

    # ── 请求工具 ─────────────────────────────────────────────────────
    def _get(self):
        response = self.client.get(SETTINGS_ENDPOINT)
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body

    def _patch(self, item_code, keywords):
        return self.client.patch(
            settings_url(item_code), {"keywords": keywords}, content_type="application/json"
        )

    def _parse(self, text):
        response = self.client.post(
            PARSE_ENDPOINT, {"content": text}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def _client_without_nodes(self):
        """一个**没挂任何节点**的真实登录态（用来证明两个端点 fail-closed）。"""
        return make_logged_in_client(username="recognition-outsider")

    # ── GET：14 行、顺序、默认关键词逐字 ──────────────────────────────
    def test_list_returns_the_fourteen_items_in_order_with_benchmark_keywords(self):
        body = self._get()
        self.assertEqual(
            [(row["item_code"], row["name"], row["keywords"]) for row in body["data"]["items"]],
            [
                (item.code, item.name, ",".join(item.keywords))
                for item in RECOGNITION_ITEMS
            ],
        )
        self.assertEqual(len(body["data"]["items"]), 14)

    # ── PATCH：保存覆盖值 / 空串 = 清空 ───────────────────────────────
    def test_patch_saves_the_override_and_the_next_get_returns_it(self):
        response = self._patch("price", "总价,价格")
        self.assertEqual(response.json()["code"], 0, response.content[:300])
        rows = {row["item_code"]: row for row in self._get()["data"]["items"]}
        self.assertEqual(rows["price"]["keywords"], "总价,价格")
        self.assertEqual(
            RecognitionKeyword.objects.get(item_code="price").keywords, "总价,价格"
        )

    def test_patch_returns_the_saved_row(self):
        response = self._patch("price", "总价,价格")
        self.assertEqual(
            response.json()["data"],
            {"item_code": "price", "name": "接单价格", "keywords": "总价,价格"},
        )

    def test_patch_to_empty_string_clears_the_item(self):
        self._patch("price", "")
        rows = {row["item_code"]: row for row in self._get()["data"]["items"]}
        self.assertEqual(rows["price"]["keywords"], "")
        self.assertTrue(RecognitionKeyword.objects.filter(item_code="price").exists())

    def test_whitespace_only_keywords_clear_the_item(self):
        """纯空白按空串处理（spec §5.2）：清空是**合法行为**，不是"没填"。"""
        self._patch("price", "   ")
        rows = {row["item_code"]: row for row in self._get()["data"]["items"]}
        self.assertEqual(rows["price"]["keywords"], "")

    # ── 校验：一律业务码 1000（不是 404、不是 500）────────────────────
    def test_unknown_item_code_is_a_business_error_not_a_500(self):
        response = self._patch("not-an-item", "x")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_over_length_keywords_are_rejected_without_500(self):
        response = self._patch("price", "长" * 256)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_non_string_keywords_are_rejected_without_500(self):
        response = self._patch("price", 123)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_non_object_json_bodies_are_a_business_error_not_a_500(self):
        """请求体**不是 JSON 对象**（数组 / 字符串 / 数字）⇒ 200 + `code=1000`。

        原先 `request.data.get("keywords", "")` 在这三种体上抛 `AttributeError`
        ⇒ DRF 的 `exception_handler` 对非 `APIException` 返回 `None` ⇒ **HTTP 500**。
        这违反 spec §5.2「非法输入一律 `code=1000`」（同族见 `AGENTS.md` 的 E-0010）。

        ⚠️ 这条路由**不受** `tests/test_no_route_500.py` 的结构网保护：那张网跳过带转换器的
        路由，而这里是 `<str:item_code>` ⇒ 这条用例是它唯一的网。

        ⚠️ 判据里的体是**实测**过的 DRF 行为（`rest_framework/parsers.py::JSONParser.parse`
        直接 `return json.load(...)`，**不**校验顶层类型 ⇒ 拿到 list / str / int），
        不是为了「形状好看」编的。
        """
        # Django 测试客户端默认把视图异常**抛上来**（不会变成 500 响应）；
        # 关掉它，判据才打在「真实 HTTP 状态码」上，而不是打在 AttributeError 上。
        self.client.raise_request_exception = False
        for raw in ("[1, 2, 3]", '"just-a-string"', "123"):
            with self.subTest(body=raw):
                response = self.client.patch(
                    settings_url("price"), raw, content_type="application/json"
                )
                self.assertEqual(response.status_code, 200, response.content[:300])
                self.assertEqual(response.json()["code"], 1000, response.content[:300])

    def test_object_body_is_still_accepted_so_the_guard_is_not_a_blanket_reject(self):
        """对照组：**对象**体照旧——缺 `keywords` 键 = 空串 = 「清空该行」（既有语义，spec §5.2）。

        没有这条，上一条用例无法区分「按体类型拒绝」与「把 PATCH 一律拒掉」。
        """
        response = self.client.patch(
            settings_url("price"), {}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 0, response.content[:300])
        self.assertEqual(
            list(
                RecognitionKeyword.objects.filter(item_code="price").values_list(
                    "keywords", flat=True
                )
            ),
            [""],
        )

    # ── 权限：fail-closed ────────────────────────────────────────────
    def test_endpoints_require_the_two_nodes(self):
        client = self._client_without_nodes()
        self.assertEqual(client.get(SETTINGS_ENDPOINT).status_code, 403)
        self.assertEqual(
            client.patch(
                settings_url("price"), {"keywords": "x"}, content_type="application/json"
            ).status_code,
            403,
        )

    def test_anonymous_cannot_reach_the_endpoints(self):
        """未登录 ⇒ 401/403（**不是** 200）。"""
        client = Client(SERVER_NAME=DOMAIN_A)
        self.assertIn(client.get(SETTINGS_ENDPOINT).status_code, (401, 403))
        self.assertIn(
            client.patch(
                settings_url("price"), {"keywords": "x"}, content_type="application/json"
            ).status_code,
            (401, 403),
        )

    # ── ⭐ 核心判据：改词之后 `/parse/` 的识别结果跟着变 ────────────────
    def test_changing_a_keyword_changes_what_the_parse_endpoint_recognises(self):
        # 默认词表里 `价格` 能认、`总价` 认不出（spec §3-D3）。
        self.assertIn("total_price", self._parse("价格：100")["fields"])
        self.assertNotIn("total_price", self._parse("总价：100")["fields"])
        # 把「总价」加进「接单价格」那一行 ⇒ 同一段文本就认出来了（两位小数字符串）。
        self._patch("price", "价格,总价")
        self.assertEqual(self._parse("总价：100")["fields"]["total_price"], "100.00")
        self.assertEqual(self._parse("价格：100")["fields"]["total_price"], "100.00")

    def test_clearing_the_price_keywords_makes_the_parse_endpoint_stop_recognising(self):
        """清空 = 该字段不再产出规则 ⇒ `/parse/` 也不认（同一份配置驱动的阴性对照）。

        ⚠️ 清空之后 `total_price` **连 `unmatched` 都不进**：`unmatched` 是「喂进去的规则里
        没命中的那些」，规则本身不产出就没有它。所以判据是 `fields` / `matched` 双阴性。
        """
        self._patch("price", "")
        result = self._parse("价格：100")
        self.assertNotIn("total_price", result["fields"])
        self.assertNotIn("total_price", result["matched"])

    # ── T4：留痕恰好一行；no-op 与被拒请求零行 ───────────────────────
    def test_patch_writes_exactly_one_log_row_and_a_noop_writes_none(self):
        self._patch("price", "价格,总价")
        rows = OperationLog.objects.filter(action="system.recognition_keywords_update")
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.log_type, 40)
        self.assertEqual(row.actor, "recognition-admin")
        self.assertEqual(row.target_kind, "recognition_item")
        self.assertEqual(row.target_id, "price")
        self.assertEqual(row.target_name, "接单价格")
        self.assertEqual(row.to_value, "价格,总价")
        self.assertEqual(row.summary, "【recognition-admin】修改了录单识别项「接单价格」的关键词：价格,总价")
        # 纯 no-op：原样重发 ⇒ **零行**（用 count 差，不要用 assertNotExists）
        before = rows.count()
        self._patch("price", "价格,总价")
        self.assertEqual(rows.count(), before)
        # 被拒的请求（未知 item_code）⇒ 零行
        rejected = self._patch("not-an-item", "x")
        self.assertEqual(rejected.json()["code"], 1000)
        self.assertEqual(rows.count(), before)


class RecognitionSettingIsolationTests(TenantTestCase):
    """A 租户改的词不影响 B 租户（`AGENTS.md` §4 硬规则）。

    ⚠️ 判据用**租户内取值独有、且两租户取值不同**的关键词串（不是主键）：
    两个 schema 的主键序列各自从 1 开始，用主键指认会恒真/恒假。

    ⚠️ 跨域名请求**不会在响应结束时还原 schema**（`tests/test_p3b_isolation.py` 的实测），
    所以 B 侧请求一律走 `_on_b()`，它用 `try/finally` 把连接钉回租户 A。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a = cls.tenant
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="识别设置隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # 第一行：挡住上一条用例把连接留在 B 的 schema 上的残留。
        connection.set_tenant(self.tenant_a)
        self.client_a = _grant_nodes_and_login(DOMAIN_A, "rec-a-admin")
        try:
            with tenant_context(self.tenant_b):
                self.client_b = _grant_nodes_and_login(DOMAIN_B, "rec-b-admin")
        finally:
            connection.set_tenant(self.tenant_a)

    def _get(self, client):
        response = client.get(SETTINGS_ENDPOINT)
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return {row["item_code"]: row["keywords"] for row in body["data"]["items"]}

    def _patch(self, client, item_code, keywords):
        response = client.patch(
            settings_url(item_code), {"keywords": keywords}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)

    def _on_b(self, func, *args):
        try:
            return func(*args)
        finally:
            connection.set_tenant(self.tenant_a)

    def test_keyword_overrides_do_not_leak_across_tenants(self):
        value_a = "仅A租户词甲"
        value_b = "仅B租户词乙"
        self._patch(self.client_a, "price", value_a)
        self._on_b(self._patch, self.client_b, "price", value_b)

        # 阳性对照：A / B 各自都真的写进去了，且拿回的是**自己**的值。
        rows_a = self._get(self.client_a)
        self.assertEqual(rows_a["price"], value_a)
        self.assertNotEqual(rows_a["price"], value_b)
        rows_b = self._on_b(self._get, self.client_b)
        self.assertEqual(rows_b["price"], value_b)
        self.assertNotEqual(rows_b["price"], value_a)

    def test_rules_for_tenant_only_carry_the_current_tenants_keywords(self):
        with tenant_context(self.tenant_a):
            RecognitionKeyword.objects.create(item_code="price", keywords="仅A租户词甲")
        with tenant_context(self.tenant_b):
            RecognitionKeyword.objects.create(item_code="price", keywords="仅B租户词乙")
        try:
            with tenant_context(self.tenant_b):
                rules_b = rules_for_tenant()
                self.assertIn(
                    "total_price",
                    parse_order_text("仅B租户词乙：100", rules=rules_b).fields,
                )
                self.assertNotIn(
                    "total_price",
                    parse_order_text("仅A租户词甲：100", rules=rules_b).fields,
                )
            with tenant_context(self.tenant_a):
                rules_a = rules_for_tenant()
                self.assertIn(
                    "total_price",
                    parse_order_text("仅A租户词甲：100", rules=rules_a).fields,
                )
                self.assertNotIn(
                    "total_price",
                    parse_order_text("仅B租户词乙：100", rules=rules_a).fields,
                )
        finally:
            connection.set_tenant(self.tenant_a)
