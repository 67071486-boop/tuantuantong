"""T6：打手侧**对外**端点第二批之一 —— **我的单 / 查单**（`GET /api/taker/v1/my/orders/`
与 `GET /api/taker/v1/orders/<order_no>/`）。

spec：`docs/specs/2026-10-08-taker-open-api-design.md` §5 端点表第 3、4 行 · §5.2（行 = §5.1 ＋ 7 键）·
§5.3-2（⭐ 红队 2026-10-08 改准：判据 = **「他是当前接手人」**）· §5.4（失败档）·
§9 的 1（四路对照）/ 8（P2 字段）/ 14（孤儿订单）。

## 本文件证什么（逐条都能失败）

| # | 判据 | spec |
|---|---|---|
| 1 | ⭐⭐ **「他是当前接手人」不是「游戏范围内」**：造一张**在他游戏权限范围内、但被别人接手**的单 ⇒ 查它 `1002`（⛔ 这一条**只靠游戏范围实现就必红**） | §5.3-2 / §9-2 |
| 2 | ⭐ **范围判据四路对照**（含①大厅那一半的**核对**）：对**别人的单** ①大厅列表里没有它 ②查它 `1002` ④我的单里没有它；⭐ 并配**对照**：对**他自己的单**同样四条都成功 | §9-1 |
| 3 | ⭐ **孤儿订单**（一条 MAIN 绩效行都没有的单）⇒ `1002`，⛔ **不是 500**、也⛔ 不是"空成功" | §9-14 |
| 4 | ⭐ **软删的单**（`deleted_at` 非空）⇒ 查它 `1002`、且不进「我的单」（⛔ 不是只写在列表那一条上） | §5.3-2 末句 |
| 5 | ⭐ **P2 字段 = 恰好 18 个键**（字面量写死在这里，⛔ 不从实现派生）＋ 显式枚举的"不许出现的键"一个都不在 ＋ **有** `customer_phone` / `customer_ww` | §5.2 / §9-8 |
| 6 | ⭐ **列表与查单逐键同形**（同一份装配出口 ⇒ 两处形状不会漂移） | §5.2 |
| 7 | **`updated_since`**：两张单先后更新 ⇒ 只回更新的那张；非法值 ⇒ `1000`（⛔ 不是 500、也⛔ 不是静默不过滤） | §5 端点表第 3 行 |
| 8 | **分页**：`page` / `page_size` 非法 ⇒ `1000`；`page_size` 超 100 ⇒ 夹到 100；`count` 是命中总数 | §5（统一口径） |
| 9 | **鉴权面**：无令牌 / 上家侧令牌 / 会话 ⇒ `401`；⭐ 对照组（有合法令牌 ⇒ 200） | §9-2 |
| 10 | ⭐ **装配**（红队 §9-15）：两个视图类的 `authentication_classes` / `permission_classes` / `throttle_classes` **恰好**那三件 | §9-15 |
| 11 | **畸形路径段**：订单号里带 NUL ⇒ **404**（⛔ 不是 500）——带转换器的路由不在 `test_no_route_500` 那条腿的覆盖内，所以必须在这里补 HTTP 级判据 | `test_no_route_500` 的已知盲区 |

## ⚠️ 证明力边界（如实写清楚）

- 令牌**一律从 `/api/taker/oauth/token` 换**（走真路由 + 真 `Client`，⛔ 不手搓夹具）；
- 第 1 条（本任务最重要的判据）**不靠"范围外"取证**：它造的单**在**他的游戏权限内，
  只把 MAIN 绩效行的归属人换成**别人** ⇒ 只有"他是接手人"那份判据能拦住它，
  "游戏范围内"那份判据会**放行**（把 `order_scope.claimed_order_of` 换成 T5 那套
  `scope_queryset` + `game_scope` ⇒ 本用例**必红**，读数见报告）；
- 夹具一律**合成值**（`AGENTS.md` §5-C），来源 IP 取文档保留段（⛔ 不是任何真实客户端）。
"""
import json
import pathlib
import tempfile
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderScreenshot,
    OrderStatus,
    Performance,
)
from apps.business.order_writes import WriteContext, assign_order
from apps.core.storage import get_storage
from apps.openapi.models import ApiApp
from apps.takerapi.models import TakerApp
from tests.p5_auth_helpers import grant_nodes, make_logged_in_client

User = get_user_model()

# ── 路径（与 `apps/takerapi/urls.py` 逐字一致；⚠️ 都**带尾斜杠**）────────────────
TOKEN_PATH = "/api/taker/oauth/token"
MY_ORDERS_PATH = "/api/taker/v1/my/orders/"
#: 查单那条**带路径转换器** ⇒ 它是 `tests/test_no_route_500.py` 显式路由腿的**已知盲区**
#: （本文件用 HTTP 级用例补，见 `MyOrdersMalformedPathTests`）。
ORDER_DETAIL_TEMPLATE = "/api/taker/v1/orders/{order_no}/"
HALL_PATH = "/api/taker/v1/hall/"
OPENAPI_ORDERS_PATH = "/api/open/v1/orders/"
#: ⚠️ 下面两条**不是**本端点（它们属另两个模块）——`ReassignmentInvalidatesTheOldTakerTests`
#: 要用**同一条凭据**打四条端点，所以路径字面量也放在这里（⛔ 不从实现 import：
#: 路径是本线对外契约的一部分，`E-0024`）。
STATUS_TEMPLATE = "/api/taker/v1/orders/{order_no}/status/"
IMAGES_TEMPLATE = "/api/taker/v1/orders/{order_no}/images/"
TICKET_TEMPLATE = "/api/taker/v1/orders/{order_no}/images/ticket/"

#: 一个**真的** JPEG 魔数开头的最小字节串（认领那一跳按**魔数**判类型，假开头会被拒）。
PROGRESS_JPEG = b"\xff\xd8\xff" + b"syn-reassign-progress-image"

GRANT_TYPE = "client_credentials"

# ── 合成凭据（⛔ 与任何真实租户的 appKey / secret 无关）────────────────────────
TAKER_A_KEY = "takermyorders00001"
TAKER_A_SECRET = "taker-myorders-secret-0000001"
TAKER_B_KEY = "takermyorders00002"
TAKER_B_SECRET = "taker-myorders-secret-0000002"
OPENAPI_KEY = "openmyorders000001"
OPENAPI_SECRET = "open-myorders-secret-00001"

#: 文档保留段 `203.0.113.0/24` 的地址（⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.71"

# ── 业务码 / HTTP 码字面值（`E-0024`：写数字，让"码值被改掉"在用例里可见）──────
OK = 0
VALIDATION = 1000
BUSINESS = 1002
UNAUTHENTICATED = 401
NOT_FOUND = 404

#: 该账号持的节点：⭐ 只够"看"（本文件不测改状态，那条在
#: `tests/test_takerapi_order_status_api.py`）——`my_orders.send_to_my_system` 是内部那个
#: 「我接的单」页的节点；对外这两条端点**不装权限类**（`permission_classes = []`），
#: 所以它其实用不上。⚠️ 留着它是有意的：**证明"节点不是对外端点的门"**
#: （撤掉它本文件的用例照样全绿 ⇒ 对外面的授权面是"绑定账号 + 接手人"，不是节点）。
MY_ORDERS_NODE = "my_orders.send_to_my_system"
#: 大厅要的节点（第 2 条判据的 ① 那一半要用它拉大厅）。
TAKE_ORDER_NODE = "grab_hall.take_order"

# ── ⭐ §5.2 的行白名单：**字面量**写在这里（⛔ 不从 `my_orders_views` / `hall_views`
#    的任何常量派生——派生是自指恒真：被断言的集合跟着被测的常量一起变，`E-0024`）──
#: §5.1 那 11 个键（与 `tests/test_takerapi_hall_api.py` 的 `HALL_ROW_KEYS` **同一份**
#: 字面量——那边证"大厅行恰好这 10 个"，这边证"我的单行 = 这 11 个 + 7 个"）。
HALL_ROW_KEYS = {
    "order_no",
    "source_order_no",
    "game",
    "game_name",
    "server",
    "project_text",
    "content",
    "time_requirement",
    "is_urgent",
    "hall_price",
    "created_at",
}
#: §5.2 **加**的那 7 个。
MY_ORDER_EXTRA_KEYS = {
    "status",
    "status_text",
    "claimed_at",
    "updated_at",
    "customer_phone",
    "customer_ww",
    "total_price",
}
#: ⭐ 恰好 18 个（spec §5.2 的算式：10 + 7）。
MY_ORDER_ROW_KEYS = HALL_ROW_KEYS | MY_ORDER_EXTRA_KEYS

#: ⛔ 一行「我的单」里**绝不许**出现的键（`E-0065`：拦截必须**显式枚举**，
#: ⛔ 不用"键名里含 customer"这类模糊判据）。四类来源逐条对应 §5.1 / §5.2 的"不给"：
#: ① 员工身份 / 我方内部痕迹 —— ⚠️ 注意 `remark` 是**我方员工写的**那一列，
#:    客户联系方式是 `customer_*`，两者**不是**一回事（§5.2 给的正是后者）；
#: ② 财务；③ 提成 / 成本；④ 镜像那一路。
FORBIDDEN_ROW_KEYS = {
    # ① 员工身份 / 我方内部痕迹
    "remark",
    "demand",
    "sales_agent",
    "created_by",
    "updated_by",
    "hall_publisher",
    "hall_publisher_name",
    "game_account",
    "game_role_name",
    # ② 财务
    "net_received",
    "refund_fee",
    "received_price",
    "platform_price",
    "fee",
    "payment_method",
    "payment_status",
    # ③ 提成 / 成本（§5.2 末句点名的"成本 / 提成"）
    "bonus_amount",
    "temp_amount",
    "commission_rate",
    "costs",
    "taker_price",
    # ④ 镜像那一路（本端点根本不经过那条路；写出来是为了钉住"没走合并读口"）
    "origin",
    "source_system",
    "remote_order_id",
    "remote_game_area",
    "mirror_pk",
}

# ── 幂等键契约的两端（与 `apps/openapi/idempotency.py` 的对外契约同值）─────────
MIN_KEY = 8
MAX_KEY = 128


class _MyOrdersFixture(TenantTestCase):
    """两个打手（各持**不同**游戏权限）+ 若干张单 + 两套凭据。

    ⚠️ 甲对**两款游戏都有权限**是有意的：第 1 条判据要造的那张"范围内但被别人接手"的单
    必须落在他有权限的游戏里——否则用例会以"范围外"这个**另一个原因**变绿（`E-0004` 形态）。
    """

    def setUp(self):
        super().setUp()
        self.domain_name = self.domain.domain
        self.anonymous = Client(SERVER_NAME=self.domain_name)
        self.game_a = Game.objects.create(name="我的单测试游戏甲")
        self.game_b = Game.objects.create(name="我的单测试游戏乙")

        #: 甲：**两款游戏都有权限**（见类 docstring）；乙：同样两款（当第二个视角）。
        self.taker_a = self._make_taker("my-orders-taker-a", games=[self.game_a, self.game_b])
        self.taker_b = self._make_taker("my-orders-taker-b", games=[self.game_a, self.game_b])
        self.app_a = self._make_app(
            "我的单测试应用甲", TAKER_A_KEY, TAKER_A_SECRET, self.taker_a
        )
        self.app_b = self._make_app(
            "我的单测试应用乙", TAKER_B_KEY, TAKER_B_SECRET, self.taker_b
        )
        self.token_a = self.issue_token(self.app_a)
        self.token_b = self.issue_token(self.app_b)

        #: ⭐ 对照：**他自己接手**的一张进行中的单（四条都应当成功）。
        self.order_mine = self._order(
            self.game_a,
            "20261008000000000301",
            status=OrderStatus.IN_PROGRESS,
            taker=self.taker_a,
            price="88.00",
            project_text="测试项目文本甲",
            customer_phone="19900000001",
            customer_ww="测试旺旺甲",
        )
        #: ⭐⭐ **本任务最重要的那张单**：在**甲的**游戏权限范围内，但 MAIN 绩效行归**乙**
        #: ——只有"他是当前接手人"那份判据拦得住它（"游戏范围内"会**放行**）。
        self.order_others = self._order(
            self.game_a,
            "20261008000000000302",
            status=OrderStatus.IN_PROGRESS,
            taker=self.taker_b,
            price="66.00",
        )
        #: 大厅里的一张（甲有权限、**没人接**）——第 2 条判据的 ① 那一半要用它当**正对照**。
        self.order_hall = self._order(
            self.game_a, "20261008000000000303", price="55.00"
        )

    # ── 造物 ───────────────────────────────────────────────────────────
    def _make_taker(self, username, *, games):
        """普通账号 + 显式游戏权限 + 那一个节点（理由见 `MY_ORDERS_NODE` 的注释）。"""
        user = User.objects.create_user(username=username, password="pw-12345678")
        group, _ = Group.objects.get_or_create(name="代练员")
        user.groups.add(group)
        StaffProfile.objects.create(user=user).games.set(games)
        grant_nodes(user, MY_ORDERS_NODE, TAKE_ORDER_NODE)
        return user

    def _make_app(self, name, app_key, app_secret, taker):
        return TakerApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret=app_secret,
            taker=taker,
            created_by=taker,
        )

    def _order(
        self, game, order_no, *, status=OrderStatus.WAITING_GRAB, taker=None,
        price="88.00", project_text="", customer_phone="", customer_ww="",
        with_main_performance=True, deleted_at=None,
    ):
        """建「订单 + 一条 MAIN 绩效行」（`with_main_performance=False` ⇒ **孤儿订单**）。

        ⚠️ 绩效行是**必需的**（除了孤儿那一条判据）：`claim.py` 与内部「我接的单」的
        入选条件都以它为前提；少了它，"这张单不出现"会以**与被测行为无关的原因**变绿。
        """
        order = Order.objects.create(
            game=game,
            order_no=order_no,
            status=status,
            hall_price=None if price is None else Decimal(price),
            project_text=project_text,
            customer_phone=customer_phone,
            customer_ww=customer_ww,
            deleted_at=deleted_at,
        )
        if with_main_performance:
            Performance.objects.create(
                order=order,
                user=taker,
                emp_type=EmpType.MAIN,
                temp_amount=Decimal(price) if price is not None else Decimal("0.00"),
                claimed_at=timezone.now() if taker is not None else None,
            )
        return order

    # ── 令牌：**只从取令牌端点拿**（spec §9-2 的要求）───────────────────
    def issue_token(self, app, *, path=TOKEN_PATH) -> str:
        response = self.anonymous.post(
            path,
            {
                "grant_type": GRANT_TYPE,
                "client_id": app.app_key,
                "client_secret": app.app_secret,
            },
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = json.loads(response.content)
        self.assertEqual(set(body), {"access_token", "token_type", "expires_in"})
        return body["access_token"]

    def make_api_app(self):
        """一行**上家侧**应用（跨凭据类型那一半要用它换一个上家侧令牌）。"""
        return ApiApp.objects.create(
            name="我的单测试上家应用甲",
            app_key=OPENAPI_KEY,
            app_secret=OPENAPI_SECRET,
            created_by=self.taker_a,
        )

    # ── 发请求 ─────────────────────────────────────────────────────────
    def my_orders(self, token=None, params=None, *, remote_addr=SOURCE_IP):
        return self.anonymous.get(
            MY_ORDERS_PATH,
            params or {},
            HTTP_AUTHORIZATION=f"Bearer {token or self.token_a}",
            REMOTE_ADDR=remote_addr,
        )

    def order_detail(self, order_no, *, token=None, remote_addr=SOURCE_IP):
        return self.anonymous.get(
            ORDER_DETAIL_TEMPLATE.format(order_no=order_no),
            HTTP_AUTHORIZATION=f"Bearer {token or self.token_a}",
            REMOTE_ADDR=remote_addr,
        )

    def hall(self, token=None):
        return self.anonymous.get(
            HALL_PATH,
            {},
            HTTP_AUTHORIZATION=f"Bearer {token or self.token_a}",
            REMOTE_ADDR=SOURCE_IP,
        )

    # ── 断言小工具 ─────────────────────────────────────────────────────
    @staticmethod
    def body(response):
        return json.loads(response.content)

    def assert_business_failure(self, response, code):
        """业务失败：**HTTP 200** + 指定业务码（本仓口径：状态码只表达传输 / 认证层）。"""
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], code, payload)
        self.assertIsNone(payload["data"], payload)
        return payload

    def rows_of(self, token=None, params=None):
        """「我的单」的行（同时把"成功"这件事钉住：HTTP 200 + `code=0`）。"""
        response = self.my_orders(token, params)
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], OK, payload)
        return payload["data"]["results"]

    def nos_of(self, token=None, params=None):
        return sorted(row["order_no"] for row in self.rows_of(token, params))

    def detail_of(self, order_no, *, token=None):
        response = self.order_detail(order_no, token=token)
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], OK, payload)
        return payload["data"]


class ScopeIsTheCurrentTakerNotTheGameScopeTests(_MyOrdersFixture):
    """⭐⭐ **本任务最重要的判据**：范围 = **「他是这张单的当前接手人」**，⛔ 不是"游戏范围内"。

    红队 2026-10-08 的预防性警告：T5 的取单口（`order_write_views._live_order_in_scope`）
    用的是**游戏范围**——那对**大厅**是对的（大厅本来就是"他游戏范围内、没人接的单"），
    但拿它当查单 / 改状态 / 进度图 / 我的单的授权判据，会让**"范围内、别人接的单"被查到**。

    ## 判别力（这条用例**专门**钉那件事）

    夹具 `order_others` **在**甲的游戏权限范围内（`game_a`，甲有权限），只把 MAIN 绩效行的
    归属人换成**乙**。⇒ 只有"他是接手人"那份判据拦得住它；
    **把 `order_scope.claimed_order_of` 换成 T5 那套 `scope_queryset` + `game_scope("game_id")`
    ⇒ 本类**必红**（读数见 T6 报告）。
    """

    def test_an_order_in_his_game_scope_but_claimed_by_someone_else_is_1002(self):
        """⭐ 查单：**范围内、别人接手**的单 ⇒ `1002`（⛔ 不是 200）。"""
        payload = self.assert_business_failure(self.order_detail(self.order_others.order_no), BUSINESS)
        # ⚠️ 文案与"不存在"**同形**（spec §5.3-2）：两种成因的 msg 必须逐字相同。
        self.assertEqual(payload["msg"], self.body(self.order_detail("20261008000000009999"))["msg"])

    def test_that_order_does_not_appear_in_his_my_orders(self):
        """⭐ 我的单：那张单**不出现**（而他自己那张**在**——⛔ 只看"不在"在恒空上照样绿）。"""
        nos = self.nos_of()
        self.assertNotIn(self.order_others.order_no, nos)
        self.assertIn(self.order_mine.order_no, nos)

    def test_the_order_really_is_visible_to_its_actual_taker(self):
        """⭐ **第二个视角**：同一张单在**乙**（真的接手人）那里**看得到**、逐键成功。

        判别力：若那张单其实**没有 MAIN 绩效行**（夹具坏了），上面两条会以"与被测行为无关的
        原因"变绿；本用例把那个可能性排除掉，并顺带证明"同一个端点对**正确的人**是放行的"
        （⛔ 否则"什么都拒"也能让上面两条绿）。
        """
        self.assertEqual(self.nos_of(self.token_b), [self.order_others.order_no])
        detail = self.detail_of(self.order_others.order_no, token=self.token_b)
        self.assertEqual(detail["order_no"], self.order_others.order_no)

    def test_his_own_order_succeeds_on_both_endpoints(self):
        """⭐ **对照**：他自己的单，查单 200 + 我的单里有它（缺它，全拒的实现也能让上面绿）。"""
        detail = self.detail_of(self.order_mine.order_no)
        self.assertEqual(detail["order_no"], self.order_mine.order_no)
        self.assertIn(self.order_mine.order_no, self.nos_of())


class ScopeFourWayComparisonTests(_MyOrdersFixture):
    """⭐ spec §9-1：**范围收窄四路对照**（①大厅 ②查单 ④我的单；③改状态在另一个文件）。

    ## 为什么必须四路一起写（`E-0004` 形态的假绿）

    只写"别人的单不出现"时，一个**恒空**的实现也能让它绿。所以每条否定断言旁边都要有
    **正对照**（他自己的单必须出现 / 必须查得到），① 那一半还要有**第二个视角**
    （同一张单在**别人**那里是在厅的 ⇒ 证明它**真的在厅**，而不是"它其实没进厅"）。

    ⚠️ ① 那一半用的是 `order_hall`（甲有权限、**没人接**的厅单）：spec §9-1 的**前提**
    要求被断言的大厅单必须是 `WAITING_GRAB` + MAIN 行无主——否则它本来就不在 `search_hall`
    的结果里，那条断言**永远绿**（`selectors.py:571` 只取 `WAITING_GRAB`）。
    """

    def test_1_the_hall_list_does_not_show_an_order_claimed_by_someone_else(self):
        """① 大厅列表里**没有**别人接手的单（T5 已有，这里**核对**一次，并配两个正对照）。"""
        response = self.hall()
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], OK, payload)
        nos = sorted(row["order_no"] for row in payload["data"]["results"])
        self.assertNotIn(self.order_others.order_no, nos)
        # ⭐ 正对照①：**没人接**的厅单**在**（否则"大厅恒空"也能让上面绿）。
        self.assertIn(self.order_hall.order_no, nos)
        # ⭐ 正对照②：另一条路径上的正对照 = 他自己的单**不在大厅**（它已经被人接了），
        #    而它在「我的单」里**在**（两条路径互不冒充）。
        self.assertNotIn(self.order_mine.order_no, nos)
        self.assertIn(self.order_mine.order_no, self.nos_of())

    def test_2_getting_an_order_claimed_by_someone_else_is_1002(self):
        """② 查别人的单 ⇒ `1002`；⭐ 对照：查自己的单 ⇒ 200。"""
        self.assert_business_failure(self.order_detail(self.order_others.order_no), BUSINESS)
        self.assertEqual(
            self.detail_of(self.order_mine.order_no)["order_no"], self.order_mine.order_no
        )

    def test_4_my_orders_does_not_contain_an_order_claimed_by_someone_else(self):
        """④ 我的单里**没有**别人的单；⭐ 对照：有自己的那一张。"""
        nos = self.nos_of()
        self.assertNotIn(self.order_others.order_no, nos)
        self.assertEqual(nos, [self.order_mine.order_no])

    def test_a_nonexistent_order_no_is_1002_with_the_same_message(self):
        """不存在的单号 ⇒ `1002`，且与"别人的单"**同一句文案**（同形是承重的）。"""
        别人 = self.assert_business_failure(
            self.order_detail(self.order_others.order_no), BUSINESS
        )
        不存在 = self.assert_business_failure(
            self.order_detail("20261008000000009888"), BUSINESS
        )
        self.assertEqual(别人["msg"], 不存在["msg"])


class OrphanOrderTests(_MyOrdersFixture):
    """⭐ spec §9-14：**孤儿订单**（这张单**一条 MAIN 绩效行都没有**）⇒ `1002`，⛔ **不是 500**。

    `claim.py:227-231` 自己承认这种形态真实存在；先例 `peer_inbound.py:1183` 就是写
    `perf is None or …`。拿它当判据的理由：本端点的取数口从 `Performance` 起，
    漏了 `perf is None` 那一句就会 `AttributeError` ⇒ **HTTP 500**。
    """

    def setUp(self):
        super().setUp()
        self.orphan = self._order(
            self.game_a,
            "20261008000000000304",
            status=OrderStatus.IN_PROGRESS,
            taker=None,
            with_main_performance=False,
        )

    def test_getting_an_orphan_order_is_1002_not_500(self):
        response = self.order_detail(self.orphan.order_no)
        # ⚠️ 第一句就是"⛔ 不是 500"：HTTP 200（本仓的业务失败口径）＋ 业务码 `1002`。
        payload = self.assert_business_failure(response, BUSINESS)
        self.assertEqual(payload["msg"], self.body(self.order_detail("20261008000000009887"))["msg"])

    def test_an_orphan_order_never_appears_in_my_orders(self):
        """它也**不进**「我的单」（join 不上 MAIN 行）——而列表本身**不炸**。"""
        nos = self.nos_of()
        self.assertNotIn(self.orphan.order_no, nos)
        self.assertIn(self.order_mine.order_no, nos)


class SoftDeletedOrderTests(_MyOrdersFixture):
    """软删的单（`deleted_at` 非空）⇒ 查它 `1002`、且不进「我的单」（spec §5.3-2 末句）。

    ⚠️ 判据**同时**打两条路径：spec 明写"⛔ 别只写在「我的单」那一条上"。
    """

    def setUp(self):
        super().setUp()
        self.deleted = self._order(
            self.game_a,
            "20261008000000000305",
            status=OrderStatus.IN_PROGRESS,
            taker=self.taker_a,
            deleted_at=timezone.now(),
        )

    def test_a_soft_deleted_order_of_his_own_is_1002(self):
        self.assert_business_failure(self.order_detail(self.deleted.order_no), BUSINESS)

    def test_a_soft_deleted_order_does_not_appear_in_my_orders(self):
        nos = self.nos_of()
        self.assertNotIn(self.deleted.order_no, nos)
        self.assertIn(self.order_mine.order_no, nos)


class MyOrderPayloadTests(_MyOrdersFixture):
    """⭐ spec §5.2 / §9-8：行 = **§5.1 那 11 个 ＋ 这 7 个 = 恰好 18 个键**。

    ⚠️ 键集是**字面量**写在本文件顶部的（⛔ 不从实现模块 import / 派生 —— 派生是自指恒真，
    `E-0024`）。⚠️ 判据两向都要：**恰好相等**（多了 / 少了都红）＋ 显式枚举的"不许出现的键"
    一个都不在（拦截必须显式枚举，`E-0065`）。
    """

    def test_the_row_key_set_is_exactly_eighteen_keys(self):
        row = self.rows_of()[0]
        self.assertEqual(set(row), MY_ORDER_ROW_KEYS, row)
        self.assertEqual(len(MY_ORDER_ROW_KEYS), 18, "§5.2 的算式是 11 + 7")

    def test_no_forbidden_key_appears(self):
        row = self.rows_of()[0]
        self.assertEqual(FORBIDDEN_ROW_KEYS & set(row), set(), row)

    def test_the_customer_contact_fields_are_present(self):
        """⭐ **P2**：抢到之后**给**客户联系方式（§5.2 明写；§5.1 的大厅行⛔ 不许有）。"""
        row = self.rows_of()[0]
        self.assertEqual(row["customer_phone"], "19900000001")
        self.assertEqual(row["customer_ww"], "测试旺旺甲")

    def test_the_extra_keys_carry_real_values(self):
        """⭐ 那 7 个键**不是占位**：值真的来自库里（否则"给出全部的键、值恒空"也能绿）。"""
        row = self.rows_of()[0]
        order = Order.objects.get(order_no=self.order_mine.order_no)
        self.assertEqual(row["status"], OrderStatus.IN_PROGRESS)
        self.assertEqual(row["status_text"], order.get_status_display())
        self.assertIsNotNone(row["claimed_at"])
        self.assertTrue(row["updated_at"])
        # ⚠️ 金额是**两位小数字符串**（与仓内唯一那个出口同口径）。
        self.assertEqual(row["total_price"], "0.00")
        self.assertEqual(row["hall_price"], "88.00")

    def test_the_claimed_and_updated_moments_are_project_timezone_strings(self):
        """D-0302（2026-10-24）：`claimed_at` / `updated_at` 是**项目时区**（+08:00）串。

        TAKER-API 文档 §5.2 的示例本就是 `+08:00`；旧写法 `_iso` / 裸 `.isoformat()` 出
        `+00:00`，与文档示例矛盾、与 §5.1 大厅行（已收口）混屏。夹具把两个时刻钉在
        UTC 04:30 ⇒ 北京墙钟 12:30。
        """
        from datetime import datetime as dt
        from datetime import timezone as dt_tz

        fixed = dt(2026, 10, 24, 4, 30, tzinfo=dt_tz.utc)
        order = Order.objects.get(order_no=self.order_mine.order_no)
        Order.objects.filter(pk=order.pk).update(updated_at=fixed)
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN).update(
            claimed_at=fixed,
        )
        row = self.rows_of()[0]
        self.assertEqual(row["claimed_at"], "2026-10-24T12:30:00+08:00")
        self.assertEqual(row["updated_at"], "2026-10-24T12:30:00+08:00")

    def test_the_source_order_no_filter_narrows_to_that_order(self):
        """⭐ 2026-10-23：`?source_order_no=` 只在自己的单里按**逐字相等**筛。"""
        self.order_mine.source_order_no = "SRC-MINE-001"
        self.order_mine.save(update_fields=["source_order_no"])
        self.assertEqual(self.nos_of(params={"source_order_no": "SRC-MINE-001"}), [self.order_mine.order_no])
        self.assertEqual(self.rows_of(params={"source_order_no": "SRC-MINE"}), [])

    def test_my_orders_row_order_no_stays_the_internal_lookup_key(self):
        """⭐ D-0338（2026-10-30）**保护钉**：「我的单」行的 `order_no` 键 = **内置单号**，
        ⛔ 不跟大厅那一路一起改成「来源号优先」。

        为什么必须钉住：这一键是查单（`GET /orders/<order_no>/`）/ 改状态 / 进度图
        三个端点的**寻址参数**（`order_scope` 只认内置号精确匹配）——第三方从「我的单」
        拿号拼那三条 URL。本装配（`claimed_order_row`）与大厅行**共用**
        `hall_row_payload`，而大厅原生行的展示覆写住在 `native_hall_rows`
        （D-0338 刻意不放进共享装配）⇒ 谁把覆写"顺手"挪进 `hall_row_payload`，
        这一条当场红。
        """
        self.order_mine.source_order_no = "SRC-MINE-PROTECT-001"
        self.order_mine.save(update_fields=["source_order_no"])
        row = self.rows_of()[0]
        self.assertEqual(row["order_no"], self.order_mine.order_no)
        self.assertEqual(row["source_order_no"], "SRC-MINE-PROTECT-001")
        # 寻址闭环：拿行上这个 `order_no` 打查单端点 ⇒ 真的查得到（键没被改坏）。
        detail = self.detail_of(self.order_mine.order_no)
        self.assertEqual(detail["order_no"], self.order_mine.order_no)

    def test_the_detail_payload_has_the_same_keys_as_a_list_row(self):
        """⭐ 列表与查单**逐键同形**（同一份装配出口 ⇒ 两处形状不会漂移）。"""
        row = self.rows_of()[0]
        detail = self.detail_of(self.order_mine.order_no)
        self.assertEqual(set(detail), MY_ORDER_ROW_KEYS)
        self.assertEqual(set(detail), set(row))

    def test_the_detail_payload_values_match_the_list_row(self):
        """⭐ 不只是键集相同：**值**也逐键相同（两处装配分叉时这条会红）。"""
        row = self.rows_of()[0]
        detail = self.detail_of(self.order_mine.order_no)
        self.assertEqual(detail, row)

    def test_the_hall_row_has_no_customer_contact_fields(self):
        """⭐⛔ **P2 的另一半**（防回退）：大厅行里**不许**有 `customer_phone` / `customer_ww`。

        T5 已经有一条同义判据（`tests/test_takerapi_hall_api.py::HallPayloadTests`），
        这里**核对**一次的理由：本批给「我的单」加了这两个键，而两条路径**共用**
        `hall_views.hall_row_payload` ⇒ 最容易被"顺手加进去"的改法就是从那里加。
        """
        response = self.hall()
        rows = self.body(response)["data"]["results"]
        self.assertTrue(rows, "大厅是空的 ⇒ 这条断言恒真")
        for row in rows:
            self.assertNotIn("customer_phone", row)
            self.assertNotIn("customer_ww", row)


class UpdatedSinceTests(_MyOrdersFixture):
    """`updated_since`（spec §5 端点表第 3 行）：按 `order.updated_at >=` 过滤。

    ⚠️ 夹具用**两次 `Order.objects.filter(pk=…).update(updated_at=…)`** 显式把两张单的
    `updated_at` 拉开——⛔ 不靠 `time.sleep`：那会让用例变慢且**仍然可能**因为精度问题不稳。
    """

    def setUp(self):
        super().setUp()
        self.old = self._order(
            self.game_a, "20261008000000000306", status=OrderStatus.IN_PROGRESS,
            taker=self.taker_a,
        )
        self.new = self._order(
            self.game_a, "20261008000000000307", status=OrderStatus.IN_PROGRESS,
            taker=self.taker_a,
        )
        self.base = timezone.now() - timedelta(days=2)
        Order.objects.filter(pk=self.old.pk).update(updated_at=self.base)
        Order.objects.filter(pk=self.new.pk).update(
            updated_at=self.base + timedelta(hours=1)
        )
        # ⚠️ 夹具里那张「对照组」的单（`order_mine`）也**必须在分界点之前**，
        #    否则它会被一起过滤出来 ⇒ 本类第一条用例会以"多了一张"的**假象**红
        #    （它测的是"只回更新的那张"，不是"总数"）。
        Order.objects.filter(pk=self.order_mine.pk).update(
            updated_at=self.base - timedelta(hours=1)
        )
        #: 分界点落在两张之间（⭐ 不是"现在"）：这样才能分辨"真的按 `updated_at` 过滤"
        #: 与"按创建时刻过滤"（后者的两张单都在分界点之前 ⇒ 只回一张也会绿）。
        self.since = (self.base + timedelta(minutes=30)).isoformat()

    def test_only_the_order_updated_after_the_cutoff_comes_back(self):
        nos = self.nos_of(params={"updated_since": self.since})
        self.assertEqual(nos, [self.new.order_no])

    def test_both_come_back_without_the_parameter(self):
        """⭐ **对照**：不传时两张都在（否则"过滤恒空"也能让上一条绿）。"""
        nos = self.nos_of()
        self.assertIn(self.old.order_no, nos)
        self.assertIn(self.new.order_no, nos)

    def test_a_cutoff_before_both_returns_both(self):
        """⭐ 第二路对照：分界点落在**两张之前** ⇒ 两张都回（证明它是**真的**边界判断）。"""
        earlier = (self.base - timedelta(hours=1)).isoformat()
        nos = self.nos_of(params={"updated_since": earlier})
        self.assertIn(self.old.order_no, nos)
        self.assertIn(self.new.order_no, nos)

    def test_a_zulu_suffixed_value_is_accepted(self):
        """带 `Z` 的 ISO8601 也收（`parse_datetime` 支持它；⛔ 不接受就等于契约写窄了）。"""
        zulu = (self.base + timedelta(minutes=30)).astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")
        nos = self.nos_of(params={"updated_since": zulu})
        self.assertEqual(nos, [self.new.order_no])

    def test_a_malformed_value_is_1000_not_500(self):
        """⛔ 非法值 ⇒ `1000`（⛔ 不是 500、也⛔ 不是"静默不过滤"）。"""
        for bad in ("abc", "2026-13-45T99:99:99", "20261008"):
            with self.subTest(value=bad):
                self.assert_business_failure(
                    self.my_orders(params={"updated_since": bad}), VALIDATION
                )

    def test_a_malformed_value_does_not_silently_return_everything(self):
        """⭐ 最后那半句的判据：非法值**不**回全部（"静默不过滤"比 500 更贵——
        对端会以为拿到了增量、据此删掉自己那边的单）。"""
        payload = self.body(self.my_orders(params={"updated_since": "not-a-date"}))
        self.assertEqual(payload["code"], VALIDATION)
        self.assertIsNone(payload["data"])


class MyOrdersPaginationTests(_MyOrdersFixture):
    """分页（spec §5 统一口径）：默认 20 / **上限 100** / 越界夹到 100 / `count` 是命中总数。"""

    def setUp(self):
        super().setUp()
        self.extra = [
            self._order(
                self.game_a,
                f"2026100800000000040{index}",
                status=OrderStatus.IN_PROGRESS,
                taker=self.taker_a,
            )
            for index in range(3)
        ]

    def test_count_is_the_total_not_the_page_size(self):
        response = self.my_orders(params={"page": 1, "page_size": 2})
        data = self.body(response)["data"]
        self.assertEqual(len(data["results"]), 2)
        # ⭐ 命中总数 = 自己的 1 张 + 新加的 3 张 = 4（⛔ 不是本页条数 2）。
        self.assertEqual(data["count"], 4)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], 2)

    def test_the_second_page_returns_the_rest(self):
        first = self.body(self.my_orders(params={"page": 1, "page_size": 2}))["data"]
        second = self.body(self.my_orders(params={"page": 2, "page_size": 2}))["data"]
        self.assertEqual(second["count"], 4)
        self.assertEqual(len(second["results"]), 2)
        self.assertEqual(
            {row["order_no"] for row in first["results"]}
            & {row["order_no"] for row in second["results"]},
            set(),
        )

    def test_page_size_is_clamped_to_one_hundred(self):
        """⭐ 超上限 ⇒ **夹到 100**（⛔ 不是报错——契约写的就是"越界夹到 100"）。"""
        data = self.body(self.my_orders(params={"page_size": 1000}))["data"]
        self.assertEqual(data["page_size"], 100)
        self.assertEqual(data["count"], 4)

    def test_a_malformed_page_is_1000_not_500(self):
        for params in ({"page": "abc"}, {"page": 0}, {"page": "-1"}, {"page_size": "x"}, {"page_size": 0}):
            with self.subTest(params=params):
                self.assert_business_failure(self.my_orders(params=params), VALIDATION)

    def test_a_page_beyond_the_end_is_an_empty_page_not_an_error(self):
        """第 5 页没有行 ⇒ 空 `results` + **真实** `count`（⛔ 不报错、也不回 1002）。"""
        data = self.body(self.my_orders(params={"page": 5, "page_size": 2}))["data"]
        self.assertEqual(data["results"], [])
        self.assertEqual(data["count"], 4)


class MyOrdersAuthTests(_MyOrdersFixture):
    """鉴权面（spec §9-2 的"三组不混"，在**真路由**上打）。

    ⚠️ 本类只管**认证**；"上家侧令牌打本端点 ⇒ 401"那条**承重**判据在 T5 的
    `tests/test_takerapi_hall_api.py::CrossCredentialTypeOnRealRoutesTests` 里已有同款
    （它对 `v1/hall/` 打），本类补的是**本批新路由**上的同一件事。
    """

    def test_no_credential_is_401(self):
        response = self.anonymous.get(MY_ORDERS_PATH, REMOTE_ADDR=SOURCE_IP)
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])

    def test_an_openapi_token_is_rejected(self):
        api_app = self.make_api_app()
        token = self.issue_token(api_app, path="/api/open/oauth/token")
        response = self.my_orders(token=token)
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])
        self.assertEqual(self.body(response)["code"], UNAUTHENTICATED)

    def test_a_session_request_is_rejected(self):
        session = make_logged_in_client(
            domain=self.domain_name, username="my-orders-session-user", games=None
        )
        for path in (MY_ORDERS_PATH, ORDER_DETAIL_TEMPLATE.format(order_no=self.order_mine.order_no)):
            with self.subTest(path=path):
                response = session.get(path)
                self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])

    def test_the_taker_token_is_rejected_by_the_openapi_route(self):
        """反方向：打手令牌打**上家侧**真路由 ⇒ 401（两族各查各的表 ⇒ 结构性 401）。"""
        response = self.anonymous.get(
            OPENAPI_ORDERS_PATH,
            HTTP_AUTHORIZATION=f"Bearer {self.token_a}",
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])

    def test_the_control_group_a_valid_token_works(self):
        """⭐ 对照组（⛔ 缺了它，上面几条对"什么都拒"的实现也会绿）。"""
        self.assertIn(self.order_mine.order_no, self.nos_of())


class MyOrdersAssemblyTests(_MyOrdersFixture):
    """⭐ spec §9-15：**对外视图的装配是承重的** —— 三个类属性**恰好**那三件。

    ⚠️ 依据（红队原话）：`config/settings.py` 的 `REST_FRAMEWORK` **只**设了默认权限类
    （`IsAuthenticated`）、**没设**默认限流类 ⇒
    ① `permission_classes` 不写会吃默认的 `IsAuthenticated`（Bearer 下 `request.user` 是
       `AnonymousUser`）⇒ 合法令牌反被判 **401**；
    ② **限流漏挂 = 完全无限流，且没有任何别的网会红**（那些网只测 throttle 类自身的行为）。
    """

    def test_both_view_classes_are_assembled_exactly(self):
        from apps.takerapi.api.my_orders_views import (
            MyOrderDetailView,
            MyOrdersListView,
        )
        from apps.takerapi.authentication import TakerAppBearerAuthentication
        from apps.takerapi.throttling import TakerAppFallbackThrottle

        for view in (MyOrdersListView, MyOrderDetailView):
            with self.subTest(view=view.__name__):
                self.assertEqual(view.authentication_classes, [TakerAppBearerAuthentication])
                self.assertEqual(view.permission_classes, [])
                self.assertEqual(view.throttle_classes, [TakerAppFallbackThrottle])

    def test_the_node_is_not_the_gate_of_the_external_endpoints(self):
        """⭐ 撤掉该账号的节点 ⇒ 本批端点**照样通**（对外面的门是"绑定账号 + 接手人"）。

        ⚠️ 这条不是"没测"，而是**把口径钉死**：本线对外端点 `permission_classes = []`
        （spec §5：⛔ 不装权限类，与上家侧同口径）；"他有没有这个入口"由**凭据**回答，
        而"能看到哪些单"由**接手人**回答（不是节点）。
        """
        from apps.rbac.models import PermissionNode, Role

        node = PermissionNode.objects.get(code=MY_ORDERS_NODE)
        for name in self.taker_a.groups.values_list("name", flat=True):
            Role.objects.get(name=name).permissions.remove(node)
        self.assertIn(self.order_mine.order_no, self.nos_of())


class MyOrdersMalformedPathTests(_MyOrdersFixture):
    """㊙ **`test_no_route_500` 的已知盲区**：带路径转换器的路由不在那条腿的覆盖内。

    查单这条路由是 `v1/orders/<order_no:order_no>/`（**共享**转换器）⇒
    必须在这里补 **HTTP 级**判据。判据照 T5 给抢单补的那三条的写法。

    正确答复是 **404**：`OrderNoConverter` 的 `regex` 不匹配带 NUL 的路径段 ⇒ 那张网
    "畸形路径段根本不进视图"（⛔ 不是 500；裸 `<str:order_no>` 会让 `%00` 进
    `filter(order_no=…)` ⇒ PostgreSQL `DataError` ⇒ HTML 500，本仓实测过）。
    """

    def test_a_nul_in_the_order_no_is_404_not_500(self):
        response = self.anonymous.get(
            "/api/taker/v1/orders/2026%00 1/",
            HTTP_AUTHORIZATION=f"Bearer {self.token_a}",
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, NOT_FOUND, response.content[:200])

    def test_a_too_long_order_no_does_not_500(self):
        """⭐ **天文数字**那一格：300 字符的单号 ⇒ `1002`（⛔ 不是 500、也⛔ 不是 404）。

        ⚠️ 为什么要它：`order_no` 列宽是 20，而路径段本身**没有**长度上限 ⇒
        它会真的进 `filter(order_no=…)`（PostgreSQL 对 `varchar` 比较**不报** `value too long`，
        而是直接不命中）⇒ 答复是"查不到"那一档。
        """
        long_no = "9" * 300
        self.assert_business_failure(self.order_detail(long_no), BUSINESS)

    def test_my_orders_survives_malformed_query_params(self):
        """本端点自己的畸形入参兜底（它是一条**不带转换器**的显式路由）。"""
        for params in ({"page": "abc"}, {"updated_since": "x"}, {"page_size": "-5"}):
            with self.subTest(params=params):
                response = self.my_orders(params=params)
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertEqual(self.body(response)["code"], VALIDATION)

    def test_a_trailing_slash_is_required_on_both_paths(self):
        """⚠️ 路径契约逐字：两条都**带尾斜杠**（⛔ 不带的那条不是本端点的路由）。

        ⚠️ Django 的 `APPEND_SLASH` 只在 **GET** 且**没有**匹配时才重定向；本仓的
        `CommonMiddleware` 会把它变成 301（而测试客户端默认不跟）⇒ 判据钉"**不是 200 的成功**"，
        ⛔ 不钉具体的 301/404（那是框架配置、不是本线契约）。
        """
        response = self.anonymous.get(
            "/api/taker/v1/my/orders",
            HTTP_AUTHORIZATION=f"Bearer {self.token_a}",
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertNotEqual(response.status_code, 200)


# ══════════════════════════════════════════════════════════════════════════════════════════
# 红队只读审查 → 总指挥裁定补的两块：① 竞态收口 + ② 换人失效补网
# ══════════════════════════════════════════════════════════════════════════════════════════
class DetailAssemblyRaceTests(_MyOrdersFixture):
    """① 查单的「两次查询」竞态 ⇒ **`1002`**（⛔ 不是 500）。

    ## 竞态的形状（红队 点名）
    详情视图先调范围判据 `claimed_perf_of(taker, order_no)`，**再**用
    `claimed_order_with_price_qs(taker).get(pk=perf.order_id)` 取装配用的那张单
    （两次查询各有理由：`claimed_at` 在**绩效行**上、两个注解在 **`Order`** 上）。
    两次之间那一瞬单被**软删** / **换了接手人** ⇒ 第二次查询命中 0 行 ⇒
    `.get()` 抛 `Order.DoesNotExist` ⇒ **HTTP 500**；而正确答复是 `1002`
    （与"查不到 / 不是他的"**同形**，spec §5.3-2）。

    ## 判据如何**弄红**（改前实测红、改后绿）
    - 把那一句 `.get()` 打桩成抛 `Order.DoesNotExist`：`mock.patch` 打在**被查找的那个
      模块**（`apps.takerapi.api.my_orders_views`）上（`E-0029`），并**自证桩接上了** ——
      `patched.assert_called_once()` ＋ 那句 `.get` 的参数据实
      （`fake_qs.get.assert_called_once_with(pk=…)`）；
    - 客户端显式 `raise_request_exception=False`：竞态下的**错误答复是 HTTP 500**，而
      Django 测试客户端**默认会把异常抛进用例** ⇒ 那样只看到 ERROR、看不到"500 而不是
      `1002`"这件事本身（判据要打在**答复**上）。
    - ⭐ 正对照：**同一个视图**、同一颗令牌、一张正常单 ⇒ 仍旧 `200`（缺它，"什么都回
      `1002`"的实现也能让上面那条绿）。

    ⚠️ 证明力边界：本类**只**证"那一行取不到时答复是 `1002`"。它**不**证"这个竞态在真实
    并发下会发生"（那要把两次查询真的交错开，本线没有那种夹具）。
    """

    #: 一个**不存在**的合法单号（20 位数字，过得了路由转换器）——拿它的文案当"同形"基准。
    ORDER_NO_MISSING = "20261008000000009999"

    def _race_client(self):
        """一个**不把异常抛进用例**的客户端（理由见类 docstring 的判据如何弄红）。"""
        return Client(SERVER_NAME=self.domain_name, raise_request_exception=False)

    def _race_detail(self, order_no, token=None):
        return self._race_client().get(
            ORDER_DETAIL_TEMPLATE.format(order_no=order_no),
            HTTP_AUTHORIZATION=f"Bearer {token or self.token_a}",
            REMOTE_ADDR=SOURCE_IP,
        )

    def test_the_order_vanishing_between_the_two_queries_is_1002_not_500(self):
        """⭐ 范围判据过了、装配那一行取不到 ⇒ **`1002`**（⛔ 不是 500）。"""
        fake_qs = mock.Mock()
        fake_qs.get.side_effect = Order.DoesNotExist
        with mock.patch(
            "apps.takerapi.api.my_orders_views.claimed_order_with_price_qs",
            return_value=fake_qs,
        ) as patched:
            response = self._race_detail(self.order_mine.order_no)
        # ⭐ 桩自证（`E-0029`）：打在**被查找的模块**上，且**真的被走到**（含 `.get` 的参数）。
        patched.assert_called_once()
        fake_qs.get.assert_called_once_with(pk=self.order_mine.pk)
        # ⭐ 判据：**不是 500**，且与"查不到"**同一句文案 + 同一个码**（spec §5.3-2 的"同形"）。
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        payload = self.assert_business_failure(response, BUSINESS)
        missing = self.assert_business_failure(
            self.order_detail(self.ORDER_NO_MISSING), BUSINESS
        )
        self.assertEqual(payload["msg"], missing["msg"])

    def test_a_normal_order_still_assembles(self):
        """⭐ 正对照：同一条路径、不打桩 ⇒ 正常单仍旧 `200`，且单数版逐键 = 列表版。"""
        detail = self.detail_of(self.order_mine.order_no)
        self.assertEqual(detail["order_no"], self.order_mine.order_no)
        self.assertEqual(
            set(detail), MY_ORDER_ROW_KEYS, sorted(set(detail) ^ MY_ORDER_ROW_KEYS)
        )


class ReassignmentInvalidatesTheOldTakerTests(_MyOrdersFixture):
    """② 补网：**走真实路径换人** ⇒ 旧接手人四条端点**立刻失效**、新接手人四条全通。

    ## 为什么这条必须有（红队）
    「经 `/assign/` 换人 ⇒ 旧人下一次任何一条端点都 `1002`」是本批授权面的**前提**，而既有
    用例都是**直接造一条归属别人的 MAIN 绩效行**（夹具 `order_others`）来代表"别人的单"
    ⇒ 换人这件事**没有任何用例钉住**：将来 `assign_order` 改成"删旧行、建新行"，或者换人时
    忘了清旧行的 `user_id`，四条端点会**静默**放行旧人（没有任何网会红）。

    ## 怎么做到"真实路径"（⛔ 不自己 UPDATE 绩效行）
    调内部 `/assign/` 对应的**领域函数** `apps.business.order_writes.assign_order`
    （`dry_run=False`：真写 —— `reset_taker_performance` 回退旧人七列 ＋ `set_taker` 换人
    ＋ 同事务那几条副作用），并在同一处**断言终态**：那条 MAIN 绩效行的 `user_id` 已经换成
    新接手人（⛔ 不拿"函数没报错"当判据）。

    ## 单号打**载荷**上取（`E-0176`）
    单号**从新接手人自己看到的「我的单」载荷里取**，再用它拼另外几条端点的 URL
    （⛔ 不直接拿夹具对象拼 —— 那样"调用方其实拿不到这个单号"也不会被发现）。

    ## 四条端点各断言什么

    | 端点 | 旧接手人（换人后） | 新接手人（换人后） |
    |---|---|---|
    | `GET /api/taker/v1/my/orders/` | **成功**但列表里**没有**它 | 列表里**有**它 |
    | `GET /api/taker/v1/orders/<单号>/` | `1002`（与"查不到"同形） | `200` + 逐键 |
    | `POST …/status/` | `1002`（**且状态没变、不留痕**） | `200` + **库里真变了** |
    | `GET …/images/` | `1002`（⛔ 不是空列表） | `200`（认领前空、认领后有） |
    | `POST …/images/ticket/` | `1002`（**且一行都不落**） | `200` + 一行 `pending` |
    | `POST …/images/` | `1002` | `200` + 那一行变 `ready` |

    ⚠️ 「我的单」是旧接手人**唯一**一条成功答复（`200` + 列表里没有它）——另外几条都是
    `1002`。这是**有意的**（spec §5.3-2）：列表回答的是"他接手的单的**集合**"，不是"某一单
    的授权"，换人只把它从集合里拿掉。
    ⚠️ 进度图那两条 `POST` 都**必带**幂等键，且**旧人那几次与新人那几次各自换键**：
    同一颗键配不同 body / 路径会回 `1000`（同键异体），那会把 `1002` 这条判据顶掉。
    ⚠️ 夹具沿用 `_MyOrdersFixture`（两个打手 + 两套凭据 + 一张 `IN_PROGRESS` 的单），
    另外补两件它没有的：`order.change_status` 节点（新人改状态那一步要它）与
    `MEDIA_ROOT` 临时目录（认领那一跳要真把字节放进 storage；⛔ 不往本机 media 写文件）。
    """

    #: 一个**不存在**的合法单号（20 位数字）——拿它的文案当"同形"基准。
    ORDER_NO_MISSING = "20261008000000009999"

    #: 幂等键（字面量；旧人 / 新人各自一套，理由见类 docstring 末段）。
    IDEM_OLD_STATUS = "probe-reassign-old-status"
    IDEM_NEW_STATUS = "probe-reassign-new-status"
    IDEM_OLD_TICKET = "probe-reassign-old-ticket"
    IDEM_NEW_TICKET = "probe-reassign-new-ticket"
    IDEM_OLD_CLAIM = "probe-reassign-old-claim"
    IDEM_NEW_CLAIM = "probe-reassign-new-claim"

    def setUp(self):
        super().setUp()
        # ⚠️ 认领那一跳要往 storage 放字节 ⇒ `MEDIA_ROOT` 指到**临时目录**（与
        #    `tests/test_takerapi_order_images_api.py` 同款；⛔ 不写本机 `.tools/media`）。
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        patcher.enable()
        self.addCleanup(patcher.disable)
        # ⚠️ 两个打手在**同一个组**里 ⇒ 这条实际同时给到两人（本类两半都要它，
        #    ⛔ 不是"只给新人"）。节点 = `order.change_status`（`IN_PROGRESS → 待验收`
        #    那条边要它，见 `order_status._TARGET_NODES`）。
        grant_nodes(self.taker_a, "order.change_status", "order.submit_work")

    # ── 真实换人 + 从载荷取单号 ─────────────────────────────────────────
    def _reassign_to_taker_b(self):
        """走**真实领域路径**换人（= 内部 `/assign/` 的动作体），并断言**终态**。"""
        ctx = WriteContext(
            actor="换人补网测试客服",
            log_actor="换人补网测试客服",
            log_actor_id=None,
            # ⚠️ `nodes` 在这一档**不是承重的**：单已是 `IN_PROGRESS` ⇒ `assign_order` 按
            #    R-3 **跳过** `apply_transition`（状态机没有自环）⇒ 一个字都不咨询节点。
            #    写这一份只是贴近真实调用方（内部 `/assign/` 的类级门）。
            nodes=frozenset({"order.assign_taker", "order.submit_payout_price"}),
            user=None,
        )
        outcome = assign_order(self.order_mine, taker=self.taker_b, ctx=ctx, dry_run=False)
        self.assertTrue(outcome.ok, outcome.msg)
        # ⭐ **终态判据**：那条 MAIN 绩效行真的换了归属人（⛔ 不靠"函数没报错"）。
        perf = Performance.objects.get(order=self.order_mine, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, self.taker_b.pk)

    def _reassigned_order_no_from_payload(self):
        """换人，然后从**新接手人自己看到的「我的单」载荷**里取那个单号。"""
        self._reassign_to_taker_b()
        rows = self.rows_of(self.token_b)
        row = next((r for r in rows if r["order_no"] == self.order_mine.order_no), None)
        self.assertIsNotNone(row, sorted(r["order_no"] for r in rows))
        return row["order_no"]

    # ── 发请求 ─────────────────────────────────────────────────────────
    def _get(self, path, *, token):
        return self.anonymous.get(
            path, HTTP_AUTHORIZATION=f"Bearer {token}", REMOTE_ADDR=SOURCE_IP
        )

    def _post(self, path, payload, *, key, token):
        """`POST`（⚠️ `separators` 固定：multipart 的 boundary 随机会让同键异体⇒假红）。"""
        return self.anonymous.post(
            path,
            data=json.dumps(payload, separators=(",", ":")),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            HTTP_IDEMPOTENCY_KEY=key,
            REMOTE_ADDR=SOURCE_IP,
        )

    def _ok(self, response):
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], OK, payload)
        return payload["data"]

    # ── ① 旧接手人：四条端点全部失效 ───────────────────────────────────
    def test_the_old_taker_is_locked_out_everywhere_after_a_real_reassignment(self):
        order_no = self._reassigned_order_no_from_payload()
        # 基线（换人**之后**、旧人动手**之前**）：下面几条否定断言要有东西可比。
        status_before = Order.objects.get(order_no=order_no).status
        logs_before = OperationLog.objects.filter(order_no=order_no).count()

        # ⭐ ① 我的单：**成功**（`200` + `code=0`）但列表里**没有它**（⛔ 不是 1002）。
        self.assertNotIn(order_no, self.nos_of(self.token_a))
        # ⭐ ② 查单 ⇒ `1002`，文案与"查不到"**逐字相同**。
        detail = self.assert_business_failure(
            self.order_detail(order_no, token=self.token_a), BUSINESS
        )
        missing = self.assert_business_failure(
            self.order_detail(self.ORDER_NO_MISSING, token=self.token_a), BUSINESS
        )
        self.assertEqual(detail["msg"], missing["msg"])
        # ⭐ ③ 改状态 ⇒ `1002`（⛔ 且一行都不许写：状态没变、操作日志没多一行）。
        self.assert_business_failure(
            self._post(
                STATUS_TEMPLATE.format(order_no=order_no),
                {"status": "PENDING_REVIEW"},
                key=self.IDEM_OLD_STATUS,
                token=self.token_a,
            ),
            BUSINESS,
        )
        # ⭐ ④ 进度图三条 ⇒ `1002`（列表那条⛔ **不是空列表**）。
        self.assert_business_failure(
            self._get(IMAGES_TEMPLATE.format(order_no=order_no), token=self.token_a),
            BUSINESS,
        )
        self.assert_business_failure(
            self._post(
                TICKET_TEMPLATE.format(order_no=order_no),
                {"ext": "jpg"},
                key=self.IDEM_OLD_TICKET,
                token=self.token_a,
            ),
            BUSINESS,
        )
        self.assert_business_failure(
            self._post(
                IMAGES_TEMPLATE.format(order_no=order_no),
                {"key": "shared-orders/whatever/probe-reassign.jpg"},
                key=self.IDEM_OLD_CLAIM,
                token=self.token_a,
            ),
            BUSINESS,
        )
        # ⭐ 终态（三条否定断言的"真的什么都没发生"）：状态、留痕、进度图行三处都没动。
        self.assertEqual(Order.objects.get(order_no=order_no).status, status_before)
        self.assertEqual(
            OperationLog.objects.filter(order_no=order_no).count(), logs_before
        )
        self.assertEqual(
            OrderScreenshot.objects.filter(order__order_no=order_no).count(), 0
        )

    # ── ② 新接手人：四条端点全通（缺它，"什么都拒"也能让上面那条绿）──────
    def test_the_new_taker_can_use_every_endpoint_after_the_same_reassignment(self):
        order_no = self._reassigned_order_no_from_payload()
        # ⭐ ① 我的单里有它。
        self.assertIn(order_no, self.nos_of(self.token_b))
        # ⭐ ② 查单 `200` + 逐键。
        detail = self.detail_of(order_no, token=self.token_b)
        self.assertEqual(detail["order_no"], order_no)
        self.assertEqual(
            set(detail), MY_ORDER_ROW_KEYS, sorted(set(detail) ^ MY_ORDER_ROW_KEYS)
        )
        # ⭐ ③ 改状态 `200` + **库里真变了**（⛔ 不看答复自说自话）。
        changed = self._ok(
            self._post(
                STATUS_TEMPLATE.format(order_no=order_no),
                {"status": "PENDING_REVIEW"},
                key=self.IDEM_NEW_STATUS,
                token=self.token_b,
            )
        )
        self.assertEqual(changed["order_no"], order_no)
        self.assertEqual(
            Order.objects.get(order_no=order_no).status, OrderStatus.PENDING_REVIEW
        )
        # ⭐ ④ 进度图：列表（认领前空）→ 取票 → 真上传 → 认领 → 列表里有它。
        listing = self._ok(
            self._get(IMAGES_TEMPLATE.format(order_no=order_no), token=self.token_b)
        )
        self.assertEqual(listing["results"], [])
        ticket = self._ok(
            self._post(
                TICKET_TEMPLATE.format(order_no=order_no),
                {"ext": "jpg"},
                key=self.IDEM_NEW_TICKET,
                token=self.token_b,
            )
        )
        # ⚠️ 对象名取自**取票的载荷**（`E-0176`），⛔ 不从库里翻。
        object_key = ticket["key"]
        get_storage().put_bytes(object_key, PROGRESS_JPEG, content_type="image/jpeg")
        claimed = self._ok(
            self._post(
                IMAGES_TEMPLATE.format(order_no=order_no),
                {"key": object_key},
                key=self.IDEM_NEW_CLAIM,
                token=self.token_b,
            )
        )
        self.assertEqual(
            OrderScreenshot.objects.get(object_key=object_key).status,
            OrderScreenshot.STATUS_READY,
        )
        after = self._ok(
            self._get(IMAGES_TEMPLATE.format(order_no=order_no), token=self.token_b)
        )
        self.assertEqual([row["url"] for row in after["results"]], [claimed["url"]])
        # ⭐ 旧接手人**此刻**再看这张单：仍旧 `1002`（换人是**持久**的，不是只对第一条请求）。
        self.assert_business_failure(
            self.order_detail(order_no, token=self.token_a), BUSINESS
        )


class HiddenOrderScopeTests(_MyOrdersFixture):
    """⭐ 评审 Important 7：隐藏单**不是**他接的单（「我的单」/ 查单都看不到）。

    ⚠️ 防御性：生产上隐藏单没有 MAIN 绩效行（天然 join 不上），本类故意造一条
    `user=甲` 的 MAIN 行 ⇒ 只有显式 `hall_card` 排除才拦得住。
    """

    def setUp(self):
        super().setUp()
        from tests.hall_shadow_helpers import make_mirror

        self.hidden = self._order(
            self.game_a, "20261008000000000304",
            status=OrderStatus.IN_PROGRESS, taker=self.taker_a,
        )
        self.hidden.hall_card = make_mirror(
            game=self.game_a, bound_by=self.taker_a, no="SYN-SHADOW-MY"
        )
        self.hidden.save(update_fields=["hall_card"])

    def test_it_does_not_appear_in_his_my_orders(self):
        nos = self.nos_of()
        self.assertIn(self.order_mine.order_no, nos)       # 正对照
        self.assertNotIn(self.hidden.order_no, nos)

    def test_reading_it_by_order_no_is_1002(self):
        self.assert_business_failure(
            self.order_detail(self.hidden.order_no), BUSINESS
        )
