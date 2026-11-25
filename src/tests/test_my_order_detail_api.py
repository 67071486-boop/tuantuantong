"""booster-h5 T1：打手受限订单详情 `GET /api/business/my-orders/<order_no>/detail/`
（spec `2026-10-10-booster-h5-m1-design.md` §6.2 / §8.1）。

六组判据（§8.1 逐条对应）：

1. 正常 200 且**键集恰好**等于 §6.2 的 16 键（`assertEqual(sorted(...), [...])`，
   ⛔ 不用 `toContain` 放宽那种写法）。
2. 非接单人（别人的单）⇒ 403；撤掉 `my_orders.send_to_my_system` 节点的账号 ⇒ 403（类级门）。
3. 单不存在（含软删）⇒ **200 + `1002`**（本仓信封约定：`Http404` 被归一化成业务结果，
   见 `apps/core/exceptions.py`）；仅非 MAIN 绩效行 ⇒ 403；含控制字符的单号 ⇒ 路由层 404。
4. **跨租户否定**：以租户 A 打手凭据访问租户 B 的单号必须失败——判据用**单号值**
   （租户内唯一、两租户取值不同），⛔ 不用主键（`AGENTS.md` §5-G：两个 schema 的
   主键序列各自从 1 开始）。照 `tests/test_p3b_isolation.py` 的手法。
5. **群码字段**：有 `img_type=1` 已认领行 ⇒ `group_qr_url` 是签名 URL（断言生成件
   `get_storage().presign_get` 收到的**正是那张群码行的 object_key**）；无 ⇒ `null`；
   加一张 `img_type=0` 的行 ⇒ 仍 `null`（⛔ 不串）。
6. **F1 核实点**（spec §9）：上游镜像单被本租户打手抢下后，`my-orders` 列表与 detail
   端点是否同原生单一样工作——**用测试实证**（夹具照
   `tests/test_jidanpu_upstream_hall.py::UpstreamHallClaimEndpointTests` 的形态）。

⚠️ 执行期发现（与任务书 / spec §6.2 原句的差异，已按实测钉死并在 spec §6.2 追加更正）：
共享转换器 `<order_no:order_no>`（`apps/business/converters.py`）**只挡控制字符、
不强制 20 位数字**——「形状不符但无控制字符」的单号（如 `abc`）会进视图、落进
「记录不存在」那个 200 + 1002 出口；**只有含控制字符**（如 `%00`）才在路由层 404。
20 位形状闸在前端路由（spec §4.1 的 `:orderNo(\\d{20})`），后端不重复这道闸。

⚠️ 断言纪律（照 `tests/test_my_orders_api.py`）：期望值一律**字面值**（E-0024）；
单号全零段合成（E-0077）；登录夹具走 `make_logged_in_client`（真实登录端点，
`User.objects.create_user` + `client.login()` 的会话没有 `identity_plane` 会 401）。
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business import jidanpu
from apps.business.models import (
    EmpType,
    Game,
    GameServer,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    Order,
    OrderStatus,
    OrderScreenshot,
    Performance,
)
from apps.core.storage import (
    Presigned,
    build_order_key,
    build_shared_order_key,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

#: 类级节点门与列表同一颗（`MyOrdersViewSet.MY_ORDERS_PERMISSIONS["detail"]`）。
MY_ORDERS_NODES = ("my_orders.send_to_my_system",)

#: 上游接单那一跳的端点（与 `jidanpu_upstream.REC_ORDER_PATH` 同值，字面量照抄先例）。
REC_ORDER_PATH = "/OrderAjax/RecOrderByInternal"

# ── 判据用的业务值（合成，租户内唯一且两租户取值不同；**不是主键**）────────
NO_A = "10000000000000000001"          # taker_a 接的单（主夹具）
NO_B = "10000000000000000002"          # taker_b 接的单（「别人的单」那一半）
NO_MISSING = "10000000000000000009"    # 不存在的 20 位单号
NO_SOFT_DELETED = "10000000000000000003"
NO_SALES_ONLY = "10000000000000000004"  # 只有 SALES 绩效行的单
GAME_A = "测试游戏甲"
SERVER_A = "测试区服甲"
PROJECT_A = "深渊满星十二层"
TEMP_A = Decimal("88.00")

#: §6.2 的 16 键 + D-0301 加的 `source_order_no` = **17 键**（sorted；
#: ⛔ 不许 contains 放宽）。
DETAIL_KEYS = [
    "claimed_at",
    "content",
    "demand",
    "game_account",
    "game_name",
    "game_role_name",
    "game_sub_account",
    "group_qr_url",
    "is_urgent",
    "order_no",
    "project_text",
    "remark",
    "server_name",
    "source_order_no",
    "status",
    "temp_amount",
    "time_requirement",
]


def _url(order_no):
    return f"/api/business/my-orders/{order_no}/detail/"


class _UpstreamStubClient:
    """SDK 边界替身（照 `test_jidanpu_upstream_hall.py` 的 `_StubClient` 形态，
    只服务 `RecOrderByInternal`；`last_envelope` 是接单那一跳回读的信封，⛔ 不能少——
    少了它 `call` 之后的判据读不到，会落成 1017「暂时无法接单」）。
    ⚠️ `paths` 从**真实记录的调用**里取（与先例同款）：写死一个列表的话
    「上游恰好被调一次」就是恒真断言（§5-G：断言要钉值，不钉常量）。"""

    def __init__(self, *, claim_code=1, claim_msg="接单成功，请到「我的订单」里查看订单资料"):
        self.claim_code = claim_code
        self.claim_msg = claim_msg
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {}

    def ensure_login(self):
        pass

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, dict(kwargs)))
        if path == REC_ORDER_PATH:
            self.last_envelope = {
                "code": self.claim_code, "msg": self.claim_msg, "data": "",
            }
            return ""
        raise AssertionError(f"未预期的端点：{method} {path}")

    @property
    def paths(self) -> list[str]:
        return [path for _method, path, _kwargs in self.calls]


class _UpstreamClientCtx:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *_exc):
        return False


class MyOrderDetailTests(TenantTestCase):
    """§8.1 第 1 / 2 / 3 项：键集、准入、404/403 的出口语义。"""

    def setUp(self):
        self.game = Game.objects.create(name=GAME_A)
        self._clients = {}
        self.client = make_logged_in_client(
            username="测试打手甲", groups=("代练员",), nodes=MY_ORDERS_NODES
        )
        self._clients["测试打手甲"] = self.client
        self.taker_a = User.objects.get(username="测试打手甲")
        self._login("代练员", username="测试打手乙")
        self.taker_b = User.objects.get(username="测试打手乙")
        # 别人的单：taker_b 接的 NO_B（「非接单人 403」那一半的反面素材）。
        self.perf_b = self._perf(NO_B, self.taker_b, Decimal("66.00"))
        # 主夹具：taker_a 接的 NO_A，订单字段全部给**合成字面值**（E-0024：断值不断常量）。
        self.server = GameServer.objects.create(game=self.game, name=SERVER_A)
        self.order_a = Order.objects.create(
            game=self.game,
            server=self.server,
            order_no=NO_A,
            status=OrderStatus.IN_PROGRESS,
            project_text=PROJECT_A,
            content="合成客户原始需求：十二层满星后截图",
            demand="合成需求描述：打手看的这一段",
            time_requirement="48小时",
            game_account="synth-account-a",
            game_sub_account="synth-sub-account-a",
            game_role_name="synth-role-a",
            is_urgent=True,
            remark="合成重要备注：晚班交接",
        )
        self.perf_a = Performance.objects.create(
            order=self.order_a,
            emp_type=EmpType.MAIN,
            user=self.taker_a,
            temp_amount=TEMP_A,
        )
        # 上面那次 `_login` 把 self.client 换成了别人；测试默认身份回到打手甲。
        self.client = self._clients["测试打手甲"]

    def _login(self, *groups, username, nodes=MY_ORDERS_NODES):
        """以 `username` 登录（同一个号在一个用例内只建一次，照 my-orders 测试的缓存法）。"""
        cached = self._clients.get(username)
        if cached is not None:
            self.client = cached
            return User.objects.get(username=username)
        self.client = make_logged_in_client(
            username=username, groups=groups, nodes=nodes
        )
        self._clients[username] = self.client
        return User.objects.get(username=username)

    def _perf(self, no, user, price):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal(price),
        )
        return Performance.objects.create(
            order=order,
            emp_type=EmpType.MAIN,
            user=user,
            temp_amount=Decimal(price),
        )

    def _body(self, order_no=NO_A):
        return self.client.get(_url(order_no)).json()

    # ── §8.1-1：正常 200 且键集恰好 16 键 ─────────────────────────
    def test_key_set_is_exactly_the_sixteen_fields(self):
        response = self.client.get(_url(NO_A))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0)
        data = body["data"]
        self.assertEqual(
            sorted(data),
            DETAIL_KEYS,
            "键集与 §6.2 的 16 键不符（多了或少了都算失败）",
        )

    def test_payload_values_come_from_the_order_and_performance(self):
        """16 键的**值**来源逐格钉死：订单列 + 绩效行（`claimed_at`/`temp_amount`）。"""
        data = self._body()["data"]
        self.assertEqual(data["order_no"], NO_A)
        self.assertEqual(data["status"], OrderStatus.IN_PROGRESS)   # 订单状态，非绩效审核态
        self.assertEqual(data["game_name"], GAME_A)
        self.assertEqual(data["server_name"], SERVER_A)             # 有区服 ⇒ 区服名
        self.assertEqual(data["project_text"], PROJECT_A)
        self.assertEqual(data["content"], "合成客户原始需求：十二层满星后截图")
        self.assertEqual(data["demand"], "合成需求描述：打手看的这一段")
        self.assertEqual(data["time_requirement"], "48小时")
        self.assertEqual(data["game_account"], "synth-account-a")
        self.assertEqual(data["game_sub_account"], "synth-sub-account-a")
        self.assertEqual(data["game_role_name"], "synth-role-a")
        self.assertIs(data["is_urgent"], True)
        self.assertEqual(data["remark"], "合成重要备注：晚班交接")
        # `claimed_at` = 绩效行 `created_at`（与列表「接手时间」同轴，spec §2.3/§6.2）。
        # ⚠️ 时区口径：DRF `DateTimeField` 按项目时区序列化（+08:00 的 ISO 串），
        #    与列表 `MyOrderSerializer.created_at` **同一份口径**——所以断言两件：
        #    ① 与绩效行的时刻**相等**（解析后比时刻，不比字符串的时区写法）；
        #    ② 与**列表端点**对同一行序列化出的 `created_at` **逐字相等**（同轴的出口判据）。
        from datetime import datetime as _dt

        self.assertEqual(_dt.fromisoformat(data["claimed_at"]), self.perf_a.created_at)
        listed = self.client.get("/api/business/my-orders/").json()
        self.assertEqual(listed["code"], 0)
        row = next(r for r in listed["data"]["items"] if r["order_no"] == NO_A)
        self.assertEqual(data["claimed_at"], row["created_at"])
        # `temp_amount` 字符串口径与 `MyOrderSerializer` 一致（DecimalField → str）。
        self.assertEqual(data["temp_amount"], "88.00")

    def test_order_without_server_reports_server_name_as_null(self):
        """`Order.server` 可空 ⇒ 无区服的单 `server_name` 是 `null`（键必须在，
        ⛔ 不许像缺 `allow_null` 那样让键从载荷里静默消失——见序列化器 docstring）。"""
        self._login("代练员", username="测试打手乙")     # NO_B 是打手乙接的单
        data = self._body(NO_B)["data"]
        self.assertIn("server_name", data)
        self.assertIsNone(data["server_name"])

    def test_non_urgent_order_reports_is_urgent_as_false(self):
        """`is_urgent` 两个取值各断一次（上面断过 `True`）：不加急的单回 `False`。"""
        self._login("代练员", username="测试打手乙")
        data = self._body(NO_B)["data"]
        self.assertIs(data["is_urgent"], False)

    # ── §8.1-2：谁可以看 ──────────────────────────────────────────
    def test_another_taker_claiming_the_order_gets_403(self):
        """单存在但不是当前登录人接的 ⇒ 403（msg 参照 `/submit` 的句式）。"""
        self._login("代练员", username="测试打手乙")
        response = self.client.get(_url(NO_A))
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["code"], 403)                 # 传输层码，不是业务码
        self.assertEqual(body["msg"], "该单不是你接的，不能查看详情")
        # 反向对照（非空哨兵）：接单人自己看同一张单是 200。
        self.assertEqual(
            self._clients["测试打手甲"].get(_url(NO_A)).status_code, 200
        )

    def test_account_without_the_node_gets_403(self):
        """撤掉 `my_orders.send_to_my_system` ⇒ 类级节点门 403（与列表同一颗）。"""
        self._login("无节点组", username="无节点打手甲", nodes=())
        response = self.client.get(_url(NO_A))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)

    def test_anonymous_is_rejected(self):
        from django.test import Client

        response = Client(SERVER_NAME="tenant.test.com").get(_url(NO_A))
        self.assertEqual(response.status_code, 401)

    # ── §8.1-3：不存在 / 软删 / 仅非 MAIN / 形状不符 ────────────────
    def test_missing_order_is_a_business_not_found(self):
        """单不存在 ⇒ **200 + 1002**（本仓信封约定：`Http404` 归一化成业务结果，
        与 `/submit`、截图端点同一出口；msg 是全仓统一的「记录不存在或已被删除」）。"""
        response = self.client.get(_url(NO_MISSING))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 1002)                # 字面值（E-0024）
        self.assertEqual(body["msg"], "记录不存在或已被删除")
        self.assertIsNone(body["data"])

    def test_soft_deleted_order_is_a_business_not_found(self):
        """软删单走默认 manager 取不到（D-0144 常规读面口径）⇒ 与「不存在」同出口。"""
        order = self._perf(NO_SOFT_DELETED, self.taker_a, Decimal("30.00")).order
        Order.objects.filter(pk=order.pk).update(
            deleted_at=datetime(2026, 10, 10, tzinfo=dt_timezone.utc)
        )
        # 自证前提：软删真的写进去了（默认 manager 已取不到这一行）。
        self.assertFalse(Order.objects.filter(pk=order.pk).exists())
        response = self.client.get(_url(NO_SOFT_DELETED))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 1002)
        self.assertIsNone(body["data"])

    def test_order_with_only_a_sales_row_for_me_is_403(self):
        """单存在、我在上面只有 `销售提成`（非 MAIN）行 ⇒ 403：判据是**接单人身份**，
        不是「我在这单上有绩效」（与列表 `emp_type=MAIN` 同轴的反例构造）。"""
        order = Order.objects.create(
            game=self.game, order_no=NO_SALES_ONLY, status=OrderStatus.IN_PROGRESS
        )
        Performance.objects.create(
            order=order,
            emp_type=EmpType.SALES,
            user=self.taker_a,
            temp_amount=Decimal("7.00"),
        )
        # 自证前提：这张单**没有** MAIN 行、我确实只有 SALES 行。
        self.assertFalse(
            Performance.objects.filter(
                order=order, emp_type=EmpType.MAIN
            ).exists()
        )
        response = self.client.get(_url(NO_SALES_ONLY))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["msg"], "该单不是你接的，不能查看详情")

    def test_unclaimed_order_is_403_for_everyone(self):
        """单存在但**没有任何接单人**（无 MAIN 行）⇒ 对打手同样是 403
        （「没有 MAIN 行」与「MAIN 行不是我的」共用同一出口，见视图 docstring）。"""
        no = "10000000000000000005"
        Order.objects.create(game=self.game, order_no=no, status=OrderStatus.UNASSIGNED)
        response = self.client.get(_url(no))
        self.assertEqual(response.status_code, 403)

    def test_shape_mismatch_without_control_chars_falls_into_not_found(self):
        """⚠️ 实测语义（spec §6.2 原句「不合形状 404」的更正，见模块 docstring）：
        共享转换器只挡控制字符——非 20 位但干净的串会进视图、落「记录不存在」出口。"""
        for malformed in ("abc", "1000000000000000001"):   # 字母 / 19 位
            response = self.client.get(_url(malformed))
            self.assertEqual(
                response.status_code, 200, malformed
            )
            self.assertEqual(response.json()["code"], 1002, malformed)

    def test_control_characters_in_the_segment_are_a_route_level_404(self):
        """含控制字符（如 `%00`=NUL）的单号被共享转换器挡在路由层 ⇒ **404**
        （Django 解析层，不进视图——`apps/business/converters.py` 的选定形状）。"""
        response = self.client.get("/api/business/my-orders/abc%00def/detail/")
        self.assertEqual(response.status_code, 404)


class MyOrderDetailGroupQrTests(TenantTestCase):
    """§8.1-5：`group_qr_url` 三态（有群码 / 无 / 只有进度截图）+ 生成件同源。"""

    def setUp(self):
        self.game = Game.objects.create(name=GAME_A)
        self.client = make_logged_in_client(
            username="测试打手甲", groups=("代练员",), nodes=MY_ORDERS_NODES
        )
        self.taker_a = User.objects.get(username="测试打手甲")
        self.order = Order.objects.create(
            game=self.game,
            order_no=NO_A,
            status=OrderStatus.IN_PROGRESS,
        )
        self.perf = Performance.objects.create(
            order=self.order,
            emp_type=EmpType.MAIN,
            user=self.taker_a,
            temp_amount=TEMP_A,
        )

    def _qr_row(self, *, order=None, random_part, status=OrderScreenshot.STATUS_READY):
        """造一张群聊二维码（`img_type=1`）截图行；对象名走既有 builder（合成随机段）。"""
        return OrderScreenshot.objects.create(
            order=order or self.order,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            object_key=build_order_key(
                "test", (order or self.order).order_no, "png",
                random_part=random_part, kind="qr",
            ),
            uploaded_by=self.taker_a,
            status=status,
        )

    def _data(self):
        body = self.client.get(_url(NO_A)).json()
        self.assertEqual(body["code"], 0)
        return body["data"]

    def test_group_qr_row_yields_a_signed_url(self):
        """有已认领的群码行 ⇒ `group_qr_url` 非空，且与本仓**既有**截图读路径同源：
        local 后端的 `presign_get` 产出 `/api/media/blob/<key>`（与
        `ScreenshotCollectionView.list_items` 的其它调用点同一形状）。"""
        row = self._qr_row(random_part="000000000001")
        data = self._data()
        self.assertEqual(data["group_qr_url"], f"/api/media/blob/{row.object_key}")

    def test_the_url_comes_from_presign_get_called_with_the_qr_object_key(self):
        """生成件同源的**判别力**版本：对 `my_orders_views` 查找的 `get_storage` 打桩，
        断言 `presign_get` 收到的**恰好是那张群码行的 object_key**（E-0029：桩打在
        被查找的模块）。同一张单还放着一张 `img_type=0` 的进度截图 ⇒ 串行也会红。"""
        qr = self._qr_row(random_part="000000000011")
        OrderScreenshot.objects.create(
            order=self.order,
            img_type=OrderScreenshot.IMG_ORDER,
            object_key=build_shared_order_key(
                NO_A, "png", random_part="000000000012"
            ),
            uploaded_by=self.taker_a,
            status=OrderScreenshot.STATUS_READY,
        )
        fake = mock.MagicMock(name="storage")
        fake.presign_get.return_value = Presigned(
            url="https://qr-synth.test.invalid/signed", expires_in=300
        )
        with mock.patch(
            "apps.business.api.my_orders_views.get_storage", return_value=fake
        ) as seam:
            data = self._data()
        self.assertTrue(seam.called, "detail 没有经过 get_storage 生成件")
        fake.presign_get.assert_called_once_with(qr.object_key)
        self.assertEqual(data["group_qr_url"], "https://qr-synth.test.invalid/signed")

    def test_no_screenshot_rows_yield_null(self):
        """没有任何截图行 ⇒ `group_qr_url` 是 `null`（键必须在）。"""
        data = self._data()
        self.assertIn("group_qr_url", data)
        self.assertIsNone(data["group_qr_url"])

    def test_only_progress_screenshots_yield_null(self):
        """只有 `img_type=0`（进度截图）的行 ⇒ 仍 `null`（⛔ 绝不把进度截图串成群码）。"""
        OrderScreenshot.objects.create(
            order=self.order,
            img_type=OrderScreenshot.IMG_ORDER,
            object_key=build_shared_order_key(NO_A, "png", random_part="000000000021"),
            uploaded_by=self.taker_a,
            status=OrderScreenshot.STATUS_READY,
        )
        # 自证前提：这张单确实有一张**已认领**的进度截图（否则「null」可能只是没建成）。
        self.assertTrue(
            OrderScreenshot.objects.filter(
                order=self.order,
                img_type=OrderScreenshot.IMG_ORDER,
                status=OrderScreenshot.STATUS_READY,
            ).exists()
        )
        data = self._data()
        self.assertIsNone(data["group_qr_url"])

    def test_pending_qr_row_alone_yields_null(self):
        """`pending`（还没认领完）的群码行不算数——判据与列表读路径同款：只出 `ready`。"""
        self._qr_row(random_part="000000000031", status=OrderScreenshot.STATUS_PENDING)
        data = self._data()
        self.assertIsNone(data["group_qr_url"])

    def test_multiple_qr_rows_pick_the_latest_one(self):
        """⭐ **2026-10-25 改准**（`uniq_order_ready_group_qr` 部分唯一索引）：每单最多一张
        `ready` 群码 ⇒ 「多张 ready 取 pk 最大」这个场景**已不可能发生**。

        这里改钉两件事：① 写第二张 `ready` 会被库约束拒绝（不变量真的生效）；
        ② 一张 `ready` + 一张更大的**非 ready**行时，详情仍取那张 `ready`（⛔ 不按 pk 乱取）。"""
        ready = self._qr_row(random_part="000000000041")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._qr_row(random_part="000000000042")
        stale = self._qr_row(random_part="000000000043", status=OrderScreenshot.STATUS_PENDING)
        self.assertGreater(stale.pk, ready.pk)             # 自证前提：pk 顺序就是构造顺序
        data = self._data()
        self.assertEqual(data["group_qr_url"], f"/api/media/blob/{ready.object_key}")


class MyOrderDetailCrossTenantTests(TenantTestCase):
    """§8.1-4：跨租户否定（照 `tests/test_p3b_isolation.py` 的手法与判据纪律）。"""

    DOMAIN_A = "tenant.test.com"
    DOMAIN_B = "bst5-h5-iso-b.test.local"      # `.test.local` 后缀在 .env 白名单里
    SCHEMA_B = "bst5_h5_iso_b"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=cls.SCHEMA_B, name="booster-h5 详情隔离租户 B", domain=cls.DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回租户 A（跨域名请求不还原 schema，见 p3b 的模块说明）。
        connection.set_tenant(self.tenant)

        game_a = Game.objects.create(name=GAME_A)
        self.taker_a = make_logged_in_client(
            domain=self.DOMAIN_A, username="测试打手甲", groups=("代练员",),
            nodes=MY_ORDERS_NODES,
        )
        order_a = Order.objects.create(
            game=game_a, order_no=NO_A, status=OrderStatus.IN_PROGRESS,
        )
        Performance.objects.create(
            order=order_a, emp_type=EmpType.MAIN,
            user=User.objects.get(username="测试打手甲"), temp_amount=TEMP_A,
        )

        try:
            with tenant_context(self.tenant_b):
                # B 侧同形夹具：B 的打手 + B 的单（单号与 A 取值不同 ⇒ 判据来源）。
                game_b = Game.objects.create(name="仅 B 的游戏")
                self.taker_b = make_logged_in_client(
                    domain=self.DOMAIN_B, username="测试打手乙", groups=("代练员",),
                    nodes=MY_ORDERS_NODES,
                )
                order_b = Order.objects.create(
                    game=game_b, order_no=NO_B, status=OrderStatus.IN_PROGRESS,
                )
                Performance.objects.create(
                    order=order_b, emp_type=EmpType.MAIN,
                    user=User.objects.get(username="测试打手乙"),
                    temp_amount=Decimal("66.00"),
                )
        finally:
            connection.set_tenant(self.tenant)

    def _get_b(self, order_no):
        """在 B 的域名上发请求，结束后把连接钉回 A（同 p3b 的 `_get_b`）。"""
        try:
            return self.taker_b.get(_url(order_no), SERVER_NAME=self.DOMAIN_B)
        finally:
            connection.set_tenant(self.tenant)

    def test_a_taker_cannot_read_b_order_no(self):
        """A 的打手拿 **B 的单号**在 A 域名上打详情 ⇒ 失败（那张单在 A 的 schema 里
        不存在 ⇒ 业务码 1002，`data=None`）。正向对照：同一凭据看 A 自己的单是 200。"""
        own = self.taker_a.get(_url(NO_A), SERVER_NAME=self.DOMAIN_A)
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.json()["code"], 0)

        crossed = self.taker_a.get(_url(NO_B), SERVER_NAME=self.DOMAIN_A)
        body = crossed.json()
        self.assertEqual(body["code"], 1002, body)         # 判据：单号值，⛔ 不是主键
        self.assertIsNone(body["data"])

    def test_b_taker_sees_own_order_but_not_a_order_no(self):
        """反方向：B 的打手看得到 B 自己的单（非空哨兵），拿 A 的单号 ⇒ 1002。"""
        own = self._get_b(NO_B)
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.json()["code"], 0)

        crossed = self._get_b(NO_A)
        body = crossed.json()
        self.assertEqual(body["code"], 1002, body)
        self.assertIsNone(body["data"])


class UpstreamMirrorClaimThenMyOrderDetailTests(TenantTestCase):
    """§8.1-6 / F1：上游镜像单被本租户打手抢下后，列表与 detail 是否同原生一样工作。

    ⚠️ **实证用例**（E-0004：不许推断）：夹具照
    `tests/test_jidanpu_upstream_hall.py::UpstreamHallClaimEndpointTests` 的形态
    （绑定 + 镜像行 + `jidanpu.open_client` 桩），真实走一遍
    `POST /api/business/hall/upstream/<pk>/claim/`，再对**响应载荷里的 `order_no`**
    （⛔ 不是数据库对象拼 URL——E-0176）打列表与 detail。
    """

    BASE_URL = "https://jidanpu-upstream.test.invalid"
    ACCOUNT = "kefu-up-bst5"
    PASSWORD = "pw-canary-bst5"
    REMOTE_ID = "20261010000000000001"       # 合成上家单号（全零段）
    BASE_TIME = datetime(2026, 10, 10, 8, 0, tzinfo=dt_timezone.utc)

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.owner_user = User.objects.create_user(
            username=self.ACCOUNT, password=self.PASSWORD, first_name="测试客服甲"
        )
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.owner_user, jidanpu_base_url=self.BASE_URL,
            account=self.ACCOUNT, password=self.PASSWORD, enabled=True,
        )
        self.client = make_logged_in_client(
            username="测试打手甲", groups=("代练员",), games=[self.game],
            nodes=("grab_hall.take_order",) + MY_ORDERS_NODES,
        )
        self.taker = User.objects.get(username="测试打手甲")
        self.mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=self.BASE_URL,
            upstream_order_id=self.REMOTE_ID,
            owner_binding=self.binding,
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game,
            project_text="合成项目",
            remote_remark="合成备注",
            remote_role_name="合成角色",
            remote_rec_fee=Decimal("88.00"),
            upstream_payload={
                "orderId": self.REMOTE_ID, "orderContent": "合成需求",
                "orderRemark": "合成备注", "roleName": "合成角色",
                "gameAcc": "acc-synth", "zgameAcc": "sub-synth", "recFee": "88.00",
            },
            first_seen_at=self.BASE_TIME,
        )

    def test_claimed_mirror_order_flows_into_my_orders_list_and_detail(self):
        stub = _UpstreamStubClient()
        with mock.patch.object(
            jidanpu, "claim_session", lambda _b: _UpstreamClientCtx(stub)
        ):
            claim = self.client.post(
                f"/api/business/hall/upstream/{self.mirror.pk}/claim/"
            )
        body = claim.json()
        self.assertEqual(body["code"], 0, body)
        # ⭐ 判据从**调用方真正看到的载荷**里取 `order_no`（E-0176），⛔ 不拿对象拼。
        order_no = body["data"]["order_no"]
        # 上游恰好被调一次、且恰好是接单那一跳（`paths` 从真实记录里取，先例同款判据）。
        self.assertEqual(stub.paths, [REC_ORDER_PATH])

        # ── 列表那一半：`my-orders` 收得这张镜像转正单 ──
        listed = self.client.get("/api/business/my-orders/").json()
        self.assertEqual(listed["code"], 0)
        self.assertIn(order_no, [row["order_no"] for row in listed["data"]["items"]])

        # ── detail 那一半：同原生单一样 200 + 恰好 16 键 + 值来自转正出来的订单 ──
        response = self.client.get(_url(order_no))
        self.assertEqual(response.status_code, 200)
        detail = response.json()["data"]
        self.assertEqual(sorted(detail), DETAIL_KEYS)
        self.assertEqual(detail["order_no"], order_no)
        self.assertEqual(detail["status"], OrderStatus.IN_PROGRESS)
        self.assertEqual(detail["game_name"], "测试游戏甲")
        self.assertEqual(detail["project_text"], "合成项目")
        # 转正单没有区服 / 没有群码 ⇒ 这两格是 null（同原生单的可空语义）。
        self.assertIsNone(detail["server_name"])
        self.assertIsNone(detail["group_qr_url"])
