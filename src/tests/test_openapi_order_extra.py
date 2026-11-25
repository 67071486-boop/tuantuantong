"""T4：开放平台 v2 **只读扩展端点**的判据（设计文档 `2026-10-08-open-platform-v2-parity-design.md`
§6.1 / §6.4 / §13.10）。

## 本文件证什么（八条，每条都能独立地红）

1. **形状**：六个端点的 `data`（与列表行的键集）**恰好**等于本文件里**写死的字面量集合**
   （⛔ 不从序列化器 / 常量模块 import —— `AGENTS.md` §5-E「钉值不钉常量」：import 常量的话
   「改实现 + 改常量」一起改就不会红）；`ranks/` 那条断言**码的数值**（`1024`，⛔ 不是"非 0"）。
2. **只认令牌**：**会话请求**打这六个端点 ⇒ **401**（对照：同一会话打 `/api/auth/me` ⇒ 200）。
   ⚠️ 其中两条带 `<order_no:order_no>` 的路由**逃出** `test_no_route_500` 的通用腿
   （那张网自己登记的盲区：跳过带转换器的路由）⇒ 它们的 401 判据**只能在这里**。
3. **跨租户**：A 的令牌打 **B 的域名** ⇒ 401；A 的应用**查不到 B 的单**（`1002`，与"不存在"同形）；
   `projects/` 给**别的租户的** `game_id` ⇒ 拿不到东西。
   ⚠️ 判据用**租户内唯一、且两租户取值不同**的业务值（订单号 / 项目名），⛔ **不用主键**
   （两个 schema 的主键序列各自从 1 开始 —— `AGENTS.md` §5-G 的既有先例）。
4. ⭐ **`submitted_at` 恒为 `null`**，且**同一次响应里** `created_at` / `claimed_at` /
   `completed_at` **真的有值** —— 没有后半句，「恒 null」可能是"整条都读不到"的**假绿**。
5. **畸形入参不 500**（超长 `order_no` / NUL 路径段 / 非法日期 / 负数 `page`），
   且**逐条写清期望**（⛔ 不是"随便回什么都行"）。
6. **分页边界**：`page_size` 超上限 ⇒ **夹到上限**（判据打在**返回里的 `page_size`** 上，
   ⛔ 不是"没 500"），并配一条"没超上限时它照原样回"的对照。
7. ⭐ **账单的数据范围对照**（总指挥裁定抽出筛法时点名要求的那条）：同一组筛选参数下，
   **超管会话**走内部 `/api/business/settlements` 与 **应用令牌**走新端点 `/api/open/v1/bills/`
   返回的**订单号集合逐字相同** —— 既证明对外端点**不是恒空**，也证明两条路的筛法口径一致。
   ⚠️ 并配一条反向对照：**少了游戏权限的非管理员会话**在内部端点上**看不到**那些行
   （证明 `scope_queryset` 卡点**没有因为这次抽取而消失**）。
8. **`ranks/` 恒回 `1024` + HTTP 200**，且它一个字节都不写（不在本文件断言，见
   `test_operation_log_coverage` 的只读反向钉子）。

## 夹具纪律

- 凭据 / 订单一律**合成值**（`AGENTS.md` §5-C），⛔ 与任何真实租户的数据无关。
- 跨域名请求结束后连接**停在那个租户的 schema 上**（`TenantMainMiddleware` 在请求进来时 set、
  响应后**不还原**）⇒ 每个发请求的辅助函数都用 `try/finally` 把连接**钉回租户 A**，
  `setUp` 第一行也显式 `set_tenant`（那一行挡用例之间的残留，`finally` 挡用例内部的）。
- `tokens.issue()` **当场签发**（用它自己的密钥）⇒ 本文件不复制任何签名 / 编码逻辑
  （⛔ 不把实现抄一份到用例里：那会变成"用被测者证明被测者"）。
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderScreenshot,
    OrderStatus,
    Performance,
    Project,
    ProjectCategory,
    ReviewStatus,
    SettleStatus,
)
from apps.openapi import tokens
from apps.openapi.authentication import BEARER_INVALID_CREDENTIALS_MSG
from apps.openapi.models import ApiApp
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

# ── 路径（与 `apps/openapi/urls.py` 的 pattern 逐字一致；⚠️ 都带尾斜杠）──────────────
STATUS_TEMPLATE = "/api/open/v1/orders/{order_no}/status/"
IMAGES_TEMPLATE = "/api/open/v1/orders/{order_no}/images/"
RANKS_PATH = "/api/open/v1/ranks/"
BILLS_PATH = "/api/open/v1/bills/"
GAMES_PATH = "/api/open/v1/games/"
PROJECTS_PATH = "/api/open/v1/projects/"
#: 内部结算列表（**对照用**；⚠️ 不带尾斜杠，与 `apps/business/urls.py` 一致）。
SETTLEMENTS_PATH = "/api/business/settlements"
#: 会话认证的对照端点（本文件用它证明"会话本身是好的"）。
SESSION_CONTROL_PATH = "/api/auth/me"

#: 业务码 / 状态码的**字面值**（`AGENTS.md` §5-E：写数字，让"码值被改掉"在用例里可见）。
OK = 0
VALIDATION = 1000
BUSINESS = 1002
RANKS_NOT_AVAILABLE = 1024
UNAUTHORIZED = 401
NOT_FOUND_STATUS = 404
#: 分页契约（§6.4）：默认 20、上限 100。
CONTRACT_DEFAULT_PAGE_SIZE = 20
CONTRACT_MAX_PAGE_SIZE = 100

#: `ranks/` 那句（**对外契约的一部分**；写死字面量，⛔ 不从实现 import —— 见模块 docstring 第 1 条）。
RANKS_MSG = "接单排行榜尚未开放"

#: ⭐ 载荷键集：**写死的字面量**（模块 docstring 第 1 条）。
ENVELOPE_KEYS = {"code", "msg", "data"}
LIST_PAYLOAD_KEYS = {"count", "results", "page", "page_size"}
STATUS_KEYS = {
    "status",
    "status_text",
    "created_at",
    "claimed_at",
    "submitted_at",
    "completed_at",
}
IMAGE_ROW_KEYS = {
    "img_type",
    "url",
    "original_name",
    "size_bytes",
    "remark",
    # ⭐ 上传人是**显示名**（不是账号主键）：键名与取值口径的改准理由见
    # `tests/test_openapi_order_extra.py::ShapeTests::test_the_uploader_is_a_display_name_not_a_primary_key`。
    "uploaded_by_name",
    "created_at",
}
BILL_ROW_KEYS = {
    "order_no",
    "emp_type",
    "temp_amount",
    "bonus_amount",
    "taker_price",
    "payable_net",
    "settle_status",
    "settled_amount",
    "settled_at",
}
GAME_ROW_KEYS = {"id", "name", "is_active", "sort_order"}
PROJECT_ROW_KEYS = {"id", "name", "base_price", "category", "sort_order"}

# ── 合成凭据（⛔ 不是任何真实租户的凭据）──────────────────────────────────────────
APP_KEY = "probeextrakey0000001"
APP_SECRET = "probe-extra-secret-00000000000001"
#: 租户 B **自己**那把（与 A 不同 ⇒ 才能证明"B 侧这条路是通的"）。
APP_KEY_B = "probeextrakey0000002"
APP_SECRET_B = "probe-extra-secret-00000000000002"

#: 租户 B 的域名与 schema（刻意与既有两个跨租户用例不同 ⇒ 同一次运行里不会撞 `Domain` 唯一约束）。
DOMAIN_B = "openapi-extra-iso-b.test.local"
SCHEMA_B = "openapi_extra_iso_b"

#: 两侧的订单号取值**不同**（同名会让负断言失去判别力）。
ORDER_NO_A = "20261008000000000101"
ORDER_NO_B = "20261008000000000102"
#: 另一张属于 A 的单：专门用来造"时间点齐全"与"分页要第二页"的场景。
ORDER_NO_A2 = "20261008000000000103"
#: 两侧的项目名**不同**（跨租户判据用业务值，⛔ 不用主键）。
PROJECT_NAME_A = "测试项目甲"
PROJECT_NAME_B = "测试项目乙"
#: 租户 B 的游戏**显式给一个 A 不可能有的 pk**（序列从 1 开始 ⇒ A 里没有这个号）
#: ⇒ A 拿它问 `projects/` 必然落进"本租户不存在"那一档（读数确定，不靠巧合）。
GAME_B_PK = 90000042
#: 两侧那张单也显式给不同的 pk（理由同 `GAME_B_PK`；本文件不用 pk 当判据）。
A_ORDER_PK = 91000001
B_ORDER_PK = 91000002

#: 来源 IP（文档保留段 203.0.113.0/24，⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.41"


class _ExtraFixture(TenantTestCase):
    """租户 A（`TenantTestCase` 的 schema）+ 租户 B（`SCHEMA_B`）两侧的最小数据。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 里建：schema 的 DDL **不能**回滚在 `TestCase` 的事务里。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="开放平台扩展租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行把连接钉回租户 A（上一条用例的跨域名请求会把连接留在那个租户的 schema 上）。
        connection.set_tenant(self.tenant)
        super().setUp()
        self.domain_a = self.domain.domain

        self.game_a = Game.objects.create(name="测试游戏甲")
        self.category_a = ProjectCategory.objects.create(
            game=self.game_a, name="测试分类甲"
        )
        self.project_a = Project.objects.create(
            category=self.category_a,
            name=PROJECT_NAME_A,
            base_price=Decimal("12.30"),
            sort_order=7,
        )
        #: 三个时间点的**基准值**：`claimed_at` 与 `completed_at` 都用它（`created_at` 是自动的）。
        self.moment = timezone.now()
        self.order_a = Order.objects.create(
            pk=A_ORDER_PK,
            game=self.game_a,
            order_no=ORDER_NO_A,
            source_order_no="TT-EXTRA-A-0001",
            status=OrderStatus.COMPLETED,
            project_text="测试占位项目",
            completed_at=self.moment,
        )
        self.taker = User.objects.create_user(username="测试打手甲")
        #: 打单工资行：**接单时刻**在它上面（`Order` 上没有这一列）。
        Performance.objects.create(
            order=self.order_a,
            emp_type=EmpType.MAIN,
            user=self.taker,
            claimed_at=self.moment,
            temp_amount=Decimal("20.00"),
            bonus_amount=Decimal("20.00"),
            review_status=ReviewStatus.APPROVED,
            settle_status=SettleStatus.UNSETTLED,
        )
        #: ⭐ **判别力**：客服行的 `claimed_at` **故意**写成另一个值 ——
        #: 接单时刻必须取自**打单工资行**，取错行时这条用例才会红（模型注释写明客服行不写它）。
        self.sales = User.objects.create_user(username="测试客服甲")
        Performance.objects.create(
            order=self.order_a,
            emp_type=EmpType.SALES,
            user=self.sales,
            claimed_at=self.moment - timedelta(days=7),
            temp_amount=Decimal("5.00"),
            commission_rate=Decimal("10.00"),
        )
        #: 第二张单（分页要第二页、`count` 与 `results` 才会分叉）。
        self.order_a2 = Order.objects.create(
            game=self.game_a,
            order_no=ORDER_NO_A2,
            project_text="测试占位项目二",
        )
        # ⭐ 上传人**故意**让昵称（`first_name`）与登录账号**不同**：这样"给的是显示名"
        # 与"给的是账号名"才是两件事 —— 显示名的口径是 `first_name or get_username()`
        # （`apps/accounts/display.py` 的共用出处），只看账号名会漏掉"昵称优先"那一半。
        self.uploader = User.objects.create_user(
            username="测试上传账号甲", first_name="测试上传昵称甲"
        )
        self.shot_ready = OrderScreenshot.objects.create(
            order=self.order_a,
            img_type=OrderScreenshot.IMG_ORDER,
            object_key=f"openapi-extra-a/{ORDER_NO_A}/001.jpg",
            original_name="进度图甲.jpg",
            size_bytes=1234,
            remark="测试备注甲",
            uploaded_by=self.uploader,
            status=OrderScreenshot.STATUS_READY,
        )
        #: ⭐ **判别力**：还没认领（`pending`）的那张**不许**出现在列表里。
        self.shot_pending = OrderScreenshot.objects.create(
            order=self.order_a,
            img_type=OrderScreenshot.IMG_ORDER,
            object_key=f"openapi-extra-a/{ORDER_NO_A}/002.jpg",
            original_name="未认领图甲.jpg",
            size_bytes=99,
            uploaded_by=self.uploader,
            status=OrderScreenshot.STATUS_PENDING,
        )
        self.app_a = self._create_app(
            name="扩展探针应用甲", app_key=APP_KEY, app_secret=APP_SECRET
        )

        try:
            with tenant_context(self.tenant_b):
                self.game_b = Game.objects.create(pk=GAME_B_PK, name="测试游戏乙")
                self.category_b = ProjectCategory.objects.create(
                    game=self.game_b, name="测试分类乙"
                )
                self.project_b = Project.objects.create(
                    category=self.category_b, name=PROJECT_NAME_B
                )
                self.order_b = Order.objects.create(
                    pk=B_ORDER_PK,
                    game=self.game_b,
                    order_no=ORDER_NO_B,
                    project_text="测试占位项目乙",
                )
                self.app_b = self._create_app(
                    name="扩展探针应用乙", app_key=APP_KEY_B, app_secret=APP_SECRET_B
                )
        finally:
            connection.set_tenant(self.tenant)

    # ── 造物 ───────────────────────────────────────────────────────────
    @staticmethod
    def _create_app(*, name, app_key, app_secret):
        """在**当前 schema** 里建一个应用。

        ⚠️ 本批的六个端点**不装权限类、也不做游戏范围过滤**（v2 §13.10：应用凭据 = 本租户全部）
        ⇒ 这里**不挂节点、不勾游戏**：那两个维度今天**不是**它们的准入判据
        （挂上去只会让用例看起来在测一件不存在的事）。
        """
        creator, _ = User.objects.get_or_create(
            username=f"openapi-extra-{app_key}", defaults={"password": "unused-in-this-test"}
        )
        return ApiApp.objects.create(
            name=name, app_key=app_key, app_secret=app_secret, created_by=creator
        )

    # ── 发请求 ─────────────────────────────────────────────────────────
    def _bearer_get(
        self, path, data=None, *, app=None, domain=None, token=None, ip=SOURCE_IP
    ):
        """按 `domain` 发一条**带 Bearer 令牌**的 GET；结束后把连接钉回租户 A。"""
        app = app if app is not None else self.app_a
        domain = self.domain_a if domain is None else domain
        token = tokens.issue(app.app_key, app.app_secret) if token is None else token
        try:
            return Client(SERVER_NAME=domain).get(
                path,
                data or {},
                HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR=ip,
            )
        finally:
            connection.set_tenant(self.tenant)

    def _session_get(self, path, *, client, data=None):
        """用一条**已登录的会话**发 GET（同样把连接钉回租户 A）。"""
        try:
            return client.get(path, data or {})
        finally:
            connection.set_tenant(self.tenant)

    # ── 断言小工具 ─────────────────────────────────────────────────────
    def assert_ok(self, response):
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], OK, body)
        self.assertEqual(set(body), ENVELOPE_KEYS, body)
        return body["data"]

    def assert_business_failure(self, response, code):
        """业务失败：**HTTP 200** + **指定的**业务码（⛔ 不只看 msg：那会让"把 1000 写成 1002"活下来）。"""
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], code, body)
        self.assertEqual(set(body), ENVELOPE_KEYS, body)
        return body

    @staticmethod
    def _game_ids_by_name(response):
        return {row["name"]: row["id"] for row in json.loads(response.content)["data"]["results"]}

    @staticmethod
    def _order_nos(payload):
        """列表载荷里的订单号集合（用 `set`：两条路的**排序**不构成本文件的判据）。"""
        return {row["order_no"] for row in payload}


class ShapeTests(_ExtraFixture):
    """① 六个端点的形状：信封 + `data` 键集**恰好**等于写死的字面量集合。"""

    def test_status_payload_has_exactly_the_six_keys(self):
        data = self.assert_ok(
            self._bearer_get(STATUS_TEMPLATE.format(order_no=ORDER_NO_A))
        )
        self.assertEqual(set(data), STATUS_KEYS, data)
        # ⚠️ 值是**字面量**（⛔ 不调 `order.get_status_display()` 反推：那会用被测者证明被测者）。
        self.assertEqual(data["status"], "COMPLETED")
        self.assertEqual(data["status_text"], "已完成")

    def test_images_payload_and_row_have_exactly_the_expected_keys(self):
        data = self.assert_ok(
            self._bearer_get(IMAGES_TEMPLATE.format(order_no=ORDER_NO_A))
        )
        self.assertEqual(set(data), LIST_PAYLOAD_KEYS, data)
        self.assertEqual(len(data["results"]), 1, data)
        row = data["results"][0]
        self.assertEqual(set(row), IMAGE_ROW_KEYS, row)
        # ⛔ **不给内部主键 `id`**（对外载荷不给内部主键）：键集断言已经涵盖，这里点明意图。
        self.assertNotIn("id", row)
        self.assertEqual(row["img_type"], 0)
        self.assertEqual(row["original_name"], "进度图甲.jpg")
        self.assertEqual(row["size_bytes"], 1234)
        self.assertEqual(row["remark"], "测试备注甲")
        # ⭐ 上传人是**显示名**（值本身与"不是主键"由下面那条用例单独钉）。
        self.assertEqual(row["uploaded_by_name"], "测试上传昵称甲")
        self.assertEqual(row["created_at"], self.shot_ready.created_at.isoformat())
        # `url` 是短时效直链（本机替身是 `/api/media/blob/<对象名>`）。
        self.assertEqual(row["url"], f"/api/media/blob/{self.shot_ready.object_key}")

    def test_the_uploader_is_a_display_name_not_a_primary_key(self):
        """⭐ 改准（T4 执行期，2026-10-08）：对外给的是**显示名**，⛔ **不是**账号主键。

        理由（两条，缺一条都不足以改）：① 对外载荷出现**内部主键**是一条没必要的暴露面
        （同模块"不给 `id`"那条纪律）；② 它与本仓 `order_payload` 的 **C 组裁定**
        （「员工的**身份**不给」）**直接冲突** —— 内部列表给主键是给自己人看的，对外那层不该照抄。

        ⚠️ 判据三条一起才成立：**非空字符串** + **不等于**那一行的 `uploaded_by_id`（主键）
        + 等于仓内共用显示名口径的值（`first_name` 优先、回落 `get_username()`）。
        只断"有值"会让"把主键转成字符串"也活下来。
        """
        data = self.assert_ok(
            self._bearer_get(IMAGES_TEMPLATE.format(order_no=ORDER_NO_A))
        )
        row = data["results"][0]
        name = row["uploaded_by_name"]
        self.assertIsInstance(name, str, row)
        self.assertTrue(name, row)
        # ① 不是主键（也不是它的字符串形状）。
        self.assertNotEqual(name, self.uploader.pk, row)
        self.assertNotEqual(name, str(self.uploader.pk), row)
        # ② 是**显示名**：昵称优先（这个账号的昵称与登录账号**刻意不同**）。
        self.assertEqual(name, "测试上传昵称甲", row)
        self.assertNotEqual(name, self.uploader.get_username(), row)
        # ③ 那一行的主键仍然长得是主键（自证"两个值确实不同"，⛔ 不是拿一个空值蒙过去）。
        self.assertEqual(
            OrderScreenshot.objects.get(pk=self.shot_ready.pk).uploaded_by_id,
            self.uploader.pk,
        )

    def test_games_payload_and_row_have_exactly_the_expected_keys(self):
        data = self.assert_ok(self._bearer_get(GAMES_PATH))
        self.assertEqual(set(data), LIST_PAYLOAD_KEYS, data)
        self.assertTrue(data["results"], data)
        for row in data["results"]:
            self.assertEqual(set(row), GAME_ROW_KEYS, row)
        self.assertEqual(
            {row["name"] for row in data["results"]}, {"测试游戏甲"}, data["results"]
        )
        row = data["results"][0]
        self.assertEqual(row["id"], self.game_a.pk)
        self.assertEqual(row["is_active"], True)
        self.assertEqual(row["sort_order"], 0)

    def test_projects_payload_and_row_have_exactly_the_expected_keys(self):
        data = self.assert_ok(
            self._bearer_get(PROJECTS_PATH, {"game_id": self.game_a.pk})
        )
        self.assertEqual(set(data), LIST_PAYLOAD_KEYS, data)
        self.assertEqual(len(data["results"]), 1, data)
        row = data["results"][0]
        self.assertEqual(set(row), PROJECT_ROW_KEYS, row)
        self.assertEqual(row["id"], self.project_a.pk)
        self.assertEqual(row["name"], PROJECT_NAME_A)
        # 金额走既有出口：**字符串两位小数**（⛔ 不是 float）。
        self.assertEqual(row["base_price"], "12.30")
        self.assertEqual(row["category"], "测试分类甲")
        self.assertEqual(row["sort_order"], 7)

    def test_bills_payload_and_row_have_exactly_the_expected_keys(self):
        data = self.assert_ok(self._bearer_get(BILLS_PATH))
        self.assertEqual(set(data), LIST_PAYLOAD_KEYS, data)
        self.assertEqual(len(data["results"]), 2, data)
        for row in data["results"]:
            self.assertEqual(set(row), BILL_ROW_KEYS, row)
            # ⛔ 不给员工身份、不给内部主键（判据是键集，这里点明意图）。
            for forbidden in ("id", "user", "username", "display_name", "settled_by"):
                self.assertNotIn(forbidden, row)
        main_row = next(row for row in data["results"] if row["emp_type"] == "打单工资")
        self.assertEqual(main_row["order_no"], ORDER_NO_A)
        self.assertEqual(main_row["temp_amount"], "20.00")
        self.assertEqual(main_row["bonus_amount"], "20.00")
        self.assertEqual(main_row["payable_net"], "20.00")
        self.assertEqual(main_row["settle_status"], "未结账")
        self.assertIsNone(main_row["settled_amount"])
        self.assertIsNone(main_row["settled_at"])
        self.assertIsNone(main_row["taker_price"])

    def test_ranks_returns_the_not_available_code_and_a_null_data(self):
        """⭐ 断言**码的数值**（`1024`），⛔ 不是"非 0"：后者对"随便回个 1002"也恒绿。"""
        response = self._bearer_get(RANKS_PATH)
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(set(body), ENVELOPE_KEYS, body)
        self.assertEqual(body["code"], RANKS_NOT_AVAILABLE, body)
        self.assertEqual(body["msg"], RANKS_MSG, body)
        self.assertIsNone(body["data"], body)


class TokenOnlyTests(_ExtraFixture):
    """② 只认令牌：会话请求打这六个端点 ⇒ 401（对照：同一会话打 `/api/auth/me` ⇒ 200）。"""

    def _paths(self):
        return (
            STATUS_TEMPLATE.format(order_no=ORDER_NO_A),
            IMAGES_TEMPLATE.format(order_no=ORDER_NO_A),
            RANKS_PATH,
            BILLS_PATH,
            GAMES_PATH,
            f"{PROJECTS_PATH}?game_id={self.game_a.pk}",
        )

    def test_a_logged_in_session_is_rejected_on_every_new_endpoint(self):
        """⭐ 六条路径**逐条**断言（⛔ 不抽查一条就代表全部）。

        ⚠️ 其中两条带 `<order_no:order_no>` 的路由不在 `test_no_route_500` 的通用腿里
        （那张网跳过带转换器的路由）⇒ 它们"会话 ⇒ 401"这条判据**只有本用例在守**。
        """
        client = make_logged_in_client(domain=self.domain_a)
        for path in self._paths():
            with self.subTest(path=path):
                response = self._session_get(path, client=client)
                self.assertEqual(response.status_code, UNAUTHORIZED, response.content[:300])
                body = json.loads(response.content)
                self.assertEqual(body["code"], UNAUTHORIZED, body)
                # ⛔ 不是"没带令牌 ⇒ 500"、也不是"被当成匿名放行"。
                self.assertEqual(body["msg"], BEARER_INVALID_CREDENTIALS_MSG, body)

    def test_the_session_itself_works(self):
        """对照：**同一个会话**打 `/api/auth/me` ⇒ 200 —— 否则上面那条 401 可能由"会话坏了"凑出来。"""
        client = make_logged_in_client(domain=self.domain_a)
        response = self._session_get(SESSION_CONTROL_PATH, client=client)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(json.loads(response.content)["code"], OK, response.content[:300])


class CrossTenantTests(_ExtraFixture):
    """③ 跨租户：A 的令牌打 B 的域名 ⇒ 401；A 的应用查不到 B 的数据。"""

    def test_a_credentials_on_the_domain_of_tenant_b_return_401(self):
        response = self._bearer_get(
            STATUS_TEMPLATE.format(order_no=ORDER_NO_B), domain=DOMAIN_B
        )
        self.assertEqual(response.status_code, UNAUTHORIZED, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], UNAUTHORIZED, body)
        # B 里没有这个 appKey ⇒ 与"令牌不对"**同一句**（§5.4：不给应用枚举面）。
        self.assertEqual(body["msg"], BEARER_INVALID_CREDENTIALS_MSG, body)

    def test_b_credentials_on_its_own_domain_are_accepted(self):
        """正向对照：B **自己**的令牌 + B 的域名 + B 的单 ⇒ 200。

        没有它，上面那条 401 可能由"对外这条路整个坏了"凑出来。
        """
        data = self.assert_ok(
            self._bearer_get(
                STATUS_TEMPLATE.format(order_no=ORDER_NO_B),
                app=self.app_b,
                domain=DOMAIN_B,
            )
        )
        self.assertEqual(set(data), STATUS_KEYS, data)

    def test_a_app_cannot_read_the_status_of_tenant_b_order(self):
        """A 的应用查 B 的订单号 ⇒ 与"不存在"**同形**（`1002`）。

        ⚠️ 判据用**订单号**（租户内唯一、两侧取值不同），⛔ **不用主键** ——
        两个 schema 的主键序列各自从 1 开始，"按 B 的 pk 查不到"在 A 里可能命中 A 自己的行。
        """
        body = self.assert_business_failure(
            self._bearer_get(STATUS_TEMPLATE.format(order_no=ORDER_NO_B)), BUSINESS
        )
        self.assertIsNone(body["data"], body)
        # 对照：A **自己**那张单查得到（否则上面的"查不到"可能是"这条路整个坏了"）。
        self.assertEqual(
            self.assert_ok(
                self._bearer_get(STATUS_TEMPLATE.format(order_no=ORDER_NO_A))
            )["status"],
            "COMPLETED",
        )

    def test_a_app_cannot_read_the_images_of_tenant_b_order(self):
        self.assert_business_failure(
            self._bearer_get(IMAGES_TEMPLATE.format(order_no=ORDER_NO_B)), BUSINESS
        )

    def test_a_projects_query_with_tenant_bs_game_id_returns_nothing(self):
        """`?game_id=` 是 **B 的游戏**（显式一个 A 不可能有的 pk）⇒ `1000`（本租户不存在）。

        ⚠️ 这条同时钉住两件事：① 游戏 id 的判据落在**当前连接所属 schema**（`AGENTS.md` §4：
        租户上下文只来自连接、⛔ 不接受请求参数里的租户标识）；
        ② **拿不到 B 的项目名**（用业务值当判据，⛔ 不用主键）。
        """
        body = self.assert_business_failure(
            self._bearer_get(PROJECTS_PATH, {"game_id": GAME_B_PK}), VALIDATION
        )
        self.assertIsNone(body["data"], body)
        self.assertNotIn(PROJECT_NAME_B, json.dumps(body, ensure_ascii=False))
        # 对照：B **自己**用这个 id 查得到它自己的项目（证明"B 那边确实有这个游戏与项目"）。
        b_data = self.assert_ok(
            self._bearer_get(PROJECTS_PATH, {"game_id": GAME_B_PK}, app=self.app_b, domain=DOMAIN_B)
        )
        self.assertEqual(
            [row["name"] for row in b_data["results"]], [PROJECT_NAME_B], b_data
        )


class StatusTimelineTests(_ExtraFixture):
    """⭐ 四个时间点：`submitted_at` 恒 `null`，另外三个**真的有值**（防"整条读不到"的假绿）。"""

    def test_the_three_existing_timestamps_really_have_values(self):
        data = self.assert_ok(
            self._bearer_get(STATUS_TEMPLATE.format(order_no=ORDER_NO_A))
        )
        self.assertEqual(data["created_at"], self.order_a.created_at.isoformat())
        self.assertEqual(data["claimed_at"], self.moment.isoformat())
        self.assertEqual(data["completed_at"], self.moment.isoformat())
        # 三个键**都不是** `None` —— 这才让下面那条"恒 null"有意义。
        for key in ("created_at", "claimed_at", "completed_at"):
            self.assertIsNotNone(data[key], data)

    def test_submitted_at_is_always_null(self):
        """⛔ 本系统没有独立的"交单时间"这一列（§13.8-3 / §13.10-2）⇒ 这个键恒 `null`。

        ⚠️ 用**两张单**各来一次（一张已完成、一张未分配）：只测一张时，"恒 null"可能只是
        "那张单恰好没有这个时间"，而不是契约值。
        """
        for order_no in (ORDER_NO_A, ORDER_NO_A2):
            with self.subTest(order_no=order_no):
                data = self.assert_ok(
                    self._bearer_get(STATUS_TEMPLATE.format(order_no=order_no))
                )
                self.assertIn("submitted_at", data)
                self.assertIsNone(data["submitted_at"], data)

    def test_claimed_at_comes_from_the_main_performance_row(self):
        """⭐ 判别力：客服行的 `claimed_at` **是另一个值** ⇒ 取错行时这条会红。"""
        data = self.assert_ok(
            self._bearer_get(STATUS_TEMPLATE.format(order_no=ORDER_NO_A))
        )
        self.assertEqual(data["claimed_at"], self.moment.isoformat(), data)
        self.assertNotEqual(
            data["claimed_at"], (self.moment - timedelta(days=7)).isoformat(), data
        )

    def test_an_order_without_any_performance_row_has_a_null_claimed_at(self):
        """没有打单工资行（B-1 之后是**合法**形态）⇒ `claimed_at` 为 `null`，⛔ 不是 500。"""
        data = self.assert_ok(
            self._bearer_get(STATUS_TEMPLATE.format(order_no=ORDER_NO_A2))
        )
        self.assertIsNone(data["claimed_at"], data)
        self.assertIsNone(data["completed_at"], data)
        # 而 `created_at` 永远有值（录单时刻是列上的 `auto_now_add`）。
        self.assertIsNotNone(data["created_at"], data)

    def test_a_soft_deleted_order_is_not_readable(self):
        """已软删的单在对外读面上**不存在**（默认 manager；与既有查单端点同口径）。"""
        Order.objects.filter(pk=A_ORDER_PK).update(deleted_at=timezone.now())
        self.assert_business_failure(
            self._bearer_get(STATUS_TEMPLATE.format(order_no=ORDER_NO_A)), BUSINESS
        )
        self.assert_business_failure(
            self._bearer_get(IMAGES_TEMPLATE.format(order_no=ORDER_NO_A)), BUSINESS
        )


class PaginationTests(_ExtraFixture):
    """④ 分页边界：`page_size` 超上限 ⇒ **夹到上限**（判据打在**返回里的 `page_size`** 上）。"""

    def test_page_size_above_the_cap_is_clamped_to_the_cap(self):
        data = self.assert_ok(self._bearer_get(GAMES_PATH, {"page_size": 1000}))
        # ⭐ 判据=**返回里的** `page_size`：少了钳制时它会是 1000（那正是这条要抓的）。
        self.assertEqual(data["page_size"], CONTRACT_MAX_PAGE_SIZE, data)
        self.assertNotEqual(data["page_size"], 1000, data)

    def test_page_size_below_the_cap_is_echoed_unchanged(self):
        """对照：没超上限时它**照原样**回 —— 否则上一条可能是"恒定回 100"凑出来的。"""
        self.assertEqual(
            self.assert_ok(self._bearer_get(GAMES_PATH, {"page_size": 1}))["page_size"], 1
        )

    def test_the_default_page_size_is_twenty(self):
        data = self.assert_ok(self._bearer_get(GAMES_PATH))
        self.assertEqual(data["page_size"], CONTRACT_DEFAULT_PAGE_SIZE, data)
        self.assertEqual(data["page"], 1, data)

    def test_count_is_the_match_total_and_results_are_the_page_window(self):
        """`count` = **命中总数**（⛔ 不是本页条数）：两页之间它不变。"""
        first = self.assert_ok(self._bearer_get(GAMES_PATH, {"page": 1, "page_size": 1}))
        second = self.assert_ok(self._bearer_get(GAMES_PATH, {"page": 2, "page_size": 1}))
        self.assertEqual(first["count"], 1, first)  # 租户 A 只有一款游戏
        self.assertEqual(second["count"], first["count"], second)
        # 第二页没有行 ⇒ **空列表**（⛔ 不是报错、也不是 1002：见契约注释）。
        self.assertEqual(second["results"], [], second)
        self.assertEqual(second["page"], 2, second)

    def test_an_invalid_page_or_page_size_is_a_parameter_error(self):
        """畸形分页入参 ⇒ `1000`（⛔ 不是 500、也不是"静默按 1 处理"）。

        ⚠️ **与内部结算列表刻意不同**：那边的 `?page=abc` 由 DRF 分页器抛 `NotFound`
        ⇒ `1002`（那是框架行为）；本批的分页入参走**显式序列化器** ⇒ 一律 `1000`。
        """
        for query in (
            {"page": "abc"},
            {"page": 0},
            {"page": -1},
            {"page_size": "abc"},
            {"page_size": 0},
            {"page_size": -5},
        ):
            with self.subTest(query=query):
                self.assert_business_failure(self._bearer_get(GAMES_PATH, query), VALIDATION)


class MalformedInputTests(_ExtraFixture):
    """⑤ 畸形入参不 500（逐条写清期望）。"""

    def test_a_nul_order_no_does_not_match_any_route(self):
        """`%00` 经测试客户端变成**真 NUL** ⇒ 共享转换器不收它 ⇒ **404**（⛔ 不是 500）。

        ⚠️ 这就是本文件要替 `test_no_route_500` 补的那一条：那两条带 `<order_no:order_no>`
        的路由**逃出**了那张网的通用腿。
        """
        for template in (STATUS_TEMPLATE, IMAGES_TEMPLATE):
            with self.subTest(template=template):
                response = self._bearer_get(template.format(order_no="%00"))
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                self.assertEqual(response.status_code, NOT_FOUND_STATUS, response.content[:300])

    def test_a_very_long_order_no_is_treated_as_not_found(self):
        """超长单号 ⇒ 与"本租户里不存在的单号"**同形**：HTTP 200 + `1002`。

        ⚠️ **如实登记这一档为什么不是 `1000`**：本批与既有查单端点（`OrderDetailView`）
        同为"按单号取单、取不到就走 `NotFound`"的口径 ⇒ 说它"参数非法"会与"查不到"
        分成两句话，而两句话的差别本身就是一个订单枚举面（§5.3 的同族理由）。
        """
        long_no = "9" * 500
        response = self._bearer_get(STATUS_TEMPLATE.format(order_no=long_no))
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assert_business_failure(response, BUSINESS)

    def test_malformed_bills_filters_are_parameter_errors(self):
        """非法日期 / 超长单号 / 非法收入类型 / 上标数字串 ⇒ `1000`（⛔ 不是 500）。

        ⚠️ `order_source: "²"`（E-0299，评审 M1）：对外合同上第三方可发任意字串，
        `str.isdigit()` 的 Unicode 陷阱修复前在**本对外端点**同样是 HTML 500——
        与 `tests/test_settlement_api.py` 的 malformed 语料**同一次**补
        （同一共用入口 `SettlementViewSet._params` 的两张脸，缺一张都是半修）。
        """
        for query in (
            {"date_from": "garbage"},
            {"date_to": "2026-13-45"},
            {"order_no": "9" * 500},
            {"emp_type": "BOGUS"},
            {"order_source": "²"},
        ):
            with self.subTest(query=query):
                self.assert_business_failure(self._bearer_get(BILLS_PATH, query), VALIDATION)

    def test_projects_requires_a_usable_game_id(self):
        """`game_id` 缺 / 空串 / 非整数 / 非正数 ⇒ `1000`（⛔ 不回全部项目）。"""
        self.assert_business_failure(self._bearer_get(PROJECTS_PATH), VALIDATION)
        for query in ({"game_id": ""}, {"game_id": "abc"}, {"game_id": 0}, {"game_id": -3}):
            with self.subTest(query=query):
                self.assert_business_failure(self._bearer_get(PROJECTS_PATH, query), VALIDATION)

    def test_projects_with_an_unknown_game_id_is_a_parameter_error(self):
        """本租户里**没有**这个游戏 ⇒ `1000` + 一句"游戏不存在"（与内部同形口径）。"""
        body = self.assert_business_failure(
            self._bearer_get(PROJECTS_PATH, {"game_id": 987654321}), VALIDATION
        )
        self.assertIn("游戏不存在", body["msg"], body)


class DataScopeTests(_ExtraFixture):
    """⑥ 账单的数据范围：内部端点（有卡点）与对外端点（无卡点）的对照。"""

    def _internal_client(self):
        """超管会话（`make_logged_in_client(is_superuser=True)` 会顺手给「系统管理员」职位）。

        ⚠️ 在**所有游戏都建好之后**才建它：夹具的默认语义是"把**当前**租户的全部游戏勾上"
        （`AGENTS.md` §5-G 的四条语义）⇒ 先建客户端再建游戏会漏掉后建的那款。
        """
        return make_logged_in_client(domain=self.domain_a, username="extra-admin", is_superuser=True)

    def test_the_internal_and_external_sieves_return_the_same_order_nos(self):
        """⭐ 同一组筛选参数下，两条路的**订单号集合逐字相同**（总指挥点名要求的那条对照）。

        - 内部 `/api/business/settlements`：筛法 + **数据权限卡点**（超管在 `scope_queryset`
          上短路放行 ⇒ 等价于不过滤）；
        - 对外 `/api/open/v1/bills/`：同一个筛法函数、**不套**卡点（§13.10：应用凭据 = 全租户）。

        ⚠️ 先自证**非空**（`assertTrue`）：两侧都返回空集时"集合相同"是恒真的假绿 ——
        而"对外恒空"正是这次抽函数之前的真实缺陷形态（Bearer ⇒ `AnonymousUser` ⇒
        `games_of` 空集 ⇒ 恒空）。
        ⚠️ 判据用**订单号**（业务值），⛔ 不用主键 / 行数。
        """
        internal = self._session_get(
            SETTLEMENTS_PATH, client=self._internal_client(), data={"page_size": 200}
        )
        self.assertEqual(internal.status_code, 200, internal.content[:300])
        internal_body = json.loads(internal.content)
        self.assertEqual(internal_body["code"], OK, internal_body)
        external = self.assert_ok(self._bearer_get(BILLS_PATH, {"page_size": 100}))

        internal_orders = self._order_nos(internal_body["data"]["items"])
        external_orders = self._order_nos(external["results"])
        self.assertTrue(internal_orders, internal_body)  # 自证非空
        self.assertTrue(external_orders, external)  # 自证非空（这一条正是"恒空"缺陷的判据）
        self.assertEqual(external_orders, internal_orders, (external, internal_body))
        # 两侧的数**也**要对上（集合相等而条数不同 = 有一侧重复了行）。
        self.assertEqual(len(external["results"]), internal_body["data"]["total"])

    def test_the_internal_card_point_still_narrows_a_user_without_game_access(self):
        """⭐ 反向对照：抽函数**没有**把游戏范围卡点弄丢。

        一个**持 `finance.payroll` 节点、但游戏范围里没有那款游戏**的非管理员会话，
        在内部端点上**看不到**那些行（`scope_queryset` + `game_scope` 收窄到空）；
        而同一个租户的应用令牌在对外端点上**看得到**（应用凭据 = 全租户，§13.10）。
        """
        other_game = Game.objects.create(name="测试游戏丙（没给这个账号）")
        # ⚠️ `nodes=` 是通过**该用户所在 Group 的 `Role`** 挂上的（`_grant_nodes` 的语义）
        # ⇒ 非管理员必须**先进一个职位组**，否则节点一个都挂不上、端点会以 403 红
        # （而那看起来像"卡点写错了"）。用内置职位「客服」：它不是「系统管理员」
        # ⇒ `is_tenant_admin` 为假、卡点**真的会收窄**（这正是本用例要的）。
        limited = make_logged_in_client(
            domain=self.domain_a,
            username="extra-limited",
            groups=("客服",),
            nodes=("finance.payroll",),
            games=[other_game],
        )
        internal = self._session_get(
            SETTLEMENTS_PATH, client=limited, data={"page_size": 200}
        )
        self.assertEqual(internal.status_code, 200, internal.content[:300])
        internal_body = json.loads(internal.content)
        self.assertEqual(internal_body["code"], OK, internal_body)
        self.assertEqual(internal_body["data"]["items"], [], internal_body)

        external = self.assert_ok(self._bearer_get(BILLS_PATH, {"page_size": 100}))
        self.assertTrue(self._order_nos(external["results"]), external)

    def test_bills_date_filters_are_inclusive_and_local(self):
        """`date_from` / `date_to` 按**本地日期**（**两端都含**）筛 **`settled_at`**（§6.4）。

        ⚠️ 筛的列是**结算时间**（与内部结算列表逐字同口径）⇒ 本用例**先把那一行标成已结账**
        （写 `settled_at`），否则整批行都不在窗口里、"按日期筛"这件事根本没有读数。
        ⚠️ 同时**如实钉住这条口径的副作用**：`settled_at` 为 `NULL` 的行（还没结账）
        在**带日期筛选**时**不出现** —— 这正是内部那套筛法的语义，⛔ 不是"漏了一批行"。
        """
        settled_at = timezone.now()
        Performance.objects.filter(order=self.order_a, emp_type=EmpType.MAIN).update(
            settled_at=settled_at
        )
        settled_day = timezone.localdate()

        # ① 两端都含：窗口就是"今天到今天" ⇒ 那一行必须在
        data = self.assert_ok(
            self._bearer_get(
                BILLS_PATH,
                {
                    "date_from": settled_day.isoformat(),
                    "date_to": settled_day.isoformat(),
                },
            )
        )
        self.assertEqual(self._order_nos(data["results"]), {ORDER_NO_A}, data)
        # ② 副作用如实钉住：同一次调用**不带**日期筛选是两行（另一行的 `settled_at` 为 NULL）
        unfiltered = self.assert_ok(self._bearer_get(BILLS_PATH))
        self.assertEqual(len(unfiltered["results"]), 2, unfiltered)

        # ③ 明天那一档 ⇒ 空集 + `count=0`（⛔ 不是"把窗口忽略掉、回全部"）
        later = (settled_day + timedelta(days=1)).isoformat()
        data_after = self.assert_ok(
            self._bearer_get(BILLS_PATH, {"date_from": later, "date_to": later})
        )
        self.assertEqual(data_after["results"], [], data_after)
        self.assertEqual(data_after["count"], 0, data_after)


class HiddenOrderStatusTests(_ExtraFixture):
    """⭐ 评审 Important 4：对外**状态**端点不得给出隐藏单的信息（按单号 1002）。"""

    def test_the_status_of_a_hidden_order_is_1002(self):
        from tests.hall_shadow_helpers import make_hidden_order, make_mirror

        card = make_mirror(
            game=self.game_a, bound_by=self.taker, no="SYN-SHADOW-OPEN-EXTRA"
        )
        hidden = make_hidden_order(
            game=self.game_a, card=card, order_no="20261008000000000188"
        )
        response = self._bearer_get(
            STATUS_TEMPLATE.format(order_no=hidden.order_no)
        )
        self.assert_business_failure(response, BUSINESS)
        # 正对照：同一应用读那张真单的状态是 200 + code=0。
        self.assert_ok(self._bearer_get(STATUS_TEMPLATE.format(order_no=ORDER_NO_A)))
