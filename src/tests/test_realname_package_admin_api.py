"""实名**资源包配置面**端点（T3，spec §2.4）：`GET/POST /api/accounts/realname-packages/`。

## 判据对应（spec §3-8）

1. **POST 建包**：库里 `total_quota` / `key_secret` 落位、响应是**掩码行**
   （`key_id` 前 4 位 + `***`，键集里**没有** `key_secret`）；
2. **GET 列表任何字段不含 secret 原文**——对照**库里那一行**断言（⛔ 不是常量比常量，
   E-0024），连 key_id 完整原文也不许出现；
3. **重复 key-id ⇒ 既有错误文案**（命令 T2 的那句，service 单一出处）；
4. **命令与端点走同一 service**（结构断言：命令文件不再有 `RealNamePackage.objects.create`
   这样的私有实现，两个调用方都 import `apps.realname.package_admin`）；
5. 节点门：无节点 403（1263 同点）、匿名 401。

## ⚠️ 夹具一律**合成值**（E-0077）

密钥形状「一眼假」（`SYNTH-…`），判据钉的是**库里那一行的值**。本文件跑在
`TenantTestCase`（端点在租户平面）；平台表的 public 落点由 service 里的
`schema_context("public")` 保证（对照判据见命令网那条裸 SQL 用例）。
"""
import ast
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.platform.models import RealNamePackage
from apps.rbac.seed import seed_catalog
from apps.business.roles import TENANT_ADMIN, WORKER
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

TENANT_DOMAIN = "tenant.test.com"
URL = "/api/accounts/realname-packages/"

#: 合成密钥（⛔ 不是任何真实凭据）。
FAKE_KEY_ID = "AKIDFAKE0000T3SYNTH"
FAKE_KEY_SECRET = "SYNTH-T3-0000-FAKE-SECRET-1111-2222"

#: 权限节点（字面量，⛔ 不 import catalog：节点改名时用例要看得见）。
NODE_RECORDS = "system_user.realname_records"

#: 既有错误文案（判据 8-3 的**字面副本**——来自 service / 命令的同一处，E-0024）。
DUPLICATE_MSG_TAIL = "已经录过了"


class RealNamePackageAdminApiTests(TenantTestCase):
    """端点判据（判据 8 的 1–3、5）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_catalog()

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.admin = make_logged_in_client(
            domain=TENANT_DOMAIN,
            username="t3-pkg-admin",
            groups=(TENANT_ADMIN,),
            nodes=(NODE_RECORDS,),
            games=None,
        )
        # ⚠️ 403 变体用 **WORKER** 组（T8 同一条理由）：TENANT_ADMIN 的 Role 自带全节点，
        # 「nodes=()」挡不掉组上来的节点——那不是「无节点」变体。
        self.no_node = make_logged_in_client(
            domain=TENANT_DOMAIN,
            username="t3-pkg-no-node",
            groups=(WORKER,),
            games=None,
        )
        self.anonymous = self.client_class(SERVER_NAME=TENANT_DOMAIN)

    def _post(self, payload):
        return self.admin.post(URL, data=payload, content_type="application/json").json()

    def test_post_creates_the_package_row_and_answers_a_masked_payload(self):
        """判据 8-1：POST 建包 ⇒ 库里落位（含 secret 明文列）、响应是掩码行（无 secret 键）。"""
        body = self._post(
            {
                "key_id": FAKE_KEY_ID,
                "key_secret": FAKE_KEY_SECRET,
                "total_quota": 250,
                "remark": "2026-10 购入备用包（合成备注）",
                "expires_at": "2027-10-10",
            }
        )
        self.assertEqual(body["code"], 0, body)
        row = body["data"]
        # 响应键集**恰好**这 8 个：没有 `key_secret`（⛔ 不是「是空串」——是键不存在）。
        self.assertEqual(
            set(row),
            {"id", "key_id", "total_quota", "used_quota", "expires_at",
             "is_active", "remark", "created_at"},
        )
        self.assertEqual(row["key_id"], f"{FAKE_KEY_ID[:4]}***")
        self.assertEqual(row["total_quota"], 250)
        self.assertEqual(row["used_quota"], 0)
        self.assertIs(row["is_active"], True)

        # 终态对账（E-0009）：库里那一行的值（secret 明文只存库、不出响应）。
        pkg = RealNamePackage.objects.get(key_id=FAKE_KEY_ID)
        self.assertEqual(pkg.key_secret, FAKE_KEY_SECRET)
        self.assertEqual(pkg.total_quota, 250)
        self.assertEqual(pkg.remark, "2026-10 购入备用包（合成备注）")
        self.assertIsNotNone(pkg.expires_at)

    def test_get_returns_masked_rows_and_never_the_secret(self):
        """判据 8-2：GET 的**任何字段**都不含 secret 原文——对照库里那行断言。"""
        self._post(
            {"key_id": FAKE_KEY_ID, "key_secret": FAKE_KEY_SECRET, "total_quota": 100}
        )
        pkg = RealNamePackage.objects.get(key_id=FAKE_KEY_ID)
        self.assertEqual(pkg.key_secret, FAKE_KEY_SECRET)  # 前提自证：确实在库里

        body = self.admin.get(URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertIsInstance(body["data"], list)
        self.assertEqual(len(body["data"]), 1)
        whole = str(body["data"])
        # ⭐ 密钥原文与它的两半都不在响应里；key_id 的完整原文也不许出现（掩码口径同命令）。
        self.assertNotIn(pkg.key_secret, whole)
        self.assertNotIn(pkg.key_secret[:16], whole)
        self.assertNotIn(pkg.key_secret[16:], whole)
        self.assertNotIn(pkg.key_id, whole)
        # 掩码在。
        self.assertIn(f"{FAKE_KEY_ID[:4]}***", whole)

    def test_the_row_moments_are_project_timezone_strings(self):
        """D-0302（2026-10-24）：资源包行的 `expires_at` / `created_at` 是**项目时区**（+08:00）串。

        ⚠️ 收口落在**端点行装配处**（`accounts/views.py::_package_row_local`）：
        `package_admin.package_payload` 与平台 CLI（`_display_local`）共用、按 datetime 处理
        ⇒ 服务层形状不动。夹具把 `expires_at` 钉在 UTC 04:30 ⇒ 响应是北京墙钟 12:30。
        """
        from datetime import datetime as dt
        from datetime import timezone as dt_tz

        fixed = dt(2026, 10, 24, 4, 30, tzinfo=dt_tz.utc)
        RealNamePackage.objects.create(
            key_id=FAKE_KEY_ID,
            key_secret=FAKE_KEY_SECRET,
            total_quota=10,
            expires_at=fixed,
        )
        row = self.admin.get(URL).json()["data"][0]
        self.assertEqual(row["expires_at"], "2026-10-24T12:30:00+08:00")
        self.assertTrue(row["created_at"].endswith("+08:00"), row["created_at"])

    def test_duplicate_key_id_answers_the_command_text_and_creates_nothing(self):
        """判据 8-3：重复 key-id ⇒ 命令的既有文案（service 单一出处的证据），且不建第二行。"""
        first = self._post(
            {"key_id": FAKE_KEY_ID, "key_secret": FAKE_KEY_SECRET, "total_quota": 100}
        )
        self.assertEqual(first["code"], 0, first)
        second = self._post(
            {
                "key_id": FAKE_KEY_ID,
                "key_secret": "SYNTH-9999-REPLACEMENT",
                "total_quota": 10,
            }
        )
        self.assertEqual(second["code"], 1000, second)
        self.assertIn(DUPLICATE_MSG_TAIL, second["msg"])
        # 终态：仍只有一行，原 secret 没被覆盖。
        self.assertEqual(RealNamePackage.objects.count(), 1)
        self.assertEqual(
            RealNamePackage.objects.get(key_id=FAKE_KEY_ID).key_secret, FAKE_KEY_SECRET
        )

    def test_service_validation_texts_surface_as_1000(self):
        """domain 判据（total < 1 / 空白密钥 / 空串有效期）⇒ 1000 + service 的文案，零副作用。"""
        for payload in (
            {"key_id": FAKE_KEY_ID, "key_secret": FAKE_KEY_SECRET, "total_quota": 0},
            {"key_id": FAKE_KEY_ID, "key_secret": "   ", "total_quota": 5},
            {"key_id": FAKE_KEY_ID, "key_secret": FAKE_KEY_SECRET, "total_quota": 5,
             "expires_at": "   "},
            {"key_id": "", "key_secret": FAKE_KEY_SECRET, "total_quota": 5},
        ):
            with self.subTest(payload=payload):
                body = self._post(payload)
                self.assertEqual(body["code"], 1000, body)
        self.assertEqual(RealNamePackage.objects.count(), 0)

    def test_the_endpoint_is_gated_by_the_node_and_login(self):
        """判据 8-5：无节点 403（1263 同点）、匿名 401；被拒路径一行不写。"""
        payload = {"key_id": "x", "key_secret": "y", "total_quota": 1}
        resp = self.no_node.post(URL, data=payload, content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        resp = self.no_node.get(URL)
        self.assertEqual(resp.status_code, 403)
        resp = self.anonymous.get(URL)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(RealNamePackage.objects.count(), 0)


class RealNamePackageServiceWiringTests(TenantTestCase):
    """判据 8-4 的**结构断言**：命令与端点走同一 service，⛔ 命令不再私有实现。"""

    def _source(self, relpath: str) -> str:
        base = Path(__file__).resolve().parents[1]
        return (base / relpath).read_text()

    def test_command_delegates_add_and_list_to_the_service(self):
        """命令文件的 `add` / `list` 不再有自己的创建 / 唯一性实现，import service。"""
        src = self._source("apps/platform/management/commands/realname_package.py")
        self.assertIn("from apps.realname.package_admin import", src)
        self.assertIn("add_package(", src)
        self.assertIn("list_packages(", src)
        # 私有实现已撤：创建与「已录过」的唯一性检查不在命令文件里。
        tree = ast.parse(src)
        calls = [
            ast.unparse(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        ]
        self.assertNotIn("RealNamePackage.objects.create", calls)
        self.assertNotIn("RealNamePackage.objects.filter", calls)

    def test_the_endpoint_calls_the_same_service(self):
        """端点的 `realname_packages` 动作 import 且调用同一个 `add_package`。"""
        src = self._source("apps/accounts/views.py")
        self.assertIn("add_package", src)
        self.assertIn("list_packages", src)
        self.assertIn("from apps.realname.package_admin import", src)
        # service 文件确实是「唯一出处」：创建与唯一性检查住在那里。
        svc = self._source("apps/realname/package_admin.py")
        self.assertIn("RealNamePackage.objects.create(", svc)
        self.assertIn("RealNamePackage.objects.filter(key_id=key_id).exists()", svc)
