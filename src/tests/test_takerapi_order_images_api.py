"""T6：打手侧**对外**端点第二批之三 —— **进度图**（`GET …/images/` + `POST …/images/ticket/`
+ `POST …/images/`）。

spec：`docs/specs/2026-10-08-taker-open-api-design.md` §5 端点表第 6 行 · §5.3-2（范围判据）·
§5.4（失败档）· §9 的 1（四路对照的等价判据）/ 14（孤儿订单）。

## 本文件证什么（逐条都能失败）

| # | 判据 | spec |
|---|---|---|
| 1 | ⭐ **别人的单 ⇒ 三条都 `1002`**（列表 / 取票 / 认领），⭐ 对照：他自己的单三条都成功 | §5.3-2 / §9-1 |
| 2 | ⭐⭐ **「他是当前接手人」不是「游戏范围内」**：一张**在他游戏权限内、但被别人接手**的单 ⇒ 三条都 `1002` | §5.3-2 |
| 3 | ⭐ **取票 + 认领真的落库**（判据打在 `OrderScreenshot` 的**行**上：`pending → ready` + `size_bytes` / `content_type`（**嗅探出来的**）/ `original_name` / `remark` / `ready_at`），⛔ **不看日志** | §5 端点表第 6 行 |
| 4 | ⭐ **只给已认领完成的图**：`pending` 的行**不在列表里**；认领之后**才**出现 | §5 端点表第 6 行 |
| 5 | ⭐ **认领回来的那一项与列表里那一项逐键同形**（同一份装配出口 ⇒ 两处形状不会漂移） | §5 端点表第 6 行 |
| 6 | ⭐ **领域口是同一份实现**（⛔ 不抄第二份）：两条端点**确实经过**业务层那两个函数（打桩判据）+ `img_type=1` ⇒ `1000` + **一行不写** | §5.3-2 / §5-E |
| 7 | **闸门**（复用领域实现的分内事，这里只核"答复与库都对"）：非图片字节 / 空对象 ⇒ `1000` + **那一行被删掉** | §5 端点表第 6 行 |
| 8 | ⭐ **幂等两条**：同键重发 ⇒ 回放且**副作用只发生一次**（取票只落一行）；缺键 ⇒ `1000` | §5 统一口径 |
| 9 | ⭐ **孤儿订单**（没有 MAIN 绩效行）⇒ 三条都 `1002`，⛔ **不是 500** | §9-14 |
| 10 | ⭐ **装配**（§9-15）：三条端点的三个类属性**恰好**那三件 | §9-15 |
| 11 | **鉴权面**：无令牌 / 上家侧令牌 / 会话 ⇒ `401`；⭐ 对照组 | §9-2 |
| 12 | **畸形入参 / 路径**：非对象体 / 缺 `key` / 超长 `key` / NUL 路径段（⇒ 404）/ 超长单号（⇒ `1002`）⛔ 都不许 500 | `test_no_route_500` 的已知盲区 |

## ⚠️ 证明力边界（如实写清楚）

- 令牌**一律从 `/api/taker/oauth/token` 换**（走真路由 + 真 `Client`，⛔ 不手搓夹具）；
- 存储后端用**本机替身**（`MEDIA_STORAGE_BACKEND=local` 默认形态）+ `MEDIA_ROOT` 指到临时目录
  （⛔ 不往本机 `.tools/media` 写文件）——所以"图真的进库"这条判据打在**行**上、
  对象则落在临时目录里（两者都验）；
- 夹具一律**合成值**（`AGENTS.md` §5-C），来源 IP 取文档保留段（⛔ 不是任何真实客户端）。
"""
import json
import pathlib
import tempfile
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business.models import EmpType, Game, Order, OrderScreenshot, OrderStatus, Performance
from apps.core.storage import get_storage
from apps.openapi.models import ApiApp
from apps.takerapi.models import TakerApp
from tests.p5_auth_helpers import grant_nodes, make_logged_in_client

User = get_user_model()

# ── 路径（与 `apps/takerapi/urls.py` 逐字一致；⚠️ 都带尾斜杠）────────────────────
TOKEN_PATH = "/api/taker/oauth/token"
OPENAPI_TOKEN_PATH = "/api/open/oauth/token"
OPENAPI_ORDERS_PATH = "/api/open/v1/orders/"
IMAGES_TEMPLATE = "/api/taker/v1/orders/{order_no}/images/"
TICKET_TEMPLATE = "/api/taker/v1/orders/{order_no}/images/ticket/"

GRANT_TYPE = "client_credentials"

# ── 合成凭据（⛔ 与任何真实租户的 appKey / secret 无关）────────────────────────
TAKER_A_KEY = "takerimageskey00001"
TAKER_A_SECRET = "taker-images-secret-0000001"
TAKER_B_KEY = "takerimageskey00002"
TAKER_B_SECRET = "taker-images-secret-0000002"
OPENAPI_KEY = "openimageskey0000001"
OPENAPI_SECRET = "open-images-secret-000001"

#: 文档保留段 `203.0.113.0/24` 的地址（⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.91"

# ── 业务码 / HTTP 码字面值（`E-0024`：写数字）──────────────────────────────────
OK = 0
VALIDATION = 1000
BUSINESS = 1002
UNAUTHENTICATED = 401
NOT_FOUND = 404

#: 该账号持的节点（**字面量**）。⚠️ 本批三条端点**不装权限类**（`permission_classes = []`）
#: ⇒ 节点不是它们的门；挂着它只为让夹具贴近真实账号（判据不依赖它）。
UPLOAD_NODE = "order.upload_screenshot"

# ── 载荷键集（**字面量**；⛔ 不从 `apps/openapi/serializers` 或本线实现 import）────
TICKET_DATA_KEYS = {"key", "upload_url", "expires_in"}
IMAGE_ROW_KEYS = {
    "img_type",
    "url",
    "original_name",
    "size_bytes",
    "remark",
    "uploaded_by_name",
    "created_at",
}

# ── 文案（对外契约的一部分）────────────────────────────────────────────────────
BAD_EXT_MSG = "只支持 jpg / jpeg / png"
IMG_TYPE_MSG = "本接口只支持订单进度图（img_type=0）"
NO_PENDING_MSG = "没有待认领的上传记录"
EMPTY_MSG = "图片无效或为空"

#: 一个**真的** JPEG 魔数开头的最小字节串（类型判据是魔数 ⇒ 假开头会在认领时被拒）。
JPEG = b"\xff\xd8\xff" + b"syn-progress-image-body"
#: ⛔ **不是**图片的字节（打"魔数认不出"那一档）。
NOT_AN_IMAGE = b"this-is-not-an-image"

IDEM_TICKET = "probe-images-ticket-01"
IDEM_CLAIM = "probe-images-claim-01"

ORDER_NO_MINE = "20261008000000000601"
ORDER_NO_OTHERS = "20261008000000000602"
ORDER_NO_ORPHAN = "20261008000000000603"


class _ImagesFixture(TenantTestCase):
    """两个打手（各持**两款游戏权限**）+ 各一张进行中的单 + 两套凭据。

    ⚠️ 甲对**两款游戏都有权限**是有意的：第 2 条判据要造的那张"范围内但被别人接手"的单
    必须落在他有权限的游戏里——否则用例会以"范围外"这个**另一个原因**变绿（`E-0004` 形态）。
    """

    def setUp(self):
        super().setUp()
        # ⚠️ `MEDIA_ROOT` 指到**临时目录**（⛔ 不往本机 `.tools/media` 写文件）。
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        patcher.enable()
        self.addCleanup(patcher.disable)

        self.domain_name = self.domain.domain
        self.anonymous = Client(SERVER_NAME=self.domain_name)
        self.game_a = Game.objects.create(name="进度图测试游戏甲")
        self.game_b = Game.objects.create(name="进度图测试游戏乙")

        self.taker_a = self._make_taker("images-taker-a", games=[self.game_a, self.game_b])
        self.taker_b = self._make_taker("images-taker-b", games=[self.game_a, self.game_b])
        self.app_a = self._make_app(
            "进度图测试应用甲", TAKER_A_KEY, TAKER_A_SECRET, self.taker_a
        )
        self.app_b = self._make_app(
            "进度图测试应用乙", TAKER_B_KEY, TAKER_B_SECRET, self.taker_b
        )
        self.token_a = self.issue_token(self.app_a)
        self.token_b = self.issue_token(self.app_b)

        self.order_mine = self._order(self.game_a, ORDER_NO_MINE, taker=self.taker_a)
        #: ⭐⭐ 在**甲的**游戏权限范围内、但 MAIN 绩效行归**乙**。
        self.order_others = self._order(self.game_a, ORDER_NO_OTHERS, taker=self.taker_b)

    # ── 造物 ───────────────────────────────────────────────────────────
    def _make_taker(self, username, *, games):
        user = User.objects.create_user(username=username, password="pw-12345678")
        group, _ = Group.objects.get_or_create(name="代练员")
        user.groups.add(group)
        StaffProfile.objects.create(user=user).games.set(games)
        grant_nodes(user, UPLOAD_NODE)
        return user

    def _make_app(self, name, app_key, app_secret, taker):
        return TakerApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret=app_secret,
            taker=taker,
            created_by=taker,
        )

    def _order(self, game, order_no, *, taker=None, with_main_performance=True):
        order = Order.objects.create(
            game=game,
            order_no=order_no,
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("88.00"),
        )
        if with_main_performance:
            Performance.objects.create(
                order=order,
                user=taker,
                emp_type=EmpType.MAIN,
                temp_amount=Decimal("88.00"),
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
        return ApiApp.objects.create(
            name="进度图测试上家应用甲",
            app_key=OPENAPI_KEY,
            app_secret=OPENAPI_SECRET,
            created_by=self.taker_a,
        )

    # ── 发请求 ─────────────────────────────────────────────────────────
    def _post(self, path, body, *, key, token=None, remote_addr=SOURCE_IP, raw=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token or self.token_a}"}
        if key is not None:
            headers["HTTP_IDEMPOTENCY_KEY"] = key
        data = raw if raw is not None else json.dumps(body, separators=(",", ":"))
        return self.anonymous.post(
            path,
            data=data,
            content_type="application/json",
            REMOTE_ADDR=remote_addr,
            **headers,
        )

    def list_images(self, order_no=ORDER_NO_MINE, *, token=None, params=None):
        return self.anonymous.get(
            IMAGES_TEMPLATE.format(order_no=order_no),
            params or {},
            HTTP_AUTHORIZATION=f"Bearer {token or self.token_a}",
            REMOTE_ADDR=SOURCE_IP,
        )

    def ticket(self, body=None, *, order_no=ORDER_NO_MINE, key=IDEM_TICKET, raw=None, **kwargs):
        payload = {"ext": "jpg"} if body is None else body
        return self._post(
            TICKET_TEMPLATE.format(order_no=order_no), payload, key=key, raw=raw, **kwargs
        )

    def claim(self, object_key, *, order_no=ORDER_NO_MINE, key=IDEM_CLAIM, body=None, raw=None, **kwargs):
        payload = {"key": object_key} if body is None else body
        return self._post(
            IMAGES_TEMPLATE.format(order_no=order_no), payload, key=key, raw=raw, **kwargs
        )

    # ── 断言小工具 ─────────────────────────────────────────────────────
    @staticmethod
    def body(response):
        return json.loads(response.content)

    def assert_ok(self, response):
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], OK, payload)
        return payload["data"]

    def assert_business_failure(self, response, code):
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], code, payload)
        self.assertIsNone(payload["data"], payload)
        return payload

    def issue_and_upload(self, *, order_no=ORDER_NO_MINE, body=None, ext="jpg", key=None, token=None):
        """走**取票**端点拿 key，再把字节**真放进 storage**（本机 = 直接写文件）。

        返回 `(row, key)`；`row` 从库里重读（⛔ 不信响应）。
        """
        data = self.assert_ok(
            self.ticket(
                {"ext": ext},
                order_no=order_no,
                key=key or f"{IDEM_TICKET}-{order_no}",
                token=token,
            )
        )
        object_key = data["key"]
        get_storage().put_bytes(
            object_key, JPEG if body is None else body, content_type="image/jpeg"
        )
        return OrderScreenshot.objects.get(object_key=object_key), object_key


class ScopeTests(_ImagesFixture):
    """⭐ spec §5.3-2：三条端点**逐条**都过「他是这张单的当前接手人」。"""

    def test_listing_someone_elses_images_is_1002_not_an_empty_list(self):
        """⭐ 别人的单 ⇒ `1002`（⛔ **不是**空列表：那会让第三方分不清"没图"与"不是你的单"）。"""
        self.assert_business_failure(self.list_images(ORDER_NO_OTHERS), BUSINESS)

    def test_ticketing_someone_elses_order_is_1002_and_writes_nothing(self):
        self.assert_business_failure(self.ticket(order_no=ORDER_NO_OTHERS, key="probe-img-t-others"), BUSINESS)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_others).count(), 0)

    def test_claiming_on_someone_elses_order_is_1002(self):
        self.assert_business_failure(
            self.claim("shared-orders/whatever/x.jpg", order_no=ORDER_NO_OTHERS, key="probe-img-c-others"),
            BUSINESS,
        )

    def test_the_three_endpoints_succeed_on_his_own_order(self):
        """⭐ **对照**（缺它，"什么都拒"的实现也能让上面三条绿）。"""
        # ① 列表：空但成功（⛔ 不是 1002）。
        data = self.assert_ok(self.list_images())
        self.assertEqual(data["results"], [])
        self.assertEqual(data["count"], 0)
        # ② 取票 ⇒ 一行 `pending`。
        row, object_key = self.issue_and_upload()
        self.assertEqual(row.status, OrderScreenshot.STATUS_PENDING)
        # ③ 认领 ⇒ 那一行变 `ready`。
        claimed = self.assert_ok(self.claim(object_key))
        self.assertEqual(set(claimed), IMAGE_ROW_KEYS)
        self.assertEqual(
            OrderScreenshot.objects.get(pk=row.pk).status, OrderScreenshot.STATUS_READY
        )

    def test_an_order_in_his_game_scope_but_claimed_by_someone_else_is_1002(self):
        """⭐⭐ **「他是当前接手人」不是「游戏范围内」**（红队点名）。

        `order_others` **在**甲的游戏权限范围内（`game_a`，甲有权限）⇒
        只有"他是当前接手人"那份判据拦得住它。
        ⛔ 把 `order_scope` 换成 T5 那套"游戏范围"⇒ 本条**必红**（读数见 T6 报告）。
        """
        for label, response in (
            ("list", self.list_images(ORDER_NO_OTHERS)),
            ("ticket", self.ticket(order_no=ORDER_NO_OTHERS, key="probe-img-scope-t")),
            ("claim", self.claim("shared-orders/x/y.jpg", order_no=ORDER_NO_OTHERS, key="probe-img-scope-c")),
        ):
            with self.subTest(endpoint=label):
                self.assert_business_failure(response, BUSINESS)

    def test_the_actual_taker_can_use_all_three(self):
        """⭐ **第二个视角**：同一张单在**乙**（真的接手人）手里三条都成功。

        判别力：若那张单其实**没有 MAIN 绩效行**（夹具坏了），上面几条会以"与被测行为无关的
        原因"变绿；本用例把它排除掉。
        """
        self.assert_ok(self.list_images(order_no=ORDER_NO_OTHERS, token=self.token_b))
        row, object_key = self.issue_and_upload(
            order_no=ORDER_NO_OTHERS, key="probe-img-b-ticket", token=self.token_b
        )
        self.assert_ok(
            self.claim(
                object_key, order_no=ORDER_NO_OTHERS, key="probe-img-b-claim", token=self.token_b
            )
        )

    def test_an_orphan_order_is_1002_on_all_three(self):
        """⭐ spec §9-14：**孤儿订单**（没有 MAIN 绩效行）⇒ 三条都 `1002`，⛔ 不是 500。"""
        self._order(self.game_a, ORDER_NO_ORPHAN, taker=None, with_main_performance=False)
        for label, response in (
            ("list", self.list_images(ORDER_NO_ORPHAN)),
            ("ticket", self.ticket(order_no=ORDER_NO_ORPHAN, key="probe-img-orphan-t")),
            ("claim", self.claim("shared-orders/x/y.jpg", order_no=ORDER_NO_ORPHAN, key="probe-img-orphan-c")),
        ):
            with self.subTest(endpoint=label):
                self.assert_business_failure(response, BUSINESS)


class ClaimReallyWritesTests(_ImagesFixture):
    """⭐ 判据打在**库里的行**上：取票落 `pending`、认领落 `ready`（⛔ 不看日志）。"""

    def test_the_ticket_creates_one_pending_row(self):
        data = self.assert_ok(self.ticket(key="probe-row-ticket"))
        self.assertEqual(set(data), TICKET_DATA_KEYS, data)
        self.assertTrue(data["upload_url"])
        self.assertTrue(data["expires_in"])
        rows = OrderScreenshot.objects.filter(order=self.order_mine)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.object_key, data["key"])
        self.assertEqual(row.status, OrderScreenshot.STATUS_PENDING)
        self.assertEqual(row.img_type, OrderScreenshot.IMG_ORDER)
        # ⭐ `uploaded_by` = **绑定账号**（本线的口径：图确实是他在传；⛔ 不是 `created_by`）。
        self.assertEqual(row.uploaded_by_id, self.taker_a.pk)

    def test_the_claim_marks_the_row_ready_with_the_real_metadata(self):
        row, object_key = self.issue_and_upload()
        data = self.assert_ok(
            self.claim(object_key, body={"key": object_key, "original_name": "测试图甲.jpg", "remark": "测试备注"})
        )
        stored = OrderScreenshot.objects.get(pk=row.pk)
        self.assertEqual(stored.status, OrderScreenshot.STATUS_READY)
        # ⭐ **真实大小**（`storage.head` 拿的，⛔ 不采信客户端上报）。
        self.assertEqual(stored.size_bytes, len(JPEG))
        # ⭐ **嗅探出来的**类型（⛔ 不是客户端给的 `Content-Type`、也不是按扩展名推的）。
        self.assertEqual(stored.content_type, "image/jpeg")
        self.assertEqual(stored.original_name, "测试图甲.jpg")
        self.assertEqual(stored.remark, "测试备注")
        self.assertIsNotNone(stored.ready_at)
        # ⭐ 返回的 "该图片项" = 列表里那一项（逐键同形，见下一条用例）。
        self.assertEqual(set(data), IMAGE_ROW_KEYS, data)
        self.assertEqual(data["size_bytes"], len(JPEG))

    def test_the_object_key_given_out_is_the_one_used(self):
        """⭐ 钉住"调用方从返回值里拿得到那个参数"（`E-0176`）：拼 URL 用的是**响应里的** key。"""
        data = self.assert_ok(self.ticket(key="probe-key-roundtrip"))
        object_key = data["key"]
        get_storage().put_bytes(object_key, JPEG, content_type="image/jpeg")
        self.assert_ok(self.claim(object_key, key="probe-key-roundtrip-claim"))
        self.assertTrue(OrderScreenshot.objects.filter(object_key=object_key).exists())


class ListOnlyShowsReadyTests(_ImagesFixture):
    """⭐ **只给已认领完成的图**（spec §5 端点表第 6 行的"只给已认领完成的图"）。"""

    def test_a_pending_row_does_not_appear_in_the_list(self):
        self.assert_ok(self.ticket(key="probe-pending-ticket"))
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_mine).count(), 1)
        data = self.assert_ok(self.list_images())
        self.assertEqual(data["results"], [], data)
        self.assertEqual(data["count"], 0)

    def test_the_row_appears_only_after_the_claim(self):
        """⭐ **同一条用例走完两段**（否则"列表恒空"也能让上一条绿）。"""
        row, object_key = self.issue_and_upload(key="probe-two-phase-ticket")
        self.assertEqual(self.assert_ok(self.list_images())["count"], 0)
        claimed = self.assert_ok(self.claim(object_key, key="probe-two-phase-claim"))
        data = self.assert_ok(self.list_images())
        self.assertEqual(data["count"], 1)
        self.assertEqual(len(data["results"]), 1)
        # ⭐ 认领回来的那一项与列表里那一项**逐键相等**（同一份装配出口）。
        self.assertEqual(claimed, data["results"][0])

    def test_a_soft_deleted_orders_images_are_not_listed(self):
        """软删单的图不进对外读面（`D-0144`）——而**单笔那条路**已经回 `1002`（上一条类）。"""
        from django.utils import timezone

        row, object_key = self.issue_and_upload(key="probe-soft-ticket")
        self.assert_ok(self.claim(object_key, key="probe-soft-claim"))
        Order.objects.filter(order_no=ORDER_NO_MINE).update(deleted_at=timezone.now())
        self.assertEqual(
            OrderScreenshot.objects.filter(order=self.order_mine, status=OrderScreenshot.STATUS_READY).count(),
            1,
        )
        self.assert_business_failure(self.list_images(), BUSINESS)

    def test_the_list_is_paginated_with_the_shared_envelope(self):
        for index in range(3):
            row, object_key = self.issue_and_upload(key=f"probe-page-ticket-{index}")
            self.assert_ok(self.claim(object_key, key=f"probe-page-claim-{index}"))
        data = self.assert_ok(self.list_images(params={"page": 1, "page_size": 2}))
        self.assertEqual(set(data), {"count", "results", "page", "page_size"}, data)
        self.assertEqual(data["count"], 3)
        self.assertEqual(len(data["results"]), 2)
        # ⚠️ 上限 100（与全批同一份实现）。
        self.assertEqual(self.assert_ok(self.list_images(params={"page_size": 1000}))["page_size"], 100)


class DomainIsSharedTests(_ImagesFixture):
    """⭐ spec §5-E：领域判据**只有一份实现**（⛔ 本线不抄第二份），用它自己的桩来证。

    ⚠️ **桩打在"被查找的那个模块"上**（`E-0029`）：两条端点分别 `from … import
    issue_screenshot_ticket` / `claim_screenshot` ⇒ 它们是在**本线模块的命名空间**里被查找的
    ⇒ 桩必须打在 `apps.takerapi.api.order_images_views` 上（⛔ 不是
    `apps.business.api.screenshot_views`，那打的是**内网**那一份查找）。
    ⚠️ 桩**不是**为了改行为：它只记"调用发生过 + 参数是什么"，然后**照常**转发给真实现
    （否则用例就变成"测桩"）。
    """

    def test_the_ticket_endpoint_goes_through_the_shared_domain_function(self):
        from apps.business.api.screenshot_views import issue_screenshot_ticket as real

        calls = []

        def spy(**kwargs):
            calls.append(kwargs)
            return real(**kwargs)

        with mock.patch(
            "apps.takerapi.api.order_images_views.issue_screenshot_ticket", side_effect=spy
        ):
            self.assert_ok(self.ticket(key="probe-spy-ticket"))
        self.assertEqual(len(calls), 1, calls)
        # ⭐ `uploaded_by` = **绑定账号**（本线的口径，见模块 docstring）。
        self.assertEqual(calls[0]["uploaded_by"], self.taker_a)
        self.assertEqual(calls[0]["img_type"], OrderScreenshot.IMG_ORDER)

    def test_the_claim_endpoint_goes_through_the_shared_domain_function(self):
        from apps.business.api.screenshot_views import claim_screenshot as real

        row, object_key = self.issue_and_upload(key="probe-spy-claim-ticket")
        calls = []

        def spy(**kwargs):
            calls.append(kwargs)
            return real(**kwargs)

        with mock.patch(
            "apps.takerapi.api.order_images_views.claim_screenshot", side_effect=spy
        ):
            self.assert_ok(self.claim(object_key, key="probe-spy-claim"))
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0]["uploaded_by"], self.taker_a)
        self.assertEqual(calls[0]["object_key"], object_key)

    def test_the_two_endpoints_are_bound_to_the_same_function_objects(self):
        """⭐ **身份判据**：本线两个名字**就是**业务层那两个函数（⛔ 不是各自的副本）。"""
        from apps.business.api.screenshot_views import (
            claim_screenshot,
            issue_screenshot_ticket,
        )
        from apps.takerapi.api import order_images_views

        self.assertIs(order_images_views.issue_screenshot_ticket, issue_screenshot_ticket)
        self.assertIs(order_images_views.claim_screenshot, claim_screenshot)


class ImageTypeAndGateTests(_ImagesFixture):
    """`img_type` 与几条**复用领域实现**的闸门（对外的答复与库都要对）。"""

    def test_img_type_one_is_1000_and_writes_nothing(self):
        payload = self.assert_business_failure(
            self.ticket(body={"ext": "jpg", "img_type": 1}, key="probe-qr"), VALIDATION
        )
        self.assertEqual(payload["msg"], IMG_TYPE_MSG)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_mine).count(), 0)

    def test_a_bad_extension_is_1000_and_writes_nothing(self):
        payload = self.assert_business_failure(
            self.ticket(body={"ext": "gif"}, key="probe-bad-ext"), VALIDATION
        )
        self.assertEqual(payload["msg"], BAD_EXT_MSG)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_mine).count(), 0)

    def test_a_missing_key_on_claim_is_1000(self):
        self.assert_business_failure(self.claim("", body={}, key="probe-no-key"), VALIDATION)

    def test_an_unknown_key_says_there_is_nothing_to_claim(self):
        payload = self.assert_business_failure(
            self.claim("shared-orders/nope/none.jpg", key="probe-unknown-key"), VALIDATION
        )
        self.assertEqual(payload["msg"], NO_PENDING_MSG)

    def test_a_non_image_body_is_rejected_and_the_row_is_deleted(self):
        """⭐ 魔数认不出 ⇒ `1000` + **那一行被删掉**（领域实现的分内事，这里核"库与答复都对"）。"""
        row, object_key = self.issue_and_upload(body=NOT_AN_IMAGE, key="probe-bad-magic-ticket")
        payload = self.assert_business_failure(
            self.claim(object_key, key="probe-bad-magic-claim"), VALIDATION
        )
        self.assertEqual(payload["msg"], BAD_EXT_MSG)
        self.assertFalse(OrderScreenshot.objects.filter(pk=row.pk).exists())
        # ⚠️ 对象也真的没了（领域实现删的；判据打在 storage 上）。
        self.assertIsNone(get_storage().head(object_key))

    def test_an_empty_object_is_rejected_and_the_row_is_deleted(self):
        row, object_key = self.issue_and_upload(body=b"", key="probe-empty-ticket")
        payload = self.assert_business_failure(
            self.claim(object_key, key="probe-empty-claim"), VALIDATION
        )
        self.assertEqual(payload["msg"], EMPTY_MSG)
        self.assertFalse(OrderScreenshot.objects.filter(pk=row.pk).exists())

    def test_the_twentieth_row_is_the_limit(self):
        """第 21 张 ⇒ `1000` + **没有新行**（每单 20 张，领域实现的分内事）。"""
        for index in range(20):
            self.assert_ok(self.ticket(key=f"probe-limit-ticket-{index:02d}"))
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_mine).count(), 20)
        self.assert_business_failure(self.ticket(key="probe-limit-21"), VALIDATION)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_mine).count(), 20)


class IdempotencyTests(_ImagesFixture):
    """⭐ 幂等：两条写端点各自"同键重发 ⇒ 回放、副作用只发生一次"；缺键 ⇒ `1000`。"""

    def test_the_ticket_replays_and_writes_only_one_row(self):
        first = self.assert_ok(self.ticket(key="probe-idem-ticket"))
        second = self.assert_ok(self.ticket(key="probe-idem-ticket"))
        # ⚠️ 判据打在**解析后的载荷**上（快照是 JSONB、不保键序 ⇒ ⛔ 不断字节）。
        self.assertEqual(second, first)
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order_mine).count(), 1)

    def test_the_claim_replays_after_the_object_arrives(self):
        key = "probe-idem-claim"
        data = self.assert_ok(self.ticket(key="probe-idem-claim-ticket"))
        object_key = data["key"]
        # ⚠️ 第一次认领**在对象还没上传时**会回 `1000`「文件还没上传成功」，而按既有口径
        #    那一档**不**声明副作用已落库 ⇒ 占位行回滚 ⇒ 同一颗键**下次可以重试**。
        self.assert_business_failure(self.claim(object_key, key=key), VALIDATION)
        get_storage().put_bytes(object_key, JPEG, content_type="image/jpeg")
        first = self.assert_ok(self.claim(object_key, key=key))
        second = self.assert_ok(self.claim(object_key, key=key))
        self.assertEqual(second, first)
        self.assertEqual(
            OrderScreenshot.objects.filter(order=self.order_mine, status=OrderScreenshot.STATUS_READY).count(),
            1,
        )

    def test_a_missing_idempotency_key_is_1000_on_both_write_endpoints(self):
        data = self.assert_ok(self.ticket(key="probe-missing-key-ticket"))
        for label, response in (
            ("ticket", self.ticket(key=None)),
            ("claim", self.claim(data["key"], key=None)),
        ):
            with self.subTest(endpoint=label):
                self.assert_business_failure(response, VALIDATION)


class AssemblyAndAuthTests(_ImagesFixture):
    """⭐ 装配（spec §9-15）＋ 鉴权面（spec §9-2，在**真路由**上打）。"""

    def test_the_three_views_are_assembled_exactly(self):
        from apps.takerapi.api.order_images_views import (
            TakerOrderImageTicketView,
            TakerOrderImagesView,
        )
        from apps.takerapi.authentication import TakerAppBearerAuthentication
        from apps.takerapi.throttling import TakerAppFallbackThrottle

        for view in (TakerOrderImagesView, TakerOrderImageTicketView):
            with self.subTest(view=view.__name__):
                self.assertEqual(view.authentication_classes, [TakerAppBearerAuthentication])
                self.assertEqual(view.permission_classes, [])
                self.assertEqual(view.throttle_classes, [TakerAppFallbackThrottle])

    def test_no_credential_is_401_on_all_three(self):
        paths = (
            ("list", "get", IMAGES_TEMPLATE.format(order_no=ORDER_NO_MINE)),
            ("ticket", "post", TICKET_TEMPLATE.format(order_no=ORDER_NO_MINE)),
            ("claim", "post", IMAGES_TEMPLATE.format(order_no=ORDER_NO_MINE)),
        )
        for label, method, path in paths:
            with self.subTest(endpoint=label):
                if method == "get":
                    response = self.anonymous.get(path, REMOTE_ADDR=SOURCE_IP)
                else:
                    response = self.anonymous.post(
                        path, data="{}", content_type="application/json", REMOTE_ADDR=SOURCE_IP
                    )
                self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])

    def test_an_openapi_token_is_rejected(self):
        api_app = self.make_api_app()
        token = self.issue_token(api_app, path=OPENAPI_TOKEN_PATH)
        # (占位：本用例只判"认证面"——上面那句原本是想断言业务失败，去掉。)
        response = self.list_images(token=token)
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])
        self.assertEqual(self.body(response)["code"], UNAUTHENTICATED)

    def test_a_session_request_is_rejected(self):
        session = make_logged_in_client(
            domain=self.domain_name, username="images-session-user", games=None
        )
        response = session.get(IMAGES_TEMPLATE.format(order_no=ORDER_NO_MINE))
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])

    def test_a_taker_token_is_rejected_by_the_openapi_route(self):
        response = self.anonymous.get(
            OPENAPI_ORDERS_PATH,
            HTTP_AUTHORIZATION=f"Bearer {self.token_a}",
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])

    def test_the_control_group_a_valid_token_works(self):
        """⭐ 对照组（⛔ 缺了它，上面几条对"什么都拒"的实现也会绿）。"""
        self.assert_ok(self.list_images(params={"page": 1}))


class MalformedInputTests(_ImagesFixture):
    """㊙ **`test_no_route_500` 的已知盲区**：带路径转换器的路由不在那条腿的覆盖内。"""

    def test_a_nul_in_the_order_no_is_404_not_500(self):
        for label, path in (
            ("list", "/api/taker/v1/orders/2026%00 1/images/"),
            ("ticket", "/api/taker/v1/orders/2026%00 1/images/ticket/"),
        ):
            with self.subTest(endpoint=label):
                if label == "list":
                    response = self.anonymous.get(
                        path,
                        HTTP_AUTHORIZATION=f"Bearer {self.token_a}",
                        REMOTE_ADDR=SOURCE_IP,
                    )
                else:
                    response = self._post(path, {"ext": "jpg"}, key="probe-nul-ticket")
                self.assertEqual(response.status_code, NOT_FOUND, response.content[:200])

    def test_a_long_order_no_is_1002_not_500(self):
        """**天文数字**那一格：120 字符的单号 ⇒ `1002`（⛔ 不是 500、也⛔ 不是 404）。

        ⚠️ 为什么是 **120** 而⛔ 不是 300：两条写端点套着幂等入口，而那个入口有一条
        **整条路径**的列宽闸（台账的 `path` 列 = 191，超长回 `1000`）⇒ 300 字符的单号
        **到不了**业务判据（下面两条单列钉住这个更靠前的闸）。
        """
        self.assert_business_failure(self.list_images("9" * 120), BUSINESS)
        self.assert_business_failure(
            self.ticket(order_no="9" * 120, key="probe-long-ticket"), BUSINESS
        )
        self.assert_business_failure(
            self.claim("shared-orders/x/y.jpg", order_no="9" * 120, key="probe-long-claim"),
            BUSINESS,
        )

    def test_an_over_long_path_is_1000_not_500(self):
        payload = self.assert_business_failure(
            self.ticket(order_no="9" * 300, key="probe-overlong-path"), VALIDATION
        )
        self.assertIn("191", payload["msg"], payload)

    def test_non_object_bodies_do_not_500(self):
        for raw in ("[]", '"x"', "123"):
            with self.subTest(raw=raw):
                self.assert_business_failure(
                    self.ticket(key=f"probe-body-{raw[0]}", raw=raw), VALIDATION
                )

    def test_an_over_long_claim_key_is_1000(self):
        """`key` 的界 = `OrderScreenshot.object_key` 的**列宽 255**（超长 ⇒ `1000`，⛔ 不是 500）。"""
        self.assert_business_failure(
            self.claim("k" * 256, key="probe-long-key"), VALIDATION
        )

    def test_the_paths_require_a_trailing_slash(self):
        for label, path in (
            ("list", f"/api/taker/v1/orders/{ORDER_NO_MINE}/images"),
            ("ticket", f"/api/taker/v1/orders/{ORDER_NO_MINE}/images/ticket"),
        ):
            with self.subTest(endpoint=label):
                if label == "list":
                    response = self.anonymous.get(
                        path,
                        HTTP_AUTHORIZATION=f"Bearer {self.token_a}",
                        REMOTE_ADDR=SOURCE_IP,
                    )
                else:
                    response = self._post(path, {"ext": "jpg"}, key="probe-no-slash")
                self.assertNotEqual(response.status_code, 200)
