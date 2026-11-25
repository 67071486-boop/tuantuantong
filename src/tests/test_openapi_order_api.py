"""T4：开放平台**两个查单端点**的判据（spec `docs/specs/2026-10-07-open-platform-api-design.md` §7.1 / §7.2 / §17.9）。

## 本文件证什么

`GET /api/open/v1/orders/<order_no>/` 与 `GET /api/open/v1/orders/?source_order_no=<值>`
跑在**真实 URLconf + 真实中间件 + 真实签名认证**上（不是 `APIRequestFactory` 打探针视图），
逐条钉住 spec 里那些**互相容易搞混**的口径：

1. ⭐ **字段集合恰好等于本文件里写死的字面量集合**（⛔ 不从序列化器 / 常量模块 import ——
   `AGENTS.md` §5-G / E-0024「钉值不钉常量」：import 常量的话，「改实现 + 改常量」一起改
   就不会红）。C 组（员工身份）/ D 组（内部痕迹）字段与任何内部主键 `id` **断言不在**。
2. 认证：**凭据缺失 / 凭据无效 / 会话打对外端点** ⇒ 都 **401**（Bearer 那句文案；
   会话对照：同一会话打 `/api/auth/me` ⇒ 200）。
3. 两种「查不到」**同形**：**另一款游戏的单** / 单不存在 ⇒ 都与「不存在」同形
   （游戏已不是可见性维度，§13.10）。
4. `source_order_no` 的三种取值：**没传 ⇒ `1000`**、**传了空串 ⇒ 空列表 + `total=0`**、
   **命中多条 ⇒ `total` = 命中总数、`items` ≤ 20（按 `id` 升序）**。
5. 跨租户：租户 A 的应用查不到租户 B 的单；A 的凭据打 B 的域名 ⇒ 401。
   ⚠️ 判据用**租户内唯一、且两租户取值不同**的业务值（订单号 / 来源单号），⛔ **不用主键**
   （两个 schema 的主键序列各自从 1 开始，用主键指认「另一个租户那一行」会恒真或恒假）。
6. ⛔ **游戏范围 / 节点门已随 v1 面删除**（§13.10 / §13.1）——相关的旧钉点退场，
   "另一款游戏查得到"反转为新契约的钉子；逐条理由见各自位置的登记块。

## 夹具纪律

- 凭据一律**合成值**（`AGENTS.md` §5-C），⛔ 与任何真实租户的 appKey / secret 无关。
- 失败计数在 `OpenApiRateBucket` 表里，随用例事务回滚。不要再当成跨用例的缓存计数。
- 凭据只有一种：**Bearer 令牌**（集合与详情两条路径都只认令牌，设计文档 §13.14-3），
  令牌一律**当场签发**（`tokens.issue`），⛔ 不抄签发逻辑；旧 HMAC 签名面已整体删除（§13.1）。
  "旧协议的头打进来会怎样"仍有一条判据（`test_expired_timestamp_returns_401`，
  素材是直接塞那三个头）。签名协议本身仍由 `tests/test_openapi_auth.py` 的探针视图逐条钉着。

## v1 面收口后的口径

1. **节点门与游戏范围过滤已删除** ⇒ 旧钉点退场 5 条、**反转** 2 条
   （"另一款游戏看不见" ⇒ 今天**看得见**）。⛔ 没有放宽任何一条：退场的钉的是**已删除的行为**，
   反转的把**新契约**钉住（把旧过滤加回来就红）。逐条理由在各自位置的登记块。
2. **夹具**：`make_app` 只有 `name` / `app_key` / `app_secret` / 创建人。
   ⚠️ 本文件的断言**全部打在端点契约上**（字段白名单 / `source_order_no` 取值 /
   跨租户 / 401），⛔ 与"应用被授权了什么"无关。
"""
import json
import time
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.claim import claim_order
from apps.business.game_scope import assert_game_id_allowed, filter_by_game_ids
from apps.business.models import (
    EmpType,
    Game,
    GameServer,
    Order,
    OrderStatus,
    Performance,
    Project,
    ProjectCategory,
)
from apps.business.performance import set_taker
from apps.openapi import tokens
from apps.openapi.authentication import BEARER_INVALID_CREDENTIALS_MSG
from apps.openapi.models import ApiApp
from apps.platform.provisioning import provision_tenant

User = get_user_model()

#: 对外端点的两条路径（与 `apps/openapi/urls.py` 逐字一致；⚠️ 都带尾斜杠）。
LIST_PATH = "/api/open/v1/orders/"
#: 详情路径的**模板**（`{order_no}` 由用例填）。
DETAIL_TEMPLATE = "/api/open/v1/orders/{order_no}/"

#: 合成凭据（⛔ 不是任何真实租户的凭据）。
APP_KEY = "probeorderkey000001"
APP_SECRET = "probe-order-secret-000000000000001"
#: 另一把 key：用来证明「key 不存在」与「签名错」回同一句（§6.3 第 3/4 步）。
UNKNOWN_KEY = "probeorderkey0000ff"

#: 来源 IP（文档保留段，⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.11"

#: 业务码字面值（E-0024：写数字，让"码值被改掉"在用例里可见）。
OK = 0
VALIDATION = 1000
BUSINESS = 1002

#: 节点门被拒时的那句（DRF 的 `PermissionDenied` 默认文案，本项目 `LANGUAGE_CODE=zh-hans`

# ── 字段白名单：**写死的字面量集合**（⛔ 不从 `apps.openapi.serializers` import）──────
TOP_LEVEL_KEYS = {
    "order_no",
    "source_order_no",
    "status",
    "status_text",
    "game",
    "project",
    "total_price",
    "created_at",
    "updated_at",
    "source",
    "amounts",
    "costs",
    # ⭐ D-0320：接手人显示名（员工身份口径的唯一显式例外，见 §3.7 改准）。
    "taker_name",
    "content",
    "order",
    "player",
    "customer",
}
AMOUNT_KEYS = {
    "total_price",
    "received_price",
    "platform_price",
    "hall_price",
    "refund_fee",
    "fee",
}
COST_ROW_KEYS = {"temp_amount", "bonus_amount", "taker_price", "commission_rate"}
CONTENT_KEYS = {"content", "demand", "time_requirement"}
ORDER_GROUP_KEYS = {
    "order_type",
    "server",
    "category",
    "project_text",
    "is_urgent",
    "tag",
    "completed_at",
    "payment_method",
    "payment_status",
}
PLAYER_KEYS = {
    "game_account",
    "game_sub_account",
    "game_role_name",
    "current_info_1",
    "current_info_2",
}
CUSTOMER_KEYS = {"customer_phone", "customer_qq", "customer_ww"}

#: ⛔ **不给**的字段（业务方裁定，spec §17.9 的 C / D 两组 + 内部主键）。
#: ⚠️ 这是**裁决**不是遗漏：写进用例是为了让"后来者顺手补上"当场变红。
#: ⛔⭐ D-0320：顶层 `taker_name`（接手人显示名）是员工身份口径的
#: **唯一显式例外**（供第三方识别代练通接手单、跳过自动验收转人工核）⇒ 它**有意不在**
#: 下面的集合里；其余员工身份字段仍一律不给。
FORBIDDEN_KEYS = {
    # C 组：员工身份
    "sales_agent",
    "sales_user",
    "hall_publisher",
    "created_by",
    "updated_by",
    # D 组：内部痕迹
    "remark",
    "deleted_at",
    "deleted_by",
    # 任何内部主键
    "id",
    # 也不给"利润"（spec §7.1 的技术性意见：它会冻结一个内部算式）
    "profit_expected",
    "profit_actual",
}


def _all_keys(node, into=None):
    """递归收集载荷里出现的**全部 dict 键**（判"某个键不在载荷里"要用它，⛔ 不是只看顶层）。"""
    into = set() if into is None else into
    if isinstance(node, dict):
        for key, value in node.items():
            into.add(key)
            _all_keys(value, into)
    elif isinstance(node, list):
        for item in node:
            _all_keys(item, into)
    return into


class _OpenApiOrderFixture(TenantTestCase):
    """对外端点的共用夹具：一个应用 + 两只游戏 + 可造的订单。"""

    def setUp(self):
        super().setUp()
        # ⚠️ 必须清：§6.3 的按来源 IP 失败计数落 LocMemCache，跨用例**不清空**
        # （见模块 docstring）。不清的话，前面某条用例的失败会把本用例的合法请求挡成 401。
        cache.clear()
        # ⚠️ `TenantTestCase.domain` 是一个 `Domain` **实例**（不是字符串）⇒ 取它的 `domain` 字段。
        self.domain_name = self.domain.domain
        self.client = Client(SERVER_NAME=self.domain_name)
        self.game_a = Game.objects.create(name="原神")
        self.game_b = Game.objects.create(name="崩铁")
        self.other_game = Game.objects.create(name="没勾给应用的游戏")

    # ── 造物 ───────────────────────────────────────────────────────────
    def make_app(self, *, name="探针应用", app_key=APP_KEY, app_secret=APP_SECRET):
        """建一个应用（只有 `name` / `app_key` / `app_secret` / 创建人）。

        ⚠️ 应用上的 `nodes` / `games` / `all_games` 授权列已随 v1 面删除（设计文档 §13.1）。
        **判据不受影响**：本文件的断言打在**端点契约**上（字段白名单 / 查询参数 / 跨租户 /
        401），⛔ 与"这个应用被授权了什么"无关（§13.10：一个应用凭据 = 本租户全部业务数据）。
        """
        creator, _ = User.objects.get_or_create(
            username="openapi-order-probe", defaults={"password": "unused-in-this-test"}
        )
        return ApiApp.objects.create(
            name=name, app_key=app_key, app_secret=app_secret, created_by=creator
        )

    def make_order(self, game=None, order_no="20261007000000000001", **fields):
        """造一张单（只有 `game` 与 `order_no` 必填；其余按需覆盖）。"""
        return Order.objects.create(
            game=game or self.game_a,
            order_no=order_no,
            project_text=fields.pop("project_text", "占位项目"),
            **fields,
        )

    def make_main_performance(self, order, **fields):
        """打手那条绩效行（`uniq_order_main_taker`：一个订单**最多一条**）。"""
        return Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, **fields
        )

    def make_sales_performance(self, order, user, **fields):
        """客服那条绩效行。`uniq_performance_order_user` 只约束「同单同一个 user」
        ⇒ **换一个 user 就能有第二条**（本文件靠它钉住 `costs.sales` 是列表）。"""
        return Performance.objects.create(
            order=order, emp_type=EmpType.SALES, user=user, **fields
        )

    # ── 发请求 ─────────────────────────────────────────────────────────
    def bearer_get(self, path, data=None, *, app=None, remote_addr=SOURCE_IP, token=None):
        """带 **Bearer 令牌**发一条 GET（集合与详情路径都只认令牌）。

        ⚠️ 令牌**当场签发**（`tokens.issue`，用它自己的密钥）——⛔ 本文件不复制任何
        签发 / 编码逻辑（那是"用被测者证明被测者"）。本文件只有这一种凭据形态。
        """
        app = app if app is not None else self.app
        token = tokens.issue(app.app_key, app.app_secret) if token is None else token
        return self.client.get(
            path,
            data or {},
            HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR=remote_addr,
        )

    # ── 断言小工具 ─────────────────────────────────────────────────────
    def assert_ok(self, response):
        """成功路径：HTTP 200 + `code=0`，返回 `data`。"""
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], OK, body)
        return body["data"]

    def assert_business_failure(self, response, code):
        """业务失败：**HTTP 200** + **指定的**业务码（本仓口径：状态码只表达传输/认证层）。

        ⚠️ `code` 是**承重入参**（不是装饰）：1000（参数缺失）与 1002（不存在 / 零游戏范围）
        的 msg 在不同分支上可能碰巧相同，只断 msg 会让「把 1000 写成 1002」活下来。
        """
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], code, body)
        return body


class OrderDetailPayloadTests(_OpenApiOrderFixture):
    """§7.1：字段白名单逐条（恰好相等 + C/D 组不在）。"""

    def setUp(self):
        super().setUp()
        self.app = self.make_app()

    def _rich_order(self):
        """一张**每个字段都有值**的单（让"值"也能被断言，不只是键在不在）。"""
        category = ProjectCategory.objects.create(game=self.game_a, name="代练分类甲")
        server = GameServer.objects.create(game=self.game_a, name="天空岛")
        project = Project.objects.create(category=category, name="配置里的项目名")
        return Order.objects.create(
            game=self.game_a,
            order_no="20261007000000000021",
            status=OrderStatus.IN_PROGRESS,
            order_type="BOOST",
            server=server,
            category=category,
            project=project,
            project_text="自由文本项目名",
            content="客户原始需求原文",
            demand="需求描述",
            time_requirement="24小时",
            game_account="账号甲",
            game_sub_account="子账号甲",
            game_role_name="角色甲",
            current_info_1="当前信息一",
            current_info_2="当前信息二",
            customer_phone="13900000001",
            customer_qq="qq-0001",
            customer_ww="ww-0001",
            total_price=Decimal("100.50"),
            received_price=Decimal("90.00"),
            platform_price=Decimal("80.00"),
            hall_price=Decimal("70.00"),
            refund_fee=Decimal("5.50"),
            fee=Decimal("2.00"),
            payment_method="支付宝",
            payment_status="UNSETTLED",
            source="淘宝",
            source_order_no="TB-2026-0001",
            is_urgent=True,
            tag="加急标记",
            completed_at=timezone.now(),
        )

    def test_payload_keys_are_exactly_the_frozen_whitelist(self):
        """⭐ 顶层 + 六个分组的键集合**恰好**等于本文件里写死的字面量集合。

        判别力：多给一个键（例如顺手把 `remark` 加上）⇒ 红；少给一个键 ⇒ 红。
        ⛔ 期望值**不是**从 `apps.openapi.serializers` import 的（那会变成自指断言，E-0024）。
        """
        order = self._rich_order()
        payload = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=order.order_no))
        )

        self.assertEqual(set(payload), TOP_LEVEL_KEYS)
        self.assertEqual(set(payload["amounts"]), AMOUNT_KEYS)
        self.assertEqual(set(payload["costs"]), {"main", "sales"})
        self.assertEqual(set(payload["content"]), CONTENT_KEYS)
        self.assertEqual(set(payload["order"]), ORDER_GROUP_KEYS)
        self.assertEqual(set(payload["player"]), PLAYER_KEYS)
        self.assertEqual(set(payload["customer"]), CUSTOMER_KEYS)

    def test_costs_rows_have_exactly_their_four_keys(self):
        """`costs` 每一行的键集合也**恰好**是四个（同上，⛔ 不 import 常量）。"""
        order = self._rich_order()
        sales_user = User.objects.create_user(username="sales-probe", password="pw-123456")
        self.make_main_performance(order, temp_amount=Decimal("10.00"))
        self.make_sales_performance(order, sales_user, temp_amount=Decimal("1.00"))

        payload = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=order.order_no))
        )
        self.assertEqual(len(payload["costs"]["main"]), 1)
        self.assertEqual(len(payload["costs"]["sales"]), 1)
        for row in payload["costs"]["main"] + payload["costs"]["sales"]:
            self.assertEqual(set(row), COST_ROW_KEYS)

    def test_forbidden_keys_are_absent_everywhere(self):
        """⭐ C 组 / D 组 / 内部主键**一个都不在**载荷里（⛔ 不是"只在顶层看不到"）。

        判据打在**递归收集的全部键**上：把它们塞进任何一个分组（例如 `order.created_by`）
        同样要红。
        """
        order = self._rich_order()

        payload = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=order.order_no))
        )
        self.assertEqual(_all_keys(payload) & FORBIDDEN_KEYS, set())
        # 反面自证：上面那句**不是**在用一个瞎了的收集器——它确实看得见**嵌套**的键。
        self.assertEqual(
            _all_keys({"id": 1, "outer": {"created_by": 2}, "rows": [{"remark": 3}]}),
            {"id", "outer", "created_by", "rows", "remark"},
        )

    def test_payload_values_come_from_that_order(self):
        """值是**这一张单**的（钉住"拿对了行"，不是"随便回了一张"）。"""
        order = self._rich_order()
        payload = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=order.order_no))
        )
        self.assertEqual(payload["order_no"], "20261007000000000021")
        self.assertEqual(payload["source_order_no"], "TB-2026-0001")
        self.assertEqual(payload["status"], "IN_PROGRESS")
        # 中文名由 Django 从 `OrderStatus.choices` 取（⛔ 不是第三份映射表）。
        self.assertEqual(payload["status_text"], "代练中")
        self.assertEqual(payload["game"], "原神")
        self.assertEqual(payload["project"], "配置里的项目名")
        self.assertEqual(payload["order"]["server"], "天空岛")
        self.assertEqual(payload["order"]["category"], "代练分类甲")
        self.assertEqual(payload["order"]["project_text"], "自由文本项目名")
        self.assertEqual(payload["order"]["is_urgent"], True)
        self.assertEqual(payload["customer"]["customer_phone"], "13900000001")
        # `current_info_1/2` **原样透传**（⛔ 不解释语义）。
        self.assertEqual(payload["player"]["current_info_1"], "当前信息一")
        self.assertEqual(payload["player"]["current_info_2"], "当前信息二")

    def test_null_amounts_stay_null_and_values_are_two_decimal_strings(self):
        """金额口径：有值 ⇒ **字符串两位小数**；`None` ⇒ **`null`**（⛔ 不是 `"0.00"`）。

        判别力：把 `None` 渲染成 `"0.00"` ⇒ 红（那会让第三方把「未知」读成「零」，
        而 D-0015 之下 0 与负数都是**合法且有意义的**边界值）。
        """
        order = self.make_order(
            order_no="20261007000000000022",
            total_price=Decimal("0.00"),
            refund_fee=Decimal("-3.50"),
        )
        # 构造物自证：四个可空金额列**真的**是 NULL（不是"我忘了赋值"）。
        order.refresh_from_db()
        self.assertIsNone(order.received_price)
        self.assertIsNone(order.platform_price)
        self.assertIsNone(order.hall_price)
        self.assertIsNone(order.fee)

        payload = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=order.order_no))
        )
        amounts = payload["amounts"]
        # `0` 与**负数**都要照原样给字符串（⛔ 不许真值判断把它们吞成 null）。
        self.assertEqual(amounts["total_price"], "0.00")
        self.assertEqual(amounts["refund_fee"], "-3.50")
        for key in ("received_price", "platform_price", "hall_price", "fee"):
            self.assertIsNone(amounts[key], key)
        self.assertEqual(payload["total_price"], "0.00")

    def test_both_sales_rows_are_returned(self):
        """⭐ `costs.sales` 是**列表**：同一订单的第二条 SALES 行也必须在（⛔ 不许只给第一条）。

        ⚠️ 判别力来自"多于一"：数据层**没有**任何约束限制 SALES 只能一条
        （`uniq_performance_order_user` 只约束「同单同一个 user」）⇒ 单值形状
        （`costs.sales` 给一个对象）会**静默**只留一条，而这是对外契约。
        """
        order = self.make_order(order_no="20261007000000000023")
        first = User.objects.create_user(username="sales-first", password="pw-123456")
        second = User.objects.create_user(username="sales-second", password="pw-123456")
        self.make_sales_performance(order, first, bonus_amount=Decimal("11.00"))
        self.make_sales_performance(order, second, bonus_amount=Decimal("22.00"))

        payload = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=order.order_no))
        )
        rows = payload["costs"]["sales"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(row["bonus_amount"] for row in rows), ["11.00", "22.00"])
        self.assertEqual(payload["costs"]["main"], [])

    def test_soft_deleted_performance_rows_are_not_exposed(self):
        """软删的绩效行不进对外载荷（与全仓常规读面同口径：软删 = 不在读面上）。"""
        order = self.make_order(order_no="20261007000000000024")
        self.make_main_performance(order, temp_amount=Decimal("9.99"))
        Performance.objects.filter(order=order).update(deleted_at=timezone.now())

        payload = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=order.order_no))
        )
        self.assertEqual(payload["costs"]["main"], [])


class TakerNameTests(_OpenApiOrderFixture):
    """⭐ D-0320（2026-10-28）：顶层 `taker_name` —— 接手人显示名的四条口径。

    判据逐条对应 `apps/openapi/serializers.py::_taker_name` 的契约注释：

    ① 值 = 接手人的**显示名**（昵称优先、空串回落登录账号），且两条「接手」写路径
      （`/claim` 的条件更新与 `/assign` 的 `set_taker`）写的是同一列 ⇒ 同一个值；
    ② 无 MAIN 绩效行 ⇒ `null`；
    ③ 「无主而有 `claimed_at`」⇒ **仍 `null`**（⛔ 不拿接单时刻推断，models 不变量 ③）；
    ④ 增量流与详情**同一出口**（D13）⇒ 流里也看得到（两条路逐字对拍另有既有网钉着）。

    期望值一律**钉字面量**（"接手小明" / 登录账号），⛔ 不 import `user_display_name`
    来算——用被测者的口径算被测者是自指断言（E-0024 同族）。
    """

    def setUp(self):
        super().setUp()
        self.app = self.make_app()

    def _claimable_order(self, order_no):
        """一张可被 `/claim` 抢走的单（`test_claim.py` 同款最小夹具：待抢 + 有大厅价 + 无主 MAIN 行）。"""
        order = Order.objects.create(
            game=self.game_a,
            order_no=order_no,
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        return order

    def test_the_claim_path_shows_the_takers_display_name(self):
        """① `/claim` 那条写路径 ⇒ 显示名（`first_name` 有值 ⇒ 用昵称，不用登录账号）。"""
        order = self._claimable_order("20261028000000000001")
        taker = User.objects.create_user(
            username="taker-claim-a", first_name="接手小明", password="pw-123456"
        )
        claim_order(user_id=taker.id, order_id=order.id, nodes={"grab_hall.take_order"})

        payload = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=order.order_no))
        )
        self.assertEqual(payload["taker_name"], "接手小明")

    def test_the_assign_path_shows_the_same_display_name(self):
        """①′ `/assign`（`performance.set_taker`）那条写路径 ⇒ 同一个值；无昵称 ⇒ 回落登录账号。"""
        order = self.make_order(
            order_no="20261028000000000002", hall_price=Decimal("88.00")
        )
        main = self.make_main_performance(order, temp_amount=Decimal("88.00"))
        taker = User.objects.create_user(username="taker-assign-b", password="pw-123456")
        set_taker(main, taker)

        payload = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=order.order_no))
        )
        self.assertEqual(payload["taker_name"], "taker-assign-b")

    def test_an_order_without_a_main_row_gives_null(self):
        """② 一张**没有任何绩效行**的单 ⇒ `null`（判空不依赖行存在）。"""
        order = self.make_order(order_no="20261028000000000003")

        payload = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=order.order_no))
        )
        self.assertIsNone(payload["taker_name"])

    def test_an_ownerless_main_row_with_claimed_at_still_gives_null(self):
        """③ ⭐「无主而有 `claimed_at`」⇒ **仍 `null`**。

        models 不变量 ③：这种行**合法**（B-1 存量行就是这个形状）⇒ ⛔ 不许拿接单时刻
        反推出「有人接手」。判别力：把 `_taker_name` 的判空改成看 `claimed_at` ⇒ 本用例红。
        """
        order = self.make_order(order_no="20261028000000000004")
        # 有接单时刻、**无归属人**——构造物与不变量 ③ 的形状逐字一致。
        self.make_main_performance(order, claimed_at=timezone.now())

        payload = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=order.order_no))
        )
        self.assertIsNone(payload["taker_name"])

    def test_the_updated_since_feed_rows_carry_the_field_too(self):
        """④ 增量流的每一行也带这个键、值同详情（详情与流共用 `order_payload`，D13）。"""
        now = timezone.now()
        order = self.make_order(
            order_no="20261028000000000005", hall_price=Decimal("88.00")
        )
        main = self.make_main_performance(order, temp_amount=Decimal("88.00"))
        taker = User.objects.create_user(
            username="taker-feed-c", first_name="接手小红", password="pw-123456"
        )
        set_taker(main, taker)
        Order.objects.filter(pk=order.pk).update(updated_at=now)

        payload = self.assert_ok(
            self.bearer_get(
                LIST_PATH,
                {"updated_since": (now - timedelta(minutes=10)).isoformat()},
            )
        )
        self.assertEqual(payload["count"], 1, payload)
        self.assertEqual(
            [row["taker_name"] for row in payload["results"]], ["接手小红"]
        )


class OrderDetailAccessTests(_OpenApiOrderFixture):
    """认证分界：无凭据 / 凭据无效 / 旧协议头都 ⇒ **401 + Bearer 那句**（详情路径只认令牌）。

    ⚠️ 签名协议本身的判据（缺头 / 签名错 / 时间窗）由 `tests/test_openapi_auth.py`
    的探针视图逐条钉着；这里钉的是**对外路径**的行为。
    """

    def setUp(self):
        super().setUp()
        self.app = self.make_app()

    def test_missing_signature_headers_return_401(self):
        """无凭据打详情路径 ⇒ **401 + Bearer 那句**。

        ⚠️ 实质判据：没有凭据 ⇒ 401 + 本仓信封 + `code=401` + `data=None`。
        ⚠️ 方法名不改（历史按名引用）；它今天管的是"没带凭据"，⛔ 不再是"没带**签名**头"
        （签名那五句仍由 `tests/test_openapi_auth.py` 的探针视图逐条钉着）。
        """
        response = self.client.get(DETAIL_TEMPLATE.format(order_no="x"), REMOTE_ADDR=SOURCE_IP)
        self.assertEqual(response.status_code, 401)
        body = json.loads(response.content)
        self.assertEqual(body["code"], 401)
        self.assertEqual(body["msg"], BEARER_INVALID_CREDENTIALS_MSG)
        self.assertIsNone(body["data"])

    def test_wrong_signature_returns_401(self):
        """凭据不对打详情路径 ⇒ **401 + Bearer 那句**（签名头不被读）。

        ⚠️ 素材是一把**形状合法但不存在的令牌**；判据实质：**凭据不对 ⇒ 401 +
        与"没有凭据"同一句话**（⛔ 不给枚举面）。
        """
        order = self.make_order()
        response = self.bearer_get(
            DETAIL_TEMPLATE.format(order_no=order.order_no), token="0" * 64
        )
        self.assertEqual(response.status_code, 401)
        body = json.loads(response.content)
        self.assertEqual(body["code"], 401)
        self.assertEqual(body["msg"], BEARER_INVALID_CREDENTIALS_MSG)

    def test_expired_timestamp_returns_401(self):
        """旧签名协议的时间戳头打对外路径 ⇒ **401 + Bearer 那句**（路径不再读它）。

        ⚠️ **签名时间窗本身的判据没有丢**：`tests/test_openapi_auth.py::ApiAppSignatureClockTests`
        用文件内探针视图直打认证类（±300 秒边界逐条钉住，`EXPIRED_MSG` 也在那儿）。
        ⚠️ 单号不必存在：认证失败发生在取对象**之前**。
        """
        stale = int(time.time()) - 301
        # 素材 = 按签名协议形状算好的过期时间戳 + 那三个旧头（值不被读）：
        # 带上旧协议的头 ⇒ 401 + Bearer 那句（证明这条路径今天只认令牌）。
        response = self.client.get(
            DETAIL_TEMPLATE.format(order_no="20260901000000000099"),
            HTTP_X_TT_APPKEY=self.app.app_key,
            HTTP_X_TT_TIMESTAMP=str(stale),
            HTTP_X_TT_SIGN="0" * 64,
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(json.loads(response.content)["msg"], BEARER_INVALID_CREDENTIALS_MSG)

# ⛔ 此处原有两条**节点门**用例（「一个节点都没勾 ⇒ 403」与「只勾一个能过、勾别的 403」），
# 被测对象已随 v1 面删除（§13.1，节点门与 `apps/openapi/permissions.py` 不存在）。
# 如实登记丢掉的判别力：**应用被误配成"没有权限"时不再有任何提示**（§4-V2 代价的可见面）。

    # ⛔ 此处原有 `test_zero_game_scope_says_so_explicitly`（**零游戏范围 ⇒ 1002 + 说破**），
# 游戏范围过滤已随 v1 面删除（§13.10）。丢掉的判别力：**"配置得对不对"这一档提示整个消失**
# —— 应用能读什么恒为「本租户全部业务数据」，没有"配错了"这种状态。

    def test_order_of_another_game_is_now_visible_too(self):
        """⭐ **游戏不再是可见性维度**：属于**另一款游戏**的单今天**照样查得到**。

        ⚠️ 判据**反过来钉新契约**（§13.10：一个应用凭据 = 本租户全部业务数据）：
        另一款游戏的单**必须查得到**，且载荷与"范围内"那张**同形**（都是 `code=0`）。

        ⚠️ **它仍然有判别力**：哪天有人把游戏范围过滤**加回来**（或从 `git` 里捞回旧实现），
        本条**当场红**（会拿到 `1002` 而不是 `code=0`）。
        ⚠️ 对照在下面一条（`test_in_scope_order_of_the_same_app_is_visible`）：
        两条一起才说明"查得到"不是由"这条路对什么都说有"凑出来的。
        """
        outside = self.make_order(
            game=self.other_game, order_no="20261007000000000031"
        )

        payload = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=outside.order_no))
        )
        self.assertEqual(payload["order_no"], outside.order_no)
        # ⚠️ 载荷里的游戏名确实是**另一款**游戏（否则"查得到"可能是查到了别的单）。
        self.assertEqual(payload["game"], self.other_game.name)

    def test_a_missing_order_is_a_business_failure(self):
        """✅ 对照（⛔ 别把它读成冗余）：**真正不存在**的单仍是 `1002` + `data=None`。

        与上一条配对：上面那条证明"另一款游戏的单查得到"，本条证明"查不到"这一档
        **仍然存在**（否则一个"对什么都说有"的实现也能让上一条绿）。
        """
        missing = self.bearer_get(DETAIL_TEMPLATE.format(order_no="20260901000000000099"))
        body = self.assert_business_failure(missing, BUSINESS)
        self.assertIsNone(body["data"])

    def test_in_scope_order_of_the_same_app_is_visible(self):
        """正向对照：**同一夹具下**范围内那张单**真的**查得到（否则上面那条可能是空转）。"""
        inside = self.make_order(order_no="20261007000000000032")
        payload = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=inside.order_no))
        )
        self.assertEqual(payload["order_no"], "20261007000000000032")

    def test_a_nul_byte_in_the_path_does_not_reach_the_view(self):
        """路径段里的 NUL 字节（`%00`）⇒ **路由不匹配 ⇒ 404**（⛔ 不是 500、也不是 401）。

        判别力：把路由的路径参数换成裸 `<str:order_no>` 时，`%00` 会**匹配**这条路由
        ⇒ 带着 NUL 进 `filter(order_no=…)` ⇒ PostgreSQL 的
        `DataError: text fields cannot contain NUL` ⇒ **HTML 500**
        （真实的红色判据是 `apps/business/converters.py` 的 docstring 与
        `tests/test_p4_no_500.py` 的同一个形状）。
        ⚠️ 本条用**不带签名头**的请求：这样"路由器没匹配"（404）与"匹配了但没凭据"（401）
        两种结果**可分辨**——正是本用例要钉的那件事。
        """
        list_path_nul = "/api/open/v1/orders/%00/"
        response = self.client.get(list_path_nul, REMOTE_ADDR=SOURCE_IP)
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, 404, response.content[:300])

    def test_a_nul_byte_cannot_reach_the_list_endpoint_via_the_query_string(self):
        """查询串里的 `%00` 进不到 `filter(source_order_no=…)`：它是**参数值**、先过序列化器。

        与上一条不同：查询串里的 NUL **不是**路径段（路由照样匹配），所以它必须由
        「序列化器 → 查询」这条路自己挡住。⚠️ 实测的答复是 **200 + `code=1000`**：
        DRF 的 `CharField` 自己挂了 `ProhibitNullCharactersValidator`
        （`rest_framework/fields.py` 的 `CharField.__init__` 末两行）⇒ 校验期就拒了，
        ⛔ 到不了 PostgreSQL（到了就是 `DataError` ⇒ HTML 500）。
        HTTP 200 是本仓业务失败的口径（状态码只表达传输 / 认证层）。
        """
        response = self.bearer_get(LIST_PATH, {"source_order_no": "\x00"})
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        body = self.assert_business_failure(response, VALIDATION)
        self.assertIsNone(body["data"])


# ⛔ 此处原有 `AllGamesScopeTests`（4 条：钉"全部游戏"模式的查询时求值与两条对照），
# 被测对象（`ApiApp` 上那列 + 查单端点的子查询支）随 v1 面删除（§13.1 / §13.10）。
# 如实登记丢掉的判别力：**"授权面随租户游戏变化而自动扩大"不再有判据**——
# 授权面已没有可收窄的维度（恒为整个租户，§4-V2）。旧语义见 git 历史与设计文档 §35。

class OrderSourceQueryTests(_OpenApiOrderFixture):
    """§7.2：`?source_order_no=` 的三种取值与 `total` 的语义。"""

    def setUp(self):
        super().setUp()
        self.app = self.make_app()

    def test_missing_parameter_returns_1000(self):
        """**两个筛单参数都没给** ⇒ `1000`（参数缺失）；⛔ 不是空列表、更不是全部订单。

        ⚠️ 期望文案是「缺少参数：source_order_no 或 updated_since」（设计文档 D15 / §7.4）：
        只提 `source_order_no` 会**说假话** —— 有第二个合法筛单参数
        却不提，调用方拿到那句会以为"必须给单号"。⚠️ **判据一个字未改**：仍是
        `code == 1000` + 那句文案（文案是**对外契约**，逐字钉着）。
        ⚠️ 这条文案的**唯一出处**是 `OrderQuerySerializer` 的字段 `error_messages` +
        它的 `validate()`（后者管"一个都没传"那一档）——⛔ 用例里不 import 那个常量
        （`AGENTS.md` §5-E「钉值不钉常量」：import 常量会让"改实现 + 改常量"自洽而检不出）。
        """
        self.make_order(source_order_no="TB-A")
        response = self.bearer_get(LIST_PATH)
        body = self.assert_business_failure(response, VALIDATION)
        self.assertEqual(body["code"], VALIDATION)
        self.assertEqual(body["msg"], "缺少参数：source_order_no 或 updated_since")

    def test_blank_parameter_is_not_treated_as_missing(self):
        """**传了空串** ⇒ 按空串查：命中 0 条 ⇒ 空列表 + `total=0`。

        判别力（§5-E 的 `is not None` 同族）：把空串当成"没传"而回**全部订单** ⇒ 本用例红
        （本夹具里**确实有**一张别的来源单号的单，所以"回全部"会给出 `total=1`），
        而那个缺陷的形状是"查什么都返回一堆单"。
        """
        self.make_order(order_no="20261007000000000041", source_order_no="TB-A")
        response = self.bearer_get(LIST_PATH, {"source_order_no": ""})
        payload = self.assert_ok(response)
        self.assertEqual(set(payload), {"items", "total"})
        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["items"], [])

    def test_blank_parameter_finds_the_order_whose_source_no_is_blank(self):
        """⭐ 对照：**空串是一个真的查询值**——库里那张 `source_order_no=""` 的单要查得到。

        ⚠️ 与上一条合起来才完整：上一条证"空串 ≠ 没传"，本一条证"空串**确实**在查"
        （否则一个恒回空列表的实现也能让上一条通过）。
        """
        blank = self.make_order(order_no="20261007000000000043", source_order_no="")
        self.make_order(order_no="20261007000000000044", source_order_no="TB-B")

        payload = self.assert_ok(self.bearer_get(LIST_PATH, {"source_order_no": ""}))
        self.assertEqual(payload["total"], 1)
        self.assertEqual(
            [item["order_no"] for item in payload["items"]], [blank.order_no]
        )

    def test_two_matches_return_two_items_and_total_two(self):
        """命中 2 条 ⇒ 回 2 条、`total=2`。

        ⭐ 这条是"**只取第一条**"的杀手：实现若只回第一张单，`total` 与 `items` 都会是 1。
        """
        first = self.make_order(order_no="20261007000000000051", source_order_no="TB-DUP")
        second = self.make_order(order_no="20261007000000000052", source_order_no="TB-DUP")
        # 构造物自证：这两张单真的在库里、且都带着同一个来源单号。
        self.assertEqual(
            Order.objects.filter(source_order_no="TB-DUP").count(), 2
        )
        self.assertLess(first.pk, second.pk)

        payload = self.assert_ok(self.bearer_get(LIST_PATH, {"source_order_no": "TB-DUP"}))
        self.assertEqual(payload["total"], 2)
        self.assertEqual(
            [item["order_no"] for item in payload["items"]],
            ["20261007000000000051", "20261007000000000052"],
        )

    def test_one_match_returns_that_match_not_merely_the_first_order(self):
        """⭐ 对照：命中 **1** 条 ⇒ 回的是**那一条**（⛔ 不是"库里第一张单"）。

        判别力：把实现写成"忽略筛选、取 `Order.objects.first()`"时，本用例红
        （本夹具里另有更早建的单，而它**不是**命中的那条）。
        """
        decoy = self.make_order(
            order_no="20261007000000000061", source_order_no="TB-DECOY"
        )
        target = self.make_order(
            order_no="20261007000000000062", source_order_no="TB-ONLY"
        )
        self.assertLess(decoy.pk, target.pk)  # 构造物：诱饵确实更早

        payload = self.assert_ok(self.bearer_get(LIST_PATH, {"source_order_no": "TB-ONLY"}))
        self.assertEqual(payload["total"], 1)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["order_no"], "20261007000000000062")

    def test_more_than_the_limit_reports_the_full_total(self):
        """命中 **21** 条 ⇒ `total=21`（命中总数）、`items` **只有 20** 条且按 `id` 升序。

        ⭐ 两件事一起钉：① 「按上限截断」真的生效；② `total` ⛔ 不是"本页条数"
        ——`total = len(items)` 那种**自洽**写法只有这条用例能抓住（§7.2 逐字冻结）。
        """
        order_nos = [f"2026100700000000{index:04d}" for index in range(100, 121)]
        for order_no in order_nos:
            self.make_order(order_no=order_no, source_order_no="TB-MANY")
        self.assertEqual(Order.objects.filter(source_order_no="TB-MANY").count(), 21)

        payload = self.assert_ok(self.bearer_get(LIST_PATH, {"source_order_no": "TB-MANY"}))
        self.assertEqual(payload["total"], 21)
        self.assertEqual(len(payload["items"]), 20)
        # 按 `id` 升序 = **创建顺序**（`id` 自增），且**恰好前 20 条**（第 21 条不在）。
        self.assertEqual(
            [item["order_no"] for item in payload["items"]], order_nos[:20]
        )

    def test_orders_of_another_game_are_in_the_list_too(self):
        """⭐ **列表也不再按游戏收窄**：两款游戏各一张同来源单号的单 ⇒ **两张都在**。

        ⚠️ 判据**反过来钉新契约**（游戏范围过滤已按 §13.10 删除）：两张都进 `items`、
        `total=2`（**按 `id` 升序**，与 `§7.2` 的冻结口径一致）。
        ⚠️ **判别力还在**：把游戏范围过滤加回来 ⇒ 本条当场红（会得到 `total=1`）。
        """
        first = self.make_order(order_no="20261007000000000071", source_order_no="TB-SHARED")
        second = self.make_order(
            game=self.other_game, order_no="20261007000000000072",
            source_order_no="TB-SHARED",
        )
        self.assertLess(first.pk, second.pk)  # 构造物：升序那条断言才有方向
        payload = self.assert_ok(self.bearer_get(LIST_PATH, {"source_order_no": "TB-SHARED"}))
        self.assertEqual(payload["total"], 2)
        self.assertEqual(
            [item["order_no"] for item in payload["items"]],
            ["20261007000000000071", "20261007000000000072"],
        )


class SessionRequestsAreRejectedTests(_OpenApiOrderFixture):
    """⭐ T3 交接过来的那条 HTTP 级判据：**真登录会话打对外端点 ⇒ 401**（D13）。

    ⚠️ 两条路径都要覆盖：列表那条同时由 `tests/test_no_route_500.py` 的
    `TOKEN_ONLY_ROUTES` 覆盖；**详情那条不在那张网里**（它带路径转换器 ⇒ 被那张网的
    枚举跳过），所以它的 401 判据**只**由本类钉住。
    """

    def setUp(self):
        super().setUp()
        self.app = self.make_app()
        self.order = self.make_order(order_no="20261007000000000081")

    def test_a_real_login_session_gets_401_and_me_still_gets_200(self):
        from tests.p5_auth_helpers import make_logged_in_client

        client = make_logged_in_client(domain=self.domain_name, username="session-probe")

        # 对照（先证这个会话**真的**是登录态）：同一会话打 `/api/auth/me` ⇒ 200。
        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200, me.content[:200])
        self.assertTrue(json.loads(me.content)["data"]["user"]["username"])

        for path, expected_msg in (
            # 两条路径都只认 Bearer，会话请求回同一句。判据仍逐条钉 msg，不是「401 就行」。
            (LIST_PATH, BEARER_INVALID_CREDENTIALS_MSG),
            (DETAIL_TEMPLATE.format(order_no=self.order.order_no), BEARER_INVALID_CREDENTIALS_MSG),
        ):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 401, response.content[:200])
                body = json.loads(response.content)
                self.assertEqual(body["code"], 401)
                self.assertEqual(body["msg"], expected_msg)


class OpenApiOrderIsolationTests(_OpenApiOrderFixture):
    """跨租户：A 的应用查不到 B 的单；A 的凭据打 B 的域名 ⇒ 401。

    ⚠️ 负向断言一律配**正向对照**（同一次运行里 A 的单**查得到**），否则"查不到"可能由
    "这个端点对谁都不生效"凑出来。判据用**租户内唯一、两租户取值不同**的业务值
    （订单号 / 来源单号），⛔ **不用主键**（两个 schema 的主键序列各自从 1 开始）。
    """

    DOMAIN_B = "openapi-iso-b.test.local"
    SCHEMA_B = "openapi_iso_b"
    #: 两侧的订单号 / 来源单号**取值不同**（同名会让断言失去判别力）。
    ORDER_NO_A = "20261007000000000091"
    ORDER_NO_B = "20261007000000000092"
    SOURCE_NO_A = "TT-SO-A-0001"
    SOURCE_NO_B = "TT-SO-B-0001"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 建：schema DDL 不能回滚在 `TestCase` 的事务里
        # （与 `tests/test_account_isolation.py` 同一手法）。
        cls.tenant_b, _ = provision_tenant(
            schema_name=cls.SCHEMA_B, name="开放平台隔离租户 B", domain=cls.DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行把连接钉回租户 A：跨域名请求结束后连接会停在那个租户的 schema 上
        # （`TenantMainMiddleware` 进请求时 set、响应后不还原——实测）。
        connection.set_tenant(self.tenant)
        super().setUp()
        self.app = self.make_app()
        self.order_a = self.make_order(
            order_no=self.ORDER_NO_A, source_order_no=self.SOURCE_NO_A
        )
        try:
            with tenant_context(self.tenant_b):
                game_b = Game.objects.create(name="B 租户的游戏")
                Order.objects.create(
                    game=game_b,
                    order_no=self.ORDER_NO_B,
                    source_order_no=self.SOURCE_NO_B,
                    project_text="占位项目",
                )
        finally:
            connection.set_tenant(self.tenant)
        # 构造物自证：两侧的单**真的**各自落在自己的 schema 上（否则负断言无意义）。
        self.assertEqual(
            Order.objects.filter(order_no=self.ORDER_NO_A).count(), 1
        )
        with tenant_context(self.tenant_b):
            self.assertEqual(
                Order.objects.filter(order_no=self.ORDER_NO_B).count(), 1
            )
            self.assertEqual(
                Order.objects.filter(order_no=self.ORDER_NO_A).count(), 0
            )
        connection.set_tenant(self.tenant)

    def test_a_app_can_read_its_own_order(self):
        """正向对照：A 的凭据 + A 的域名 ⇒ 查得到 A 的单（否则下面的负断言是空转）。

        ⚠️ 凭据是 **Bearer 令牌**；判据（200 + 载荷里那个订单号）钉在载荷上。
        """
        response = self._token_get_on(
            self.domain_name, DETAIL_TEMPLATE.format(order_no=self.ORDER_NO_A)
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(
            json.loads(response.content)["data"]["order_no"], self.ORDER_NO_A
        )

    def test_a_app_cannot_read_the_order_of_tenant_b(self):
        """A 的凭据查 B 的订单号 / 来源单号 ⇒ 与"不存在"同形、且列表为空。

        两条路径都只认令牌。列表是空集合，详情与「不存在」同形。
        """
        detail = self._token_get_on(
            self.domain_name, DETAIL_TEMPLATE.format(order_no=self.ORDER_NO_B)
        )
        body = json.loads(detail.content)
        self.assertEqual(detail.status_code, 200, detail.content[:300])
        self.assertEqual(body["code"], BUSINESS)
        self.assertIsNone(body["data"])

        listed = self._token_get_on(
            self.domain_name, LIST_PATH, {"source_order_no": self.SOURCE_NO_B}
        )
        listed_body = json.loads(listed.content)
        self.assertEqual(listed_body["code"], OK, listed_body)
        self.assertEqual(listed_body["data"], {"items": [], "total": 0})

    def test_a_credentials_on_the_domain_of_tenant_b_return_401(self):
        """A 的令牌打 B 的域名 ⇒ 401（`ApiApp` 是**每租户一份**的表）。

        ⚠️ 判据实质：B 里没有这把凭据 ⇒ 401 + 同一句不可分辨的文案（`应用凭据无效`）。
        """
        response = self._token_get_on(
            self.DOMAIN_B, LIST_PATH, {"source_order_no": self.SOURCE_NO_A}
        )
        self.assertEqual(response.status_code, 401, response.content[:200])
        body = json.loads(response.content)
        self.assertEqual(body["code"], 401)
        self.assertEqual(body["msg"], BEARER_INVALID_CREDENTIALS_MSG)
        # ⚠️ 反向对照（防"这句话对谁都说"）：同一个令牌打**自己的**域名 ⇒ 200。
        mine = self._token_get_on(
            self.domain_name, LIST_PATH, {"source_order_no": self.SOURCE_NO_A}
        )
        self.assertEqual(mine.status_code, 200, mine.content[:200])
        self.assertEqual(json.loads(mine.content)["code"], OK)

    # ── 工具（本类每个请求后都还原 schema）────────────────────────────
# ⚠️ 本类只有 `_token_get_on` 一种发请求方式（签名面已删除，§13.1）；
# 判据（跨租户 401 / 数据不可见）一条未放宽。

    def _token_get_on(self, domain, path, data=None):
        """按 `domain` 发一条**带 Bearer 令牌**的 GET（两条路径都只认令牌）。

        ⚠️ 令牌当场签发（`tokens.issue`，用本类那把合成凭据）——⛔ 不抄签发逻辑。
        """
        token = tokens.issue(self.app.app_key, self.app.app_secret)
        try:
            return Client(SERVER_NAME=domain).get(
                path,
                data or {},
                HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR=SOURCE_IP,
            )
        finally:
            connection.set_tenant(self.tenant)


class GameScopePrimitiveTests(TenantTestCase):
    """§17.4 抽出的两个原语自身的契约（两侧共用它们，所以形状要钉住）。

    ⚠️ 这一组是**原语的直接判据**，不是端点的：`filter_by_game_ids` 的「空集 ⇒ 空结果」
    与 `assert_game_id_allowed` 吃的两种"允许集合"形状（`Game` 的 QuerySet / id 查询）
    正是应用侧与用户侧各自的入参形状。
    """

    def setUp(self):
        super().setUp()
        self.game_a = Game.objects.create(name="原语游戏甲")
        self.game_b = Game.objects.create(name="原语游戏乙")
        self.order_a = Order.objects.create(
            game=self.game_a, order_no="20261007000000000001", project_text="占位项目"
        )
        self.order_b = Order.objects.create(
            game=self.game_b, order_no="20261007000000000002", project_text="占位项目"
        )

    def test_filter_by_game_ids_narrows_and_empty_means_empty(self):
        kept = filter_by_game_ids(Order.objects.all(), "game_id", [self.game_a.pk])
        self.assertEqual(
            list(kept.values_list("order_no", flat=True)), ["20261007000000000001"]
        )
        # ⚠️ 空集 ⇒ **空结果**（不是"不过滤"）：这条严格口径是业务方选的，且应用侧把它
        # 当作 fail-closed 的一半。
        self.assertEqual(list(filter_by_game_ids(Order.objects.all(), "game_id", [])), [])

    def test_assert_game_id_allowed_accepts_both_allowed_set_shapes(self):
        from apps.business.validation import ValidationProblem

        # 形状一：`Game` 的 QuerySet（用户侧 `games_of(user)` 的形状）。
        assert_game_id_allowed(Game.objects.filter(pk=self.game_a.pk), self.game_a)
        with self.assertRaises(ValidationProblem):
            assert_game_id_allowed(Game.objects.filter(pk=self.game_a.pk), self.game_b)

        # 形状二：id 查询（应用侧 `app.games.values("pk")` 那条调用已删除（§13.1），
            # 但**用户侧**仍然以子查询的形状传进来 ⇒ 这个形状的判据一个字未改，别删）。
        ids = Game.objects.filter(pk=self.game_a.pk).values("pk")
        assert_game_id_allowed(ids, self.game_a)
        with self.assertRaises(ValidationProblem):
            assert_game_id_allowed(ids, self.game_b)

    def test_assert_game_id_allowed_does_not_reject_none(self):
        # 「游戏必填」由既有校验管，原语自己也要吃 `None`（与 `assert_game_allowed` 同口径）。
        assert_game_id_allowed(Game.objects.none(), None)


class _UpdatedSinceHelpers:
    """时间档的两把小工具（**共用**，⛔ 不是断言基类）：打一条请求 + 直接写库的时刻。

    ⚠️ **为什么是一个 mixin 而不是让跨租户那一组继承时间档那一组**：`unittest` 会把
    继承来的 `test_*` **再跑一遍**（同一个用例在子类里以另一个上下文重跑），
    那是"看起来多了一层覆盖、实际只是把同一件事跑两遍"——跨租户组的语义是**另一个**面，
    它只需要这两个工具，⛔ 不需要时间档那一组的其余判据。

    ⚠️ `Order.updated_at` 是 `auto_now=True`（`save()` 改不了它）⇒ 时刻一律用
    `Order.objects.filter(pk=…).update(updated_at=…)` 直接写库，**且每个用例自己写**
    （⛔ 不靠"创建的先后顺序"当时间 —— 同一毫秒内几张单可能完全相同，判据会静默失去方向）。
    """

    def _feed(self, since, **params):
        """打一条时间档请求（ISO 8601 带时区；Django 测试客户端会替我们做 URL 编码）。"""
        query = {"updated_since": since.isoformat()}
        query.update(params)
        return self.bearer_get(LIST_PATH, query)

    def _stamp(self, order, moment):
        """把某张单的 `updated_at` 直接写成 `moment`（绕过 `auto_now`），并**自证**写进去了。

        ⚠️ **`refresh_from_db` 是必须的**（不是保险）：`update()` 是**查询集**写库，
        传进来的那个实例**还带着创建时的旧值**；拿旧值去断言"两张单同刻"会**恒假**
        （实测：两个实例相差 269 微秒 ⇒ 用例以"构造物没成立"的样子红，看起来像顺序判据坏了）。
        """
        Order.objects.filter(pk=order.pk).update(updated_at=moment)
        order.refresh_from_db(fields=["updated_at"])
        self.assertEqual(
            order.updated_at,
            moment,
            "构造物：这个时刻没有真的落到那一行上",
        )
        return order


class UpdatedSinceFeedTests(_UpdatedSinceHelpers, _OpenApiOrderFixture):
    """T5 §7.1：`GET /api/open/v1/orders/?updated_since=…` —— 按时间**增量**拉单。

    ## 这一组证什么

    1. **窗口两端**：`updated_at >= updated_since`（**含端点**）+ 只含**本租户未软删**的单；
    2. **顺序钉死**：`updated_at` 升序 → 同刻按 `id` 升序。⚠️ 只造"时间互不相同"的三张单
       **证不了第二档**（`id` 那一档只在**同刻**时才起作用），所以这里**刻意**让其中两张并列；
    3. **形状**：`data` **恰好**四个键 `{"count","results","page","page_size"}`，且**逐行**与
       **详情端点**取到的同一张单**逐字相同**（判据是"两次响应互相对拍"，
       ⛔ 不是在本文件里再抄一份 17 个键的字面量副本——那会变成第二份字段表，
       而字段表变化时这两处会一起漂、看起来还全绿）；
    4. **分页**：默认 1 / 20、上限 100 夹到 100、越界页回空 `results` + 真实 `count`；
    5. **入参**：无时区 / 非法 / 空串 ⇒ `1000`（带字段名）；与 `source_order_no` 同时给 ⇒ `1000`。

    ⚠️ 每条顺序断言前先 `assertEqual(list(...), [...])` **自证构造物**（那几张单真的带着
    我写进去的时间、且真的按那个次序排），否则"顺序对"可能是碰巧。
    """

    def setUp(self):
        # ⚠️ 夹具自己**不建应用**（见 `_OpenApiOrderFixture` 的 docstring：建不建由各组自己定）
        # ⇒ 本组必须显式建一把，否则每条用例都停在 `bearer_get` 取 `self.app` 那一步。
        super().setUp()
        self.app = self.make_app()

    def test_the_window_is_half_open_at_the_left_edge_and_the_order_is_pinned(self):
        """窗口 `>=` 的**含端点** + 升序 → 同刻按 `id` 升序（三张时间交错的单）。

        ⚠️ 构造物刻意是「早 / **同刻并列两张**」两种：
        - **同刻**那两张的 `id` 一前一后 ⇒ 只有第二档排序生效时它们的次序才被钉住；
        - **早于** `updated_since` 的那张用来证 `>=` **含**端点（改成 `>` 时它会掉出去）；
        - 时间全部由本用例直接写库（⛔ 不靠创建顺序）。
        """
        now = timezone.now()
        older = self.make_order(order_no="20261009000000001001")
        pair_first = self.make_order(order_no="20261009000000001002")
        pair_second = self.make_order(order_no="20261009000000001003")
        since = now - timedelta(minutes=10)
        self._stamp(older, now - timedelta(hours=1))
        self._stamp(pair_first, since)
        self._stamp(pair_second, since)

        payload = self.assert_ok(self._feed(since))

        # ⚠️ 构造物自证：`id` 的先后**与**"并列同刻"这两件事都真的成立（否则第二档排序
        #    根本没被考到，而这条用例看起来还是绿的）。
        self.assertLess(older.pk, pair_first.pk)
        self.assertLess(pair_first.pk, pair_second.pk)
        self.assertEqual(pair_first.updated_at, pair_second.updated_at)
        self.assertLess(older.updated_at, since)

        self.assertEqual(payload["count"], 2, payload)
        self.assertEqual(
            [row["order_no"] for row in payload["results"]],
            ["20261009000000001002", "20261009000000001003"],
        )

    def test_a_soft_deleted_order_is_not_in_the_feed(self):
        """⭐ 只含**未软删**的单（默认 manager，D-0144）—— 配一条正向对照。

        判别力：把取数换成 `Order.all_objects` ⇒ 已软删那张进来、本用例红。
        ⚠️ 对照（同一时刻那张**活的**单必须在）一起断言：否则"整条路恒空"也会让本用例绿。
        """
        now = timezone.now()
        since = now - timedelta(minutes=10)
        kept = self.make_order(order_no="20261009000000001011")
        dropped = self.make_order(order_no="20261009000000001012")
        self._stamp(kept, now)
        self._stamp(dropped, now)
        Order.objects.filter(pk=dropped.pk).update(deleted_at=now)

        payload = self.assert_ok(self._feed(since))
        self.assertEqual(
            [row["order_no"] for row in payload["results"]],
            ["20261009000000001011"],
        )
        self.assertEqual(payload["count"], 1, payload)

    def test_the_data_has_exactly_the_four_pagination_keys(self):
        """`data` **恰好**四个键（多的少的都红）—— 本仓对外分页口径（§7.1 / §6.4）。"""
        now = timezone.now()
        self._stamp(self.make_order(order_no="20261009000000001021"), now)
        payload = self.assert_ok(self._feed(now - timedelta(minutes=10)))
        self.assertEqual(set(payload), {"count", "results", "page", "page_size"})

    def test_every_row_is_byte_for_byte_the_detail_payload(self):
        """⭐ **同一张单**在时间档与详情端点上的载荷**逐字相同**（D13）。

        ⚠️ **判据形态本身就是要点**：这是**两次真实响应互相对拍**（⛔ 不是在本文件里再抄
        一份 17 个键的字面量副本）。⛔ 复制的字段表会在"实现改了、副本没改"时**假绿**，
        而这条对拍不会 —— 详情端点那条路是**已经被别的用例钉住的**既有出口。
        """
        now = timezone.now()
        order = self.make_order(
            order_no="20261009000000001031", source_order_no="TT-FEED-0001"
        )
        self._stamp(order, now)

        feed_row = self.assert_ok(self._feed(now - timedelta(minutes=10)))["results"][0]
        detail = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=order.order_no))
        )
        # ⚠️ 自证"对拍的两侧不是空 / 不是同一份东西"：订单号这一条。
        self.assertEqual(order.order_no, "20261009000000001031")
        self.assertTrue(feed_row["order_no"])
        self.assertEqual(feed_row, detail)

    def test_page_size_is_clamped_to_one_hundred_and_pages_slide(self):
        """`page_size` 超 100 ⇒ **夹到 100**（判据打在**返回里的** `page_size` 上）+ 第二页。

        ⚠️ 只造 3 张单：这里要证的是**夹到上限这个动作**（`page_size=1000` 回 100）与翻页的
        窗口（第 2 页拿到第 3 张、第 3 页空而 `count` 不变），⛔ 不是造 100 张单去证上限本身
        （上限的数字由"夹到 100"这条一起钉住）。
        """
        now = timezone.now()
        since = now - timedelta(minutes=10)
        first = self.make_order(order_no="20261009000000001041")
        second = self.make_order(order_no="20261009000000001042")
        third = self.make_order(order_no="20261009000000001043")
        for index, order in enumerate((first, second, third)):
            self._stamp(order, since + timedelta(seconds=index))

        clamped = self.assert_ok(self._feed(since, page_size=1000))
        self.assertEqual(clamped["page_size"], 100, clamped)
        self.assertNotEqual(clamped["page_size"], 1000, clamped)
        self.assertEqual(clamped["count"], 3, clamped)
        # 对照：没超上限时**照原样回**，并且窗口真的按它切（判据=返回里的 page_size + 行数）。
        window = self.assert_ok(self._feed(since, page=1, page_size=2))
        self.assertEqual(window["page_size"], 2, window)
        self.assertEqual(len(window["results"]), 2, window)
        self.assertEqual(window["page"], 1, window)
        # ⭐ `count` = **命中总数**（⛔ 不是本页条数）：翻到第 2 页时它**不变**。
        page_two = self.assert_ok(self._feed(since, page=2, page_size=2))
        self.assertEqual(page_two["count"], 3, page_two)
        self.assertEqual(
            [row["order_no"] for row in page_two["results"]],
            ["20261009000000001043"],
        )
        # 越界页 ⇒ **空 results + 真实 count**（⛔ 不报错、也不回 1002）。
        page_three = self.assert_ok(self._feed(since, page=3, page_size=2))
        self.assertEqual(page_three["results"], [], page_three)
        self.assertEqual(page_three["count"], 3, page_three)
        self.assertEqual(page_three["page"], 3, page_three)

    def test_a_bad_page_or_page_size_is_a_parameter_error(self):
        """畸形分页入参 ⇒ `1000`（⛔ 不是 500、也不是"静默按默认值处理"）。

        ⚠️ 与对外那三个列表端点**同一套**校验（两处调的是同一个 `page_window`）⇒ 判据相同是
        **有意**的：口径漂成两套时，这里会先红。
        """
        now = timezone.now()
        since = (now - timedelta(minutes=10)).isoformat()
        for query in (
            {"page": "abc"},
            {"page": 0},
            {"page": -1},
            {"page_size": "abc"},
            {"page_size": 0},
            {"page_size": -5},
        ):
            with self.subTest(query=query):
                self.assert_business_failure(
                    self.bearer_get(LIST_PATH, {"updated_since": since, **query}),
                    VALIDATION,
                )

    def test_an_updated_since_without_a_timezone_is_a_parameter_error(self):
        """无时区 / 非法格式 / 空串 ⇒ `1000`，且 `msg` **带字段名**（D14 / §7.1）。

        ⭐ **判别力最强的是无时区那一条**：DRF 的 `DateTimeField` 在 `USE_TZ=True` 下会把
        `2026-10-09T10:00:00` **静默按当前时区补上时区收下**（见 `UpdatedSinceField` 的
        docstring），于是"调用方按 UTC 递的游标"被当成 `Asia/Shanghai` —— 窗口**静默错位**，
        症状是第三方少拿到单，而我们这边一片绿。⛔ 只靠 DRF 默认行为过不了这条。
        ⚠️ 每条都断言 `msg` 里**有字段名**（`updated_since`）：本仓自有文案一律带字段名
        （`AGENTS.md` §5-G 的判别口诀），不带名字的英文默认文案是第三方表单库的内置消息。
        ⚠️ **空串那一档钉的就是"空串走哪一档"**：它是"格式非法"（回 `1000`），
        ⛔ **不是**"没传"（那会静默切到单号档、再被判成缺参数 —— 换个档位、换个语义）。
        """
        for raw in ("2026-10-09T10:00:00", "abc", ""):
            with self.subTest(updated_since=raw):
                body = self.assert_business_failure(
                    self.bearer_get(LIST_PATH, {"updated_since": raw}), VALIDATION
                )
                self.assertIn("updated_since", body["msg"], body)

    def test_both_filters_together_are_rejected(self):
        """`source_order_no` 与 `updated_since` **同时给** ⇒ `1000`（D11 的二选一）。

        ⚠️ 判据是**那句二选一的文案**（⛔ 不是"只要 code 是 1000 就算"）：只判码的话，
        「两个都传」与「两个都没传」这两个**语义相反**的档会被同一条断言放过，
        而调用方拿到"缺少参数"会去**补**一个参数、越补越错。
        """
        now = timezone.now()
        self.make_order(order_no="20261009000000001051", source_order_no="TT-BOTH-0001")
        body = self.assert_business_failure(
            self.bearer_get(
                LIST_PATH,
                {
                    "source_order_no": "TT-BOTH-0001",
                    "updated_since": now.isoformat(),
                },
            ),
            VALIDATION,
        )
        self.assertEqual(
            body["msg"], "参数 source_order_no 与 updated_since 只能选一个"
        )

    def test_the_source_order_no_branch_ignores_the_pagination_parameters(self):
        """⭐ 对照（回归）：**不给 `updated_since` 时，分页参数一律不看**（§7.1 逐字）。

        判别力（两条一起才站得住）：
        ① 传了**畸形**的 `page` / `page_size` 却**不报错** —— 反过来，把单号档也接进分页
           校验会让这条红（`page=abc` 会变成 `1000`）；
        ② 返回值仍是旧形状 `{"items", "total"}` 且**没有** `page` / `page_size` 两个键
           —— 反过来，把单号档也套上分页载荷会让第二条红。
        ⚠️ 上限也**不是** `page_size`：单号档仍是 `SOURCE_ORDER_NO_LIMIT`（那条由
        `test_more_than_the_limit_reports_the_full_total` 钉着，⛔ 本用例不重复钉）。
        """
        self.make_order(order_no="20261009000000001061", source_order_no="TT-NOPG-0001")
        payload = self.assert_ok(
            self.bearer_get(
                LIST_PATH,
                {"source_order_no": "TT-NOPG-0001", "page": "abc", "page_size": 0},
            )
        )
        self.assertEqual(set(payload), {"items", "total"})
        self.assertEqual(payload["total"], 1, payload)
        self.assertEqual(
            [row["order_no"] for row in payload["items"]],
            ["20261009000000001061"],
        )

    def test_a_direct_update_does_not_move_updated_at_so_the_feed_misses_it(self):
        """⛔ **已知边界**（红队抓到，本线**只登记不修**）：绕过 `save()` 的直接 `.update()` **不动** `updated_at`。

        ## 症状（就是本用例钉住的那件事）

        `Order.updated_at` 是 `auto_now=True` —— 那个时间戳**只随 `save()` 走**。
        全仓有不少**绕过 `save()`** 的直接写入点（`QuerySet.update(...)`），
        例如 `apps/business/jidanpu_upstream.py:2317`、`apps/business/refunds.py:315`、
        `apps/business/refunds.py:425`（⚠️ 行号是**写这条用例时**取的，别当权威，按 grep 重取）
        ⇒ **经那些链路发生的改动不会出现在增量拉单里**：第三方拿"上一批最大 `updated_at`"
        当游标下一轮接着拉，会**永远看不到**这些改动（不是延迟，是**漏**）。

        ## ⛔ 为什么本线不修

        `updated_at` 的语义（"最后修改时间"到底包不包括那几条链路）是**别的线**的事，
        而改成"每条写入点都顺手写 `updated_at`"会牵动钱与提成那两条链路的写路径
        （`AGENTS.md` §5-G 的升级规则：碰到贵东西要停下报告，⛔ 不许就地改）。
        ⇒ 本线把它落成一条**能失败的判据**：将来谁去收口这项语义，改完这里会**先红**，
        那时把它翻成"应当拉得到"或"明确登记进文档的已知边界"都是**有意识**的选择。

        ## 判别力（两个断言一起才站得住）

        ① 直接 `.update()` 之后 `updated_at` **确实没动** —— 少了这条，"拉不到"可能是因为
           时间戳移动到了别处，症状就换了形状；
        ② 一个**早于**那次 `.update()` 的 `updated_since` ⇒ `count == 0`。
        ⚠️ 佐证（不是断言）：同一张单用**详情端点**（另一条读路径）查得得到 —— 说明"拉不到"
        不是"这张单不在本租户"或"它被软删了"。
        """
        now = timezone.now()
        order = self.make_order(order_no="20261009000000001081")
        self._stamp(order, now)
        # 游标刻意取在**这张单的时间戳之后**：于是"拉不到"才可能是"它不在窗口里"以外的原因
        # （下面那条 `save_time < cursor` 是构造物自证，⛔ 不是判据本身）。
        cursor = now + timedelta(minutes=10)
        save_time = order.updated_at

        # ⛔ 绕过 `save()`：直接 `.update()` —— `auto_now` **不会**被触发（正是要钉的那件事）。
        Order.objects.filter(pk=order.pk).update(remark="红队探针：绕过 save 的写入")
        order.refresh_from_db(fields=["updated_at", "remark"])

        # ① 构造物自证：这次 `.update()` 真的写了库（`remark` 变了）**而时间戳一动不动**。
        self.assertEqual(order.remark, "红队探针：绕过 save 的写入")
        self.assertLess(save_time, cursor)
        self.assertEqual(order.updated_at, save_time)

        payload = self.assert_ok(self._feed(cursor))
        self.assertEqual(payload["count"], 0, payload)
        self.assertEqual(payload["results"], [], payload)

        # 佐证：**同一张单**在详情端点（另一条读路径）上查得得到 ⇒ "拉不到"不是
        # "它不在本租户"、也不是"它被软删了"（两条读路径的取数面不同，这里只取佐证）。
        detail = self.assert_ok(
            self.bearer_get(
                DETAIL_TEMPLATE.format(order_no="20261009000000001081")
            )
        )
        self.assertEqual(detail["order_no"], "20261009000000001081")


class UpdatedSinceIsolationTests(_UpdatedSinceHelpers, _OpenApiOrderFixture):
    """跨租户（T5 §10.1-21）：A 的令牌用 `updated_since` **拉不到** B 的单。

    ⚠️ 判据用**租户内唯一、且两租户取值不同**的业务值（订单号），⛔ **不用主键**：
    两个 schema 的主键序列各自从 1 开始 ⇒ 拿主键指认"另一个租户那一行"会**恒真或恒假**
    （`AGENTS.md` §5-G 的既有踩坑）。
    ⚠️ 这里那套 setUp 与 `OpenApiOrderIsolationTests` 是**同一个形状的平行实现**（连接钉回
    租户 A + 造第二个租户的单）——⛔ 不共用、也不改动那个类：它的判据（401 / 查不到）
    一条都不该被本线碰。
    """

    DOMAIN_B = "openapi-feed-iso-b.test.local"
    SCHEMA_B = "openapi_feed_iso_b"
    #: 两侧订单号**取值不同**（同名会让断言失去判别力）。
    ORDER_NO_A = "20261009000000001071"
    ORDER_NO_B = "20261009000000001072"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=cls.SCHEMA_B, name="增量拉单隔离租户 B", domain=cls.DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行把连接钉回租户 A：跨域名请求结束后连接会停在那个租户的 schema 上
        # （`TenantMainMiddleware` 进请求时 set、响应后不还原——实测）。
        connection.set_tenant(self.tenant)
        super().setUp()
        self.app = self.make_app()
        self.since = timezone.now() - timedelta(minutes=10)
        # 两侧的单**都**在窗口内（`updated_at` 取同一个时刻）：这样"拉不到 B"就**不能**用
        # "它太旧了"解释，只能由租户边界解释。
        self.order_a = self.make_order(order_no=self.ORDER_NO_A)
        self._stamp(self.order_a, self.since + timedelta(seconds=1))
        try:
            with tenant_context(self.tenant_b):
                game_b = Game.objects.create(name="B 租户的游戏")
                order_b = Order.objects.create(
                    game=game_b,
                    order_no=self.ORDER_NO_B,
                    project_text="占位项目",
                )
                Order.objects.filter(pk=order_b.pk).update(
                    updated_at=self.since + timedelta(seconds=1)
                )
        finally:
            connection.set_tenant(self.tenant)
        # 构造物自证：两侧的单**真的**各自落在自己的 schema 上（否则负断言无意义）。
        self.assertEqual(Order.objects.filter(order_no=self.ORDER_NO_A).count(), 1)
        with tenant_context(self.tenant_b):
            self.assertEqual(Order.objects.filter(order_no=self.ORDER_NO_B).count(), 1)
            self.assertEqual(Order.objects.filter(order_no=self.ORDER_NO_A).count(), 0)
        connection.set_tenant(self.tenant)

    def test_a_feed_returns_only_tenant_a_orders(self):
        """A 的令牌拉增量 ⇒ **只有 A 的单**（B 的那张既不进 `results`、也不进 `count`）。

        ⚠️ 正向对照（A 自己那张**必须在**）与负向断言同一次断言：否则"整条路恒空"也会绿。
        """
        payload = self.assert_ok(self._feed(self.since))
        self.assertEqual(
            [row["order_no"] for row in payload["results"]], [self.ORDER_NO_A]
        )
        self.assertEqual(payload["count"], 1, payload)


class HiddenOrderExclusionTests(_OpenApiOrderFixture):
    """⭐ 评审 Important 4：对外查单**不得看到**镜像卡派生的隐藏单（详情 / 来源号 / 增量）。

    判据（每条都配正对照，避免「恒空」假绿）：
    - 详情按单号 ⇒ `1002`（与「不存在」同形）；
    - `?source_order_no=` ⇒ 查不到它（同一次请求里另一张真单查得到）；
    - `?updated_since=` ⇒ 它不在增量流里（同一窗口里另一张真单在）。
    """

    def setUp(self):
        super().setUp()
        from tests.hall_shadow_helpers import make_hidden_order, make_mirror

        self.app = self.make_app()
        self.visible = self.make_order(
            order_no="20261026000000000041", source_order_no="SYN-OPEN-VISIBLE"
        )
        self.card = make_mirror(
            game=self.game_a, bound_by=User.objects.first(), no="SYN-OPEN-HIDDEN"
        )
        self.hidden = make_hidden_order(
            game=self.game_a, card=self.card, order_no="20261026000000000042"
        )

    def test_the_detail_of_a_hidden_order_is_1002(self):
        response = self.bearer_get(
            DETAIL_TEMPLATE.format(order_no=self.hidden.order_no)
        )
        self.assert_business_failure(response, BUSINESS)
        # 正对照：同一应用查那张真单是 200 + code=0。
        payload = self.assert_ok(
            self.bearer_get(DETAIL_TEMPLATE.format(order_no=self.visible.order_no))
        )
        self.assertEqual(payload["order_no"], self.visible.order_no)

    def test_the_source_order_query_does_not_find_it(self):
        payload = self.assert_ok(
            self.bearer_get(LIST_PATH, {"source_order_no": "SYN-OPEN-HIDDEN"})
        )
        self.assertEqual(payload, {"items": [], "total": 0})
        kept = self.assert_ok(
            self.bearer_get(LIST_PATH, {"source_order_no": "SYN-OPEN-VISIBLE"})
        )
        self.assertEqual(
            [item["order_no"] for item in kept["items"]], [self.visible.order_no]
        )

    def test_the_updated_since_feed_does_not_carry_it(self):
        now = timezone.now()
        Order.objects.filter(pk__in=[self.visible.pk, self.hidden.pk]).update(
            updated_at=now
        )
        payload = self.assert_ok(
            self.bearer_get(LIST_PATH, {"updated_since": now.isoformat()})
        )
        nos = [row["order_no"] for row in payload["results"]]
        self.assertIn(self.visible.order_no, nos)          # 正对照
        self.assertNotIn(self.hidden.order_no, nos)
        self.assertEqual(payload["count"], 1)
