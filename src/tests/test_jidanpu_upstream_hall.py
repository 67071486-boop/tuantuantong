"""上家鸡蛋铺线 Task 11：**大厅读口多并一路 + 镜像单认领端点**（spec §5.1 / §6.1 / §9.3 / §11-29 / T18）。

本文件钉五件事（每条都**钉值**，⛔ 不是「有就行」）：

① **三路行形状恰好相等**（`HallListSerializer.Meta.fields` ∪ 7 个合并键）+ `origin` 三档
   （`native` / `remote` / `upstream`）可分辨；⛔ 原两路的取值一个都没改（spec §4.6 的加法）。
② **游戏权限卡点三路共用**（spec §11-29 / A15 第 4 条）：`?game=` 对三路同语义；账号没有某游戏
   权限 ⇒ 从**本线镜像行**里也看不到那只游戏的大厅单。
③ **接单端点**（`POST /api/business/hall/upstream/<pk>/claim/`）：与原生接单**同一个节点门**
   （`grab_hall.take_order`）；匿名 401 / 无节点 403；请求体塞 `user` / 绑定 id 一律**被忽略**
   （越权面 = 0，租户与行主只来自认证态）；重复 / 并发认领**不许双开一张单**。
④ ⭐ **口径修订（业务方）**：上家没给 `recFee` ⇒ 勾兑输入按 `0.00` 算 ⇒
   镜像行的展示价是 **`"0.00"`**（⛔ 不再是 `None`）；「请填发单价」那一整套提示已下线
   （`OrderSerializer.price_hint` 字段已删）。
⑤ ⭐ **口径修订**：镜像行 / 转正单的 `hall_price` = `recFee × 绑定 hall_price_percent / 100`
   （含默认 100 = 原价接手、shell 直写 `0` 的兜底不成免费单）；
   ⚠️ 上家没给价那一档 ⇒ 发单价 `0.00`（0 元单，见判据 ④）。

⚠️ 判据打**真实出口**：① 镜像行状态机终态；② 桩的**调用集合与次数**（`RecOrderByInternal`）；
   ③ 数据库行（订单数 / 链路 / 绩效归属）；④ 端点响应信封。⛔ 不打 logger。
⚠️ **零真实网络**：`jidanpu.open_client` 打在 SDK 边界（同 `test_jidanpu_upstream_consume.py` 手法）。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `kefu-up-*`、
   密码 `pw-canary-hall`、单号全零段。

⭐ **相对任务书原文的一处执行期偏差（是「不改原两路」的必然结果，必须留着）**：

1. **大厅第三路的取数走 `upstream_hall_queryset` + `serialize_upstream_row`**（视图先过
   `scope_queryset` 再序列化）。任务书 Interfaces 写的是 `upstream_hall_rows(**params)`，
   但那个函数的 docstring 明文把**游戏权限卡点留给调用方**，而它**已经序列化完**（拿不回
   queryset）⇒ 先调它就没法过权限门。`upstream_hall_rows` 照旧可用于「不加权限门」的场合
   （Task 4 的用例在用），本线只在视图里换成「先收窄、后序列化」。

⚠️ **并发那一条用 `TransactionTestCase` + 独立连接 + 栅栏**（照 `test_claim_concurrency.py` 的
既有形状）：`TenantTestCase` 把每条测试包在一个事务里，**其他线程看不到未提交的夹具**
⇒ 那种「并发的两条请求」会恒绿（P1 的教训）。本文件把竞争窗口**造出来**（栅栏卡在
`check_grab_limits` 之后、条件更新之前），并断言**终态**：恰好一张我方订单 + 恰好一次上游调用。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.test import Client, TransactionTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import (
    get_tenant_domain_model,
    get_tenant_model,
    schema_context,
)

from apps.accounts.models import StaffProfile
from apps.accounts.wallet import get_wallet
from apps.audit.models import OperationLog
from apps.business import jidanpu, jidanpu_common, jidanpu_upstream
from jidanpu.errors import AuthError, TransportError
from apps.business.api.jidanpu_upstream_views import JidanpuUpstreamHallClaimView
from apps.business.api.order_views import OrderViewSet
from apps.business.claim import ClaimError
from apps.business.api.serializers import HallListSerializer
from apps.business.models import (
    EmpType,
    Game,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    JidanpuUpstreamOrderLink,
    Order,
    OrderStatus,
    Performance,
)
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import make_logged_in_client

HALL_URL = "/api/business/hall/"
#: 本线的接单路径**逐字**（spec §6.1 的端点面 / 计划的 Files）。
CLAIM_PATH = "/api/business/hall/upstream/{pk}/claim/"
#: 端点路径**逐字**抄 spec §2.3（⛔ 不从模块常量派生）。
REC_ORDER_PATH = "/OrderAjax/RecOrderByInternal"

BASE_URL = "https://jidanpu-upstream-hall.test.invalid"
ACCOUNT = "kefu-up-hall-owner"
ACCOUNT_OTHER = "kefu-up-hall-other"
PASSWORD = "pw-canary-hall"
#: 合成上家单号（全零段，一眼是夹具）。
REMOTE_ID = "20260928000000000911"

TENANT_DOMAIN = "tenant.test.com"
NODES = ("grab_hall.take_order",)
BASE_TIME = datetime(2026, 9, 28, 10, 0, tzinfo=dt_timezone.utc)

#: 大厅行的**合并键**（`HallListSerializer.Meta.fields` 之外那 7 个，spec §4.6）。
#: ⛔ 这里**没有** `MERGE_KEYS`（那是被删除的旧形态：`HallListSerializer.Meta.fields` 之外
#: 那 7 个合并键）。它们**今天已在 `Meta.fields` 里** ⇒ 用例直接对拍 `Meta.fields`，
#: 这个常量不再有人读 ⇒ 删除（留着是「同一事实两处」）。


class _StubClient:
    """SDK 边界替身：只服务 `RecOrderByInternal`，并记录**每一次调用**（⛔ 不接真网络）。"""

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


class _ClientCtx:
    """`jidanpu.open_client(binding)` 的替身（上下文管理器里就是桩客户端）。"""

    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *_exc):
        return False


# ══ ⓪ 端点面（结构判据）═════════════════════════════════════════════════════════


class UpstreamHallEndpointSurfaceTests(TenantTestCase):
    """接单端点的**节点门**与「只接受 POST」两条结构判据（不靠请求也能变红）。"""

    def setUp(self):
        self.game_a = Game.objects.create(name="测试游戏甲")

    def test_the_claim_node_is_the_same_literal_as_the_native_claim(self):
        """⭐ spec §6.1 / §11-29：上家镜像接单与**原生**接单同一个节点门。

        断言**元组取值本身**（⛔ 不是「列表非空」）：把 `permission_classes` 清空 ⇒
        DRF 回落到默认的 `IsAuthenticated` ⇒ 节点门**静默消失**（E-0063 一族），
        那时本用例必须在字面量上红。

        ⛔ **2026-10-08 改准**：原判据还断言与 `OrderViewSet.ORDER_PERMISSIONS["remote_claim"]`
        相等（P2 的**租户级**镜像接单）。那个 action 与它的权限项随租户级鸡蛋铺线整条拆除
        ⇒ 那一句退场（它今天是 `KeyError`，不是判据）。⚠️ 留下的两句强度没降：
        仍是**字面量元组**相等，⛔ 不是「非空」。
        """
        codes = tuple(
            code
            for cls in JidanpuUpstreamHallClaimView.permission_classes
            for code in getattr(cls, "_configured_codes", ())
        )
        self.assertEqual(codes, ("grab_hall.take_order",))
        self.assertEqual(codes, OrderViewSet.ORDER_PERMISSIONS["claim"])

    def test_only_post_is_routed_to_the_claim_view(self):
        """⛔ 不给自己留读口 / 写口：其余方法一律 405（`test_no_route_500` 覆盖不到带转换器的路由）。

        ⚠️ 观察者必须**已登录且持节点**：DRF 的 `initial()`（含 `check_permissions`）排在
        方法分派**之前** ⇒ 匿名打 GET 会先得到 **401**，量不到 405。持证打才会走到
        「这个 view 没有 `get` / `put` / …」那一档。
        """
        client = make_logged_in_client(
            username="up-hall-405", groups=("代练员",), games=[self.game_a],
            nodes=NODES,
        )
        url = CLAIM_PATH.format(pk=1)
        for method in ("get", "put", "patch", "delete"):
            with self.subTest(method=method):
                self.assertEqual(getattr(client, method)(url).status_code, 405)


# ══ ①–② 大厅读口三路（spec §4.6 / §11-29）═══════════════════════════════════════


class UpstreamHallReadTests(TenantTestCase):
    def setUp(self):
        self.game_a = Game.objects.create(name="测试游戏甲")
        self.game_b = Game.objects.create(name="测试游戏乙")
        # ⚠️ `groups=` **必须给**：节点挂在 `user.groups → Role → permissions` 上
        #   （`p5_auth_helpers._grant_nodes` 遍历该用户的组）⇒ 一个组都没有时 `nodes=`
        #   被**静默忽略**、每个请求 403（症状长得像判据写错）。
        #   `games=None` 是显式的「不建档案」⇒ 下面自己建档案并按用例调整。
        self.viewer = make_logged_in_client(
            username="up-hall-viewer", groups=("代练员",), games=None, nodes=NODES
        )
        self.profile = StaffProfile.objects.create(
            user=User.objects.get(username="up-hall-viewer")
        )
        self.profile.games.set([self.game_a, self.game_b])
        self.owner_user = User.objects.create_user(
            username=ACCOUNT, password=PASSWORD, first_name="测试客服甲"
        )
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.owner_user, jidanpu_base_url=BASE_URL, account=ACCOUNT,
            password=PASSWORD, enabled=True,
        )

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def _native(self, order_no, *, created_at, game=None):
        order = Order.objects.create(
            order_no=order_no, status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"), game=game or self.game_a, created_at=created_at,
        )
        Performance.objects.create(order=order, emp_type=EmpType.MAIN)
        # ⚠️ `created_at` 是 `auto_now_add` ⇒ 上面那个 kwarg 被 `pre_save` 覆盖，
        #    必须用 `update()` 钉回夹具给的值（`test_jidanpu_hall_merge.py` 同款）。
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        order.created_at = created_at
        return order

    def _upstream(self, upstream_order_id, *, first_seen_at, game=None, rec_fee="66.00"):
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id=upstream_order_id,
            owner_binding=self.binding, status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=game if game is not None else self.game_a,
            project_text="上家合成项目", remote_remark="上家合成备注",
            remote_role_name="上家合成角色",
            remote_rec_fee=None if rec_fee is None else Decimal(rec_fee),
            remote_game_area="安卓", remote_game_server="微信区",
            upstream_payload={"orderId": upstream_order_id},
            first_seen_at=first_seen_at,
        )

    def _get(self, **params):
        return self.viewer.get(HALL_URL, params)

    # ── 形状 / 投影 / 排序 / 分页 ───────────────────────────────────────────

    def test_the_two_streams_share_exactly_one_row_shape(self):
        """⭐ 两路的行键**恰好相等**（与 `HallListSerializer.Meta.fields` 对拍），`origin` 两档可分辨。

        失败方向：本线镜像行漏一个键 / 多一个键 ⇒ 本用例红。

        ⛔ **2026-10-08 改准（原名 `test_three_streams_share_exactly_one_row_shape`）**：
        原第三路 = P2（租户级）镜像单，它随租户级鸡蛋铺线整条拆除
        （`docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md`）⇒ 只剩原生 + 本线两路。

        ⚠️ **期望键集改准**：`mirror_pk` 与那 7 个合并键**都在 `HallListSerializer.Meta.fields`
        里**（Task 12 集成补丁把 `mirror_pk` 加进了 `fields`）⇒ 旧写法在外面再 `| MERGE_KEYS
        | {"mirror_pk"}` 会造出一个**比真实行多 8 个键**的期望集合（断言恒假——旧写法在
        本次改动前就已经是红的，只是当时本文件先在 import 阶段炸、把它挡住了）。
        现在直接对拍 `Meta.fields`，⛔ **没有**放宽成 `toContain`。
        """
        self._native("20260928000000000001", created_at=BASE_TIME)
        mirror = self._upstream("UP-H1", first_seen_at=BASE_TIME + timedelta(minutes=2))
        body = self._get().json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        self.assertEqual(len(items), 2, items)
        keys = {frozenset(item.keys()) for item in items}
        self.assertEqual(len(keys), 1, f"两路行键集不一致：{keys}")
        self.assertEqual(set(items[0].keys()), set(HallListSerializer.Meta.fields))
        self.assertEqual({item["origin"] for item in items}, {"native", "upstream"})
        # 两路的 `mirror_pk` 取值：**只有本线**是它自己的主键，原生那一路恒 `None`。
        by_origin = {item["origin"]: item["mirror_pk"] for item in items}
        self.assertIsNone(by_origin["native"])
        self.assertEqual(by_origin["upstream"], mirror.pk)

    def test_the_upstream_row_is_projected_like_the_other_two(self):
        """本线镜像行的取值口径（spec §5.1 + §15.1 + **口径修订 2026-09-29**）：
        `hall_price` = **勾兑后的我方发单价**、`game` 是 FK id。

        ⚠️ 默认百分比 100 ⇒ 这一档与「原价接手」等价 ⇒ 值仍是 `recFee` 原值（`66.00`）。
        """
        self._upstream("UP-H2", first_seen_at=BASE_TIME, rec_fee="66.00")
        item = self._get().json()["data"]["items"][0]
        self.assertEqual(item["origin"], "upstream")
        self.assertEqual(item["source_system"], jidanpu_common.JIDANPU)
        self.assertEqual(item["remote_order_id"], "UP-H2")
        self.assertEqual(item["project_text"], "上家合成项目")
        self.assertEqual(item["remark"], "上家合成备注")
        self.assertEqual(item["game_role_name"], "上家合成角色")
        self.assertEqual(item["hall_price"], "66.00")
        self.assertEqual(item["game"], self.game_a.pk)
        self.assertEqual(item["order_no"], "")
        self.assertIsNone(item["server"])
        # 原样行（`upstream_payload`）**不进**行形状（同 P2 的 S1 口径）。
        self.assertNotIn("upstream_payload", item)

    def test_a_mirror_row_without_a_rec_fee_renders_a_zero_price(self):
        """⭐ 判据 ④（**口径修订，业务方 2026-09-29**）：上家没给 `recFee` ⇒ 大厅展示价 **`"0.00"`**。

        业务原话：「如果是空的，默认按照 0 算就行了」⇒ 勾兑输入按 `0.00` 算
        （`0 × 比例 / 100 = 0.00`）。⛔ **不再是 `None`**（那会让打手在大厅里看到一个空价）。
        失败方向：把 `upstream_hall_price` 的 `None` 分支改回 `return None`
        ⇒ 这里读到 `None` ⇒ 本用例红（`test_jidanpu_upstream_consume` / `…record` 各有一条同族）。
        """
        self._upstream("UP-H3", first_seen_at=BASE_TIME, rec_fee=None)
        item = self._get().json()["data"]["items"][0]
        self.assertIn("hall_price", item)
        self.assertEqual(item["hall_price"], "0.00")

    # ── ⭐ 口径修订（业务方）：镜像行的价 = 上家价 × 绑定百分比 ──────

    def test_the_mirror_row_price_is_the_blended_offer_price(self):
        """⭐ 判据 ①：镜像行的 `hall_price` = `recFee × 绑定 hall_price_percent / 100`（**钉值**）。

        业务原话：上家给 100、比例 50 ⇒ 打手接单拿 50（我方留 50）。⚠️ 取的是**打手能看到的那一档**
        ——⛔ 不是 `recFee` 原值（原值是**客户价**，镜像行的键表里没有它）。
        ⚠️ 含**两位小数 + 四舍五入**的实例：`99.99 × 50 / 100 = 49.995` ⇒ `ROUND_HALF_UP` ⇒ `50.00`。

        失败方向：`hall_price` 改回 `_money_text(row.remote_rec_fee)`（不乘比例）⇒ 本用例红。
        """
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_price_percent=50
        )
        self._upstream("UP-H7", first_seen_at=BASE_TIME + timedelta(minutes=2), rec_fee="100.00")
        self._upstream("UP-H8", first_seen_at=BASE_TIME + timedelta(minutes=1), rec_fee="99.99")
        self._upstream("UP-H9", first_seen_at=BASE_TIME, rec_fee="88.00")
        items = self._get().json()["data"]["items"]
        by_id = {item["remote_order_id"]: item for item in items}
        self.assertEqual(by_id["UP-H7"]["hall_price"], "50.00")   # 100 × 50% = 50（业务原话那一例）
        self.assertEqual(by_id["UP-H8"]["hall_price"], "50.00")   # 49.995 ⇒ 四舍五入到 50.00
        self.assertEqual(by_id["UP-H9"]["hall_price"], "44.00")   # 88 × 50%

    def test_the_default_percent_takes_the_upstream_price_as_is(self):
        """⭐ 判据 ③（**默认值等价**）：列上的默认 100 ⇒ `hall_price == recFee`（原价接手）。

        ⚠️ 唯一改动是**新建绑定时不传这个列**（走模型默认值），⛔ 不手工写 100——那会把
        「默认值是多少」这件事从判据里抽掉。
        没有这一条，「永远打对折」的实现也能让上一条绿（自指 / 恒真，`AGENTS.md` §5-G）。
        """
        self.assertEqual(self.binding.hall_price_percent, 100)   # 夹具自证：走的是模型默认值
        self._upstream("UP-H10", first_seen_at=BASE_TIME, rec_fee="66.00")
        item = self._get().json()["data"]["items"][0]
        self.assertEqual(item["hall_price"], "66.00")

    def test_a_shell_written_zero_percent_falls_back_to_full_price_never_a_free_order(self):
        """⭐ 判据 ④（兜底）：shell 直写 0（绕过 `full_clean()`）⇒ 按 **100** 算，⛔ 不出免费单。

        `or 100` 的方向是**刻意选的**：0 等于把一张**免费单**发进我方大厅
        （与 `LAUNCH_NO_PRICE_REASON` 同族）。失败方向：`or 100` 改成 `or 0`
        ⇒ 这里读到 `"0.00"`（一张免费单）⇒ 本用例红。
        """
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_price_percent=0
        )
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_price_percent, 0)   # 前提交互可测：0 真的写进去了
        self._upstream("UP-H11", first_seen_at=BASE_TIME, rec_fee="88.00")
        item = self._get().json()["data"]["items"][0]
        self.assertEqual(item["hall_price"], "88.00")
        self.assertNotEqual(item["hall_price"], "0.00")        # ⛔ 不是免费单

    def test_the_two_streams_are_ordered_by_entering_time_and_tie_native_first(self):
        """排序键 = 「进入大厅的时刻」；并列次序 = 原生 → 本线（spec §4.6 的加法）。

        ⛔ **2026-10-08 改准（原名 `…_tie_native_remote_upstream`）**：P2 镜像那一档随
        租户级那条线拆除。⚠️ 并列次序的**承重判据没降**：仍是**恰好等于**那个四元序列
        （`assertEqual`，⛔ 不是「包含」）。
        """
        self._upstream("UP-H4", first_seen_at=BASE_TIME)
        self._native("20260928000000000004", created_at=BASE_TIME)
        self._upstream("UP-H5", first_seen_at=BASE_TIME + timedelta(minutes=5))
        items = self._get().json()["data"]["items"]
        self.assertEqual(
            [item["origin"] for item in items],
            ["upstream", "native", "upstream"],
        )
        self.assertEqual(items[0]["remote_order_id"], "UP-H5")

    def test_total_counts_both_streams(self):
        """⛔ **2026-10-08 改准**：原名 `test_total_counts_all_three_streams` ——
        P2 镜像那一档随租户级那条线拆除 ⇒ 只剩原生 + 本线两路（`total` 逐字是 **2**）。"""
        self._native("20260928000000000005", created_at=BASE_TIME)
        self._upstream("UP-H6", first_seen_at=BASE_TIME)
        self.assertEqual(self._get().json()["data"]["total"], 2)

    # ── 游戏筛选 / 权限卡点（spec §11-29 / A15 第 4 条）─────────────────────

    def test_the_game_filter_applies_to_both_streams(self):
        """`?game=<乙>` **只**回乙那一款的两路行（含本线镜像行）。

        ⛔ **2026-10-08 改准**：原名 `…_all_three_streams` —— P2 镜像那一档随租户级那条线
        拆除。⚠️ 判据强度没降：仍是**恰好等于**那个两元序列 + `total` 逐字 2。
        """
        self._native("20260928000000000006", created_at=BASE_TIME, game=self.game_a)
        self._native("20260928000000000007", created_at=BASE_TIME, game=self.game_b)
        self._upstream("UP-H7", first_seen_at=BASE_TIME, game=self.game_a)
        self._upstream("UP-H8", first_seen_at=BASE_TIME, game=self.game_b)
        body = self._get(game=self.game_b.pk).json()["data"]
        self.assertEqual(body["total"], 2)
        self.assertEqual(
            sorted(item["origin"] for item in body["items"]),
            ["native", "upstream"],
        )
        self.assertEqual({item["game"] for item in body["items"]}, {self.game_b.pk})

    def test_a_mirror_row_on_a_game_without_permission_is_invisible(self):
        """⭐ spec §11-29：没有那只游戏的权限 ⇒ **从本线镜像行里也看不到**它的大厅单。

        失败方向：视图里漏掉第三路的 `scope_queryset` ⇒ 本用例红（另两路照旧被挡）。
        """
        allowed = self._upstream("UP-H9", first_seen_at=BASE_TIME, game=self.game_a)
        hidden = self._upstream("UP-H10", first_seen_at=BASE_TIME, game=self.game_b)
        # 前置哨兵：两条都**真的**是 `listed` 的镜像行（不是被状态机挡掉的）
        self.assertEqual(
            JidanpuUpstreamHallOrder.objects.filter(
                pk__in=[allowed.pk, hidden.pk], status="listed"
            ).count(),
            2,
        )
        self.profile.games.set([self.game_a])
        body = self._get().json()["data"]
        self.assertEqual(
            [item["remote_order_id"] for item in body["items"]], ["UP-H9"]
        )
        self.assertEqual(body["total"], 1)

    def test_promoted_and_gone_mirror_rows_stay_out_of_the_hall(self):
        """大厅只列 `listed` 的镜像行（状态机终态不进大厅，spec §3.1）。"""
        self._upstream("UP-H11", first_seen_at=BASE_TIME)
        self._upstream("UP-H12", first_seen_at=BASE_TIME)
        JidanpuUpstreamHallOrder.objects.filter(upstream_order_id="UP-H11").update(
            status=JidanpuUpstreamHallOrder.Status.GONE
        )
        JidanpuUpstreamHallOrder.objects.filter(upstream_order_id="UP-H12").update(
            status=JidanpuUpstreamHallOrder.Status.SKIPPED
        )
        self.assertEqual(self._get().json()["data"]["total"], 0)

    def test_listed_rows_of_a_disabled_binding_stay_out_of_the_hall(self):
        """**归属绑定停用**的 `listed` 镜像行不进大厅（E-0288，2026-10-24）。

        绑定停用后名下的在挂行会滞留：卡片看得见、接单被「归属绑定必须启用」前置拒掉
        （修前回 `1015`「没配置好同步账号」——某租户现场：一个停用绑定名下 13 张在挂行
        被点接全部 500/拒。⭐ 2026-10-29 业务方报障修法一起，接单侧该支已改：回 `1012`
        「已被其他打手接取」并**命中即下架**（`listed → gone`），见
        `UpstreamHallClaimEndpointTests` 的两条拆雷用例）。可见与可接**同一道判据**：
        读口就地过滤，数据保留。
        """
        from apps.business.models import JidanpuUpstreamBinding

        self._upstream("UP-H13", first_seen_at=BASE_TIME)
        self.assertEqual(self._get().json()["data"]["total"], 1)
        JidanpuUpstreamBinding.objects.update(enabled=False)
        body = self._get().json()["data"]
        self.assertEqual(body["total"], 0, "停用绑定的镜像行还挂在大厅")
        # 对照自证：行本身还在（数据保留，只是不进大厅）。
        self.assertEqual(
            JidanpuUpstreamHallOrder.objects.filter(
                upstream_order_id="UP-H13", status="listed"
            ).count(),
            1,
        )


# ══ ③ 接单端点（spec §6.1 / §11-19 / §11-29）═══════════════════════════════════


class UpstreamHallClaimEndpointTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.other_game = Game.objects.create(name="测试游戏乙")
        self.owner_user = User.objects.create_user(
            username=ACCOUNT, password=PASSWORD, first_name="测试客服甲"
        )
        self.other_user = User.objects.create_user(
            username=ACCOUNT_OTHER, password=PASSWORD, first_name="测试客服乙"
        )
        #: 同地址两份绑定：owner = **最早**那份（A2 的地址级去重）；越权面那一条靠它。
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.owner_user, jidanpu_base_url=BASE_URL, account=ACCOUNT,
            password=PASSWORD, enabled=True,
        )
        self.other_binding = JidanpuUpstreamBinding.objects.create(
            user=self.other_user, jidanpu_base_url=BASE_URL, account=ACCOUNT_OTHER,
            password=PASSWORD, enabled=True,
        )
        self.taker_client = make_logged_in_client(
            username="up-hall-taker-a", groups=("代练员",), games=[self.game],
            nodes=("grab_hall.take_order", "order.view_detail"),
        )
        self.taker = User.objects.get(username="up-hall-taker-a")
        self.other_taker_client = make_logged_in_client(
            username="up-hall-taker-b", groups=("代练员",), games=[self.game],
            nodes=("grab_hall.take_order", "order.view_detail"),
        )
        self.mirror = self._mirror()

    def _mirror(self, *, remote_id=REMOTE_ID, game=None, status=None, claiming_by=None,
                owner=None, rec_fee=Decimal("88.00")):
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id=remote_id,
            owner_binding=self.binding if owner is None else owner,
            status=status or JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game if game is None else game,
            project_text="合成项目", remote_remark="合成备注",
            remote_role_name="合成角色", remote_rec_fee=rec_fee,
            claiming_by=claiming_by, claiming_at=BASE_TIME if claiming_by else None,
            upstream_payload={
                "orderId": remote_id, "orderContent": "合成需求",
                "orderRemark": "合成备注", "roleName": "合成角色",
                "gameAcc": "acc-synth", "zgameAcc": "sub-synth", "recFee": "88.00",
            },
            first_seen_at=BASE_TIME,
        )

    def _url(self, pk):
        return CLAIM_PATH.format(pk=pk)

    def _claim(self, client, pk, *, body=None):
        if body is None:
            return client.post(self._url(pk))
        return client.post(self._url(pk), body, content_type="application/json")

    def _run_with(self, client, *, pk=None, body=None):
        stub = _StubClient()
        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(stub)):
            response = self._claim(client, self.mirror.pk if pk is None else pk, body=body)
        return stub, response

    # ── 权限（匿名 401 / 无节点 403 / 无游戏权限 fail-closed）───────────────

    def test_anonymous_is_401_before_the_view_body(self):
        """⚠️ 匿名请求必须显式打在**租户域名**上：`testserver` 不在域名表里，
        `TenantMainMiddleware` 会在进 urlconf 之前 404（那会让本用例失去判别力）。"""
        anon = Client(SERVER_NAME=TENANT_DOMAIN)
        before = JidanpuUpstreamHallOrder.objects.get(pk=self.mirror.pk).status
        response = anon.post(self._url(self.mirror.pk))
        self.assertEqual(response.status_code, 401, response.content[:200])
        self.assertEqual(JidanpuUpstreamHallOrder.objects.get(pk=self.mirror.pk).status, before)
        self.assertEqual(Order.objects.count(), 0)

    def test_without_the_node_it_is_403_with_zero_writes(self):
        """持证但**没有** `grab_hall.take_order` ⇒ HTTP 403 + 零写入 + 零上游调用。"""
        from apps.rbac.permissions import has_node_now

        no_node = make_logged_in_client(
            # ⚠️ 组名必须换一个：沿用「代练员」会从那个组**继承**到节点 ⇒ 夹具前提假了。
            username="up-hall-no-node", groups=("客服",), games=[self.game],
            nodes=("order.view_detail",),
        )
        no_node_user = User.objects.get(username="up-hall-no-node")
        # 夹具自证（承重点）：这个账号**真的**没有那个节点。
        self.assertFalse(has_node_now(no_node_user, "grab_hall.take_order"))
        stub, response = self._run_with(no_node)
        self.assertEqual(response.status_code, 403, response.content[:200])
        self.assertEqual(stub.calls, [])
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(JidanpuUpstreamOrderLink.objects.count(), 0)

    def test_a_taker_without_the_games_permission_cannot_claim_a_mirror_row(self):
        """⭐ spec §11-29 的写侧一半：游戏权限卡点在**接单**上也必须有（照 P2 的同一道门）。

        持 `grab_hall.take_order` 但游戏范围只有**另一只**游戏的打手，拿**同一个**镜像行
        打接单 ⇒ 必须被拒，且**零改动 / 零上游调用**。返回口径与「这一行不存在」逐字同形
        （fail-closed、不泄露存在性）。
        """
        outsider = make_logged_in_client(
            username="up-hall-taker-other-game", groups=("代练员",),
            games=[self.other_game], nodes=("grab_hall.take_order",),
        )
        stub, response = self._run_with(outsider)
        body = response.json()
        self.assertEqual(body["code"], ErrorCode.ORDER_ALREADY_TAKEN, body)
        self.assertIsNone(body["data"])
        self.assertEqual(stub.calls, [])
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(
            JidanpuUpstreamHallOrder.objects.get(pk=self.mirror.pk).status,
            JidanpuUpstreamHallOrder.Status.LISTED,
        )

    # ── ⭐ 行 → 参数 → 端点（集成补丁：把端点面的缺口钉死）──────────

    def test_the_row_mirror_pk_reaches_the_claim_endpoint(self):
        """⭐ **整条链路**：从**大厅行载荷**里取出 `mirror_pk` ⇒ 拿它打接单端点 ⇒ 真的接手成功。

        ## 为什么必须从行里取（而不是像本文件其它接单用例那样用 `self.mirror.pk`）

        本端点按**本线镜像表主键**取行（`hall/upstream/<int:pk>/claim/`），而大厅行原先
        **没有任何主键** ⇒ 前端拿不到那个 `<pk>`、那颗「接手此单」**结构上点不动**。
        用 `self.mirror.pk` 直接拼 URL 会**绕过**这个缺口（夹具里到处都有那个对象），
        所以那种写法单侧全绿、把这个集成缺口留到前端才炸。本用例刻意走
        「**行 → 参数 → 端点**」：`mirror_pk` 一旦丢了 / 换了值，这里就红。

        ## 变异读数（必须真的红，不是「看起来能红」）

        - `serialize_upstream_row` 的 `"mirror_pk": row.pk` ⇒ 改成 `None`：
          上面第一条 `assertIsNotNone` 立刻红（URL 也会变成 `.../None/claim/`）。
        - 改成上家单号（`row.upstream_order_id`）⇒ `assertEqual(row["mirror_pk"], self.mirror.pk)`
          红（两者不相等）。
        """
        rows = self.taker_client.get(HALL_URL).json()["data"]["items"]
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["origin"], "upstream")
        self.assertIsNotNone(row["mirror_pk"])
        self.assertEqual(row["mirror_pk"], self.mirror.pk)

        stub, response = self._run_with(self.taker_client, pk=row["mirror_pk"])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        # 上游**恰好**被调一次（行里的主键指向的就是这一行 ⇒ 才会走 RecOrderByInternal）。
        self.assertEqual(stub.paths, [REC_ORDER_PATH])
        self.assertEqual(
            JidanpuUpstreamHallOrder.objects.get(pk=self.mirror.pk).status,
            JidanpuUpstreamHallOrder.Status.PROMOTED,
        )
        self.assertEqual(Order.objects.count(), 1)

    # ── ⭐ 停用绑定的僵尸卡（业务方报障修法一）────────────────────

    def test_a_stale_card_of_a_disabled_binding_answers_1012_and_defuses_it(self):
        """⭐ 停用绑定名下仍标 `listed` 的僵尸卡：接单回 `1012` + **命中即下架** + 全套留痕。

        修前口径：回 `1015`「还没配置好鸡蛋铺同步账号，请联系管理员」且行永远滞留
        `listed`（E-0288 读口过滤后 App 缓存 / 按单号仍能命中；报障现场 40 次被拒）。
        修后：对打手而言与「已下架」不可区分 ⇒ `1012`；行 `listed → gone`（点一颗拆一颗），
        留痕照拉取侧 `_mark_gone` 的完整形态：`withdrawn` 灰卡 + `upstream.hall_gone` 台账
        （成因句 = 绑定停用那条，⛔ 与拉取侧「上游撤了」那句区分）。
        """
        from apps.business.models import HallExitSnapshot

        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        stub, response = self._run_with(self.taker_client)
        body = response.json()
        self.assertEqual(body["code"], ErrorCode.ORDER_ALREADY_TAKEN, body)
        self.assertEqual(body["msg"], "该笔订单已被其他打手接取", body)
        self.assertIsNone(body["data"])
        # 零上游调用、零建单（这张卡从没到过打上游那一步）。
        self.assertEqual(stub.calls, [])
        self.assertEqual(Order.objects.count(), 0)
        # ⭐ DB 行终态（E-0009：断数据库行，不打中间层）。
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.GONE)
        self.assertIsNotNone(self.mirror.gone_at, "命中即下架必须留 gone_at")
        # 灰卡：恰好一张 withdrawn（照 `_mark_gone`；claiming → gone 的 `_claim_refused` 不写）。
        snaps = HallExitSnapshot.objects.filter(mirror=self.mirror)
        self.assertEqual(snaps.count(), 1)
        self.assertEqual(snaps.first().exit_kind, HallExitSnapshot.ExitKind.WITHDRAWN)
        # 台账：恰好一条 hall_gone，成因是「绑定停用」（与拉取侧消失判定同 kind 不同 reason）。
        events = JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone")
        self.assertEqual(events.count(), 1)
        self.assertEqual(
            events.first().reason,
            jidanpu_upstream.BINDING_DISABLED_GONE_REASON,
        )
        self.assertEqual(events.first().payload.get("upstream_binding_id"), self.binding.pk)

    def test_a_defused_stale_card_answers_1012_again_without_new_traces(self):
        """拆过的僵尸卡再点 ⇒ 仍 `1012`（走既有「非在挂」支），且**不再**新增灰卡 / 台账。"""
        from apps.business.models import HallExitSnapshot

        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        first = self._run_with(self.taker_client)[1]
        second = self._run_with(self.taker_client)[1]
        for response in (first, second):
            body = response.json()
            self.assertEqual(body["code"], ErrorCode.ORDER_ALREADY_TAKEN, body)
            self.assertEqual(body["msg"], "该笔订单已被其他打手接取", body)
        self.assertEqual(Order.objects.count(), 0)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.GONE)
        # 第二次命中走「非在挂」支：一颗雷只拆一次，留痕不翻倍。
        self.assertEqual(HallExitSnapshot.objects.filter(mirror=self.mirror).count(), 1)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").count(), 1
        )

    # ── 正向：预占 → 打上游 → 转正 ──────────────────────────────────────────

    def test_stale_mirror_after_store_switch_cannot_send_claim_to_new_store(self):
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            jidanpu_base_url="https://new-mirror-store.test.invalid",
        )

        with mock.patch.object(jidanpu, "claim_session") as session:
            response = self._claim(self.taker_client, self.mirror.pk)

        self.assertEqual(response.json()["code"], ErrorCode.ORDER_ALREADY_TAKEN)
        session.assert_not_called()
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertFalse(JidanpuSyncEvent.objects.filter(kind=jidanpu_upstream.KIND_HALL_CLAIM).exists())

    def test_the_claim_promotes_and_calls_the_upstream_exactly_once(self):
        """⭐ spec §2.3 / §6.1：载荷**只有** `orderId`，判 `code > 0`，成功才转正成我方订单。"""
        stub, response = self._run_with(self.taker_client)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        # 上游调用**逐字**（⛔ 不带别的键；⛔ A16：一次状态写都没有）。
        self.assertEqual(stub.calls, [
            ("POST", REC_ORDER_PATH, {"data": {"orderId": REMOTE_ID}, "success_rule": "lenient"}),
        ])
        order = Order.objects.get()
        self.assertEqual(order.order_no, body["data"]["order_no"])
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(order.game_id, self.game.pk)
        # 钱：客户价 ← 上家 `recFee`（**不受比例影响**）；
        # 发单价 = 勾兑后的价 —— 本夹具走**模型默认**百分比 100 ⇒ 原价接手（= `recFee`）。
        self.assertEqual(order.total_price, Decimal("88.00"))
        self.assertEqual(order.hall_price, Decimal("88.00"))
        # 终态：镜像行转正 + 指向那张单 + 占住人 = 本打手。
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)
        self.assertEqual(self.mirror.promoted_order_id, order.pk)
        self.assertEqual(self.mirror.claiming_by_id, self.taker.pk)
        # 链路挂在本线（in_hall=False）⇒ 后续 `upstream.complete` 找得到它。
        link = JidanpuUpstreamOrderLink.objects.get()
        self.assertEqual(link.order_id, order.pk)
        self.assertEqual(link.binding_id, self.binding.pk)
        self.assertFalse(link.in_hall)
        self.assertEqual(Order.objects.count(), 1)

    def test_accepted_hall_claim_keeps_the_taker_when_orders_poll_reads_first(self):
        sdk = _StubClient()
        remote_call = sdk.call
        row = {**self.mirror.upstream_payload, "gameName": self.game.name, "orderSta": "代练中"}
        poll_sdk = mock.Mock(last_envelope={"code": 0, "data": [row], "count": 1})
        poll_sdk.call.side_effect = lambda method, path, *, query: (
            [row] if query["page"] == 1 else []
        )

        def accept_while_orders_poll(*args, **kwargs):
            accepted = remote_call(*args, **kwargs)
            jidanpu_upstream.sync_my_orders(
                binding=self.binding, client=poll_sdk, now=timezone.now(),
            )
            return accepted

        sdk.call = accept_while_orders_poll
        self.taker_client.raise_request_exception = False
        with mock.patch.object(jidanpu, "claim_session", return_value=_ClientCtx(sdk)):
            response = self._claim(self.taker_client, self.mirror.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.assertEqual(sdk.paths, [REC_ORDER_PATH])
        self.assertGreater(poll_sdk.call.call_count, 0)
        order = Order.objects.get()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id, self.taker.pk)
        self.assertEqual(JidanpuUpstreamOrderLink.objects.get(order=order).binding_id, self.binding.pk)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.promoted_order_id, order.pk)

    def test_login_refusal_releases_the_card_for_the_next_taker(self):
        with mock.patch.object(jidanpu, "claim_session", side_effect=AuthError("合成登录被拒绝")):
            response = self._claim(self.taker_client, self.mirror.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], ErrorCode.PLATFORM_QUERY_FAILED)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertIsNone(self.mirror.claiming_by_id)
        self.assertIsNone(self.mirror.claiming_at)
        self.assertEqual(JidanpuSyncEvent.objects.get(kind="upstream.hall_claim").status, "done")
        self.assertFalse(Order.objects.exists())

        stub, next_response = self._run_with(self.other_taker_client)
        self.assertEqual(next_response.json()["code"], 0, next_response.content)
        self.assertEqual(stub.paths, [REC_ORDER_PATH])
        self.assertEqual(Order.objects.count(), 1)

    def test_login_failure_from_a_previous_lease_cannot_release_the_current_claim(self):
        def auth_fails_after_takeover(_binding):
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_claim").update(
                lease_owner="synthetic-next-executor",
            )
            raise AuthError("合成登录被拒绝")

        with mock.patch.object(jidanpu, "claim_session", side_effect=auth_fails_after_takeover):
            response = self._claim(self.taker_client, self.mirror.pk)

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["code"], 0)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.CLAIMING)
        self.assertEqual(self.mirror.claiming_by_id, self.taker.pk)
        event = JidanpuSyncEvent.objects.get(kind="upstream.hall_claim")
        self.assertEqual((event.status, event.lease_owner), ("pending", "synthetic-next-executor"))

    def test_the_audit_row_names_the_taker_by_account_name(self):
        """⭐ 2026-10-17（订单日志人名线 spec §3-A2 / 判据 5）：上家大厅接单那条审计里的人名
        是**账号名称**（昵称），⛔ 不是登录账号。

        ⚠️ 夹具让昵称与登录账号**不同形**（spec §5 的纪律）：两者相同时这条判据会**假绿**。
        ⚠️ 两向断言（昵称在 + 账号不在）——只断前者时「两个都写进去」也能绿。
        """
        nick_client = make_logged_in_client(
            username="up-hall-nick-taker", groups=("代练员",), games=[self.game],
            nodes=("grab_hall.take_order", "order.view_detail"),
        )
        # 登录之后再改昵称（`make_logged_in_client` 会 `create_user`，同用户名不能建第二次；
        # 而接单视图读的是 `request.user` 在**请求时**的当前值 ⇒ 改完就生效）。
        nick_taker = User.objects.get(username="up-hall-nick-taker")
        nick_taker.first_name = "测试昵称打手卯"
        nick_taker.save(update_fields=["first_name"])

        stub, response = self._run_with(nick_client)
        body = response.json()
        self.assertEqual(body["code"], 0, body)

        row = OperationLog.objects.get(action="order.claim")
        self.assertEqual(row.target_name, "测试昵称打手卯")
        self.assertNotEqual(row.target_name, "up-hall-nick-taker")
        self.assertIn("测试昵称打手卯", row.summary)
        self.assertNotIn("up-hall-nick-taker", row.summary)
        self.assertEqual(row.target_kind, "user")
        self.assertEqual(row.reason, "网页端大厅")
        self.assertEqual(row.order_no, body["data"]["order_no"])
        self.assertEqual(row.actor_id, nick_taker.pk)

    def test_the_promoted_order_price_is_blended_and_the_customer_price_is_not(self):
        """⭐ 判据 ②：转正出来的 `Order.hall_price` = **同一个勾兑价**；`total_price` 仍 = `recFee`。

        比例只作用在**我方发单价**上（业务方原话：上家给 100、比例 50 ⇒ 打手接单拿 50、
        我方留 50）——⛔ 客户价照抄 `recFee`、**不受比例影响**。
        失败方向：把比例乘进 `total_price` ⇒ `total_price` 变 44.00 ⇒ 本用例红。
        """
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_price_percent=50
        )
        stub, response = self._run_with(self.taker_client)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        order = Order.objects.get()
        self.assertEqual(order.total_price, Decimal("88.00"))      # ⛔ 比例不许影响客户价
        self.assertEqual(order.hall_price, Decimal("44.00"))       # 88 × 50%
        # 端点响应（= 打手接单后立刻看到的载荷）里也是**同一个**价。
        self.assertEqual(body["data"]["hall_price"], "44.00")

    def test_the_request_body_cannot_choose_the_recorder_or_the_binding(self):
        """⭐ spec §8.3 / §11-19：行主与归属**只来自认证态 + 事件那份绑定** ⇒ 越权面 = 0。

        请求体里塞 `user` / `sales_user` / `taker_id` / 两份绑定的 id ⇒ 一律**被忽略**：
        录单人 = 镜像行 `owner_binding`（= 事件那份）的客服，操作人 = 本次认证态打手。
        """
        stub, response = self._run_with(
            self.taker_client,
            body={
                "user": self.other_user.pk,
                "sales_user": self.other_user.pk,
                "taker_id": self.other_user.pk,
                "upstream_binding_id": self.other_binding.pk,
                "owner_binding": self.other_binding.pk,
                "created_by": "冒名顶替",
            },
        )
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        order = Order.objects.get()
        self.assertEqual(order.sales_user_id, self.owner_user.pk)
        self.assertEqual(order.created_by, self.taker.username)
        # 事件归属也仍是 owner 那一份（不是请求体里那份）。
        queued = JidanpuSyncEvent.objects.get(kind=jidanpu_upstream.KIND_HALL_CLAIM)
        self.assertEqual(queued.payload["upstream_binding_id"], self.binding.pk)
        link = JidanpuUpstreamOrderLink.objects.get()
        self.assertEqual(link.binding_id, self.binding.pk)

    # ── ⭐ 口径修订（业务方）：上家没给价 ⇒ 0 元单 ─────────────────

    def test_a_mirror_row_without_an_upstream_price_is_a_zero_price_order_for_the_gate(self):
        """⭐ 判据 ④ 的另一半（**口径修订，业务方 2026-09-29**）：上家没给 `recFee`
        ⇒ 我方大厅里这张镜像单的展示价是 **`0.00`** ⇒ 抢单闸门把它当成一张 **0 元单**：
        设了「**最低接单价 > 0**」的打手**看得见、接不了**（价格区间那一支拒）。

        ⚠️ 这是业务方拍板时接受的代价（已登记为残差）。失败方向**不再是**旧的
        「NULL 接单价不可抢」（`candidate.hall_price is None`）那一支 ——
        把 `upstream_hall_price` 改回 `None` ⇒ 这里读到 `None`、闸门换回 NULL 那一支
        （msg 不含「接单价必须在」）⇒ 本用例红。
        """
        StaffProfile.objects.filter(user=self.taker).update(grab_price_min=Decimal("1"))
        JidanpuUpstreamHallOrder.objects.filter(pk=self.mirror.pk).update(remote_rec_fee=None)
        stub, response = self._run_with(self.taker_client)
        body = response.json()
        self.assertEqual(body["code"], ErrorCode.GRAB_LIMIT_EXCEEDED, body)
        self.assertIn("接单价必须在", body["msg"])   # 价格区间那一支（⛔ 不是 NULL 价那一支）
        self.assertEqual(stub.calls, [])
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(
            JidanpuUpstreamHallOrder.objects.get(pk=self.mirror.pk).status,
            JidanpuUpstreamHallOrder.Status.LISTED,
        )

    def test_a_mirror_row_without_an_upstream_price_can_be_claimed_when_the_min_is_zero(self):
        """⭐ 判据 ④（**口径修订，业务方 2026-09-29**）：没给价 ⇒ 展示价 `0.00`
        ⇒ 没设价格下限（默认 `0`）的打手**照常接得动**，转正出来的单 `hall_price = 0.00`、
        `total_price = 0.00`（客户价照既有口径兜 `0.00`）。

        ⚠️ 与上一条合起来钉的是「闸门判的就是大厅里那个数」：同一个 `0.00`，
        `min=0` 放行、`min=1` 拒。
        """
        JidanpuUpstreamHallOrder.objects.filter(pk=self.mirror.pk).update(remote_rec_fee=None)
        stub, response = self._run_with(self.taker_client)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        order = Order.objects.get()
        self.assertEqual(order.hall_price, Decimal("0.00"))
        self.assertEqual(order.total_price, Decimal("0.00"))
        self.assertEqual(body["data"]["hall_price"], "0.00")
        self.assertEqual(stub.paths, [REC_ORDER_PATH])

    def test_the_order_payload_has_no_price_hint_key_at_all(self):
        """⭐ 判据（**口径修订，业务方 2026-09-29**）：「请填发单价」整套下线
        ⇒ `OrderSerializer` 的输出里**连键都没有** `price_hint`（⛔ 不是「键在、值为假」）。

        ⛔ **别把这句改成断值**（`assertFalse(data["price_hint"])`）：把那个
        `SerializerMethodField` 加回序列化器（哪怕实现恒回 `False`）就**检不出来**了。
        失败方向：给 `OrderSerializer` 加回 `price_hint = SerializerMethodField()`
        ⇒ 键又出现 ⇒ 本用例红（键断言，不是值断言）。
        """
        order = Order.objects.create(
            order_no="20260928000000000096", game=self.game,
            status=OrderStatus.IN_PROGRESS, hall_price=Decimal("30.00"),
        )
        detail = self.taker_client.get(f"/api/business/orders/{order.order_no}/").json()
        self.assertEqual(detail["code"], 0, detail)
        self.assertIn("hall_price", detail["data"])        # 前提交互可测：载荷真的取到了
        self.assertNotIn("price_hint", detail["data"])     # ⭐ 键不存在（⛔ 不是值为假）

    # ── 幂等 / 重复 / 并发 ─────────────────────────────────────────────────

    def test_a_second_taker_cannot_take_the_same_mirror_row(self):
        """第二个打手接同一行 ⇒ 明确报错 + **只有一张订单** + **上游只被调用一次**。"""
        stub = _StubClient()
        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(stub)):
            first = self._claim(self.taker_client, self.mirror.pk).json()
            second = self._claim(self.other_taker_client, self.mirror.pk).json()
        self.assertEqual(first["code"], 0, first)
        self.assertEqual(second["code"], ErrorCode.ORDER_ALREADY_TAKEN, second)
        self.assertEqual(second["msg"], jidanpu_common.ALREADY_TAKEN_MESSAGE)
        self.assertIsNone(second["data"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(stub.paths, [REC_ORDER_PATH])

    def test_the_same_taker_double_clicking_gets_an_explicit_error_and_no_second_order(self):
        stub = _StubClient()
        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(stub)):
            first = self._claim(self.taker_client, self.mirror.pk).json()
            again = self._claim(self.taker_client, self.mirror.pk).json()
        self.assertEqual(first["code"], 0, first)
        self.assertNotEqual(again["code"], 0, again)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(stub.paths, [REC_ORDER_PATH])

    def test_a_row_already_being_claimed_by_me_reports_processing(self):
        """重复认领要**幂等或明确报错**：行仍在我手上 `claiming` ⇒ 回「正在抢单」，
        ⛔ 不再排第二条事件、⛔ 不再打一次上游（这条钉的是「不许双开一张单」的在飞那一档）。"""
        mirror = self._mirror(remote_id="20260928000000000912")
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(
            status=JidanpuUpstreamHallOrder.Status.CLAIMING, claiming_by=self.taker,
        )
        stub, response = self._run_with(self.taker_client, pk=mirror.pk)
        body = response.json()
        self.assertEqual(body["code"], ErrorCode.ORDER_ALREADY_TAKEN, body)
        self.assertEqual(body["msg"], jidanpu_common.IN_PROGRESS_MESSAGE)
        self.assertEqual(stub.calls, [])
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(
            JidanpuUpstreamHallOrder.objects.get(pk=mirror.pk).status,
            JidanpuUpstreamHallOrder.Status.CLAIMING,
        )

    def test_an_unknown_mirror_id_is_a_clean_taken_error(self):
        """不存在的 id ⇒ 业务失败码（⛔ 不是 500、⛔ 不泄露这一行存不存在）。"""
        stub, response = self._run_with(self.taker_client, pk=987654321)
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], ErrorCode.ORDER_ALREADY_TAKEN, body)
        self.assertIsNone(body["data"])
        self.assertEqual(stub.calls, [])
        self.assertEqual(Order.objects.count(), 0)


# ══ ③ 并发：**真的**两条并行请求（TransactionTestCase + 栅栏）════════════════════
#: ⚠️ 为什么必须 `TransactionTestCase`：`TenantTestCase`（= Django `TestCase`）把每条测试包在
#:    一个事务里 ⇒ **其他线程看不到未提交的夹具**，那种「并发」会恒绿（`test_claim_concurrency.py`
#:    的 docstring 记了这条教训）。⚠️ `django_tenants` 没有 `TenantTransactionTestCase`
#:    ⇒ 租户夹具照 `TenantTestCase.setUpClass` 自己搭（同一份形状，⛔ 不发明第二套）。

TENANT_SCHEMA = "test"
COMPETITORS = 2
_BARRIER_TIMEOUT = 10


class UpstreamDepositClaimTests(UpstreamHallClaimEndpointTests):
    """上家单与自家单**一样**过押金闸（业务方 2026-10-20）。

    `enforce_deposit=True` 时：冻结与本地预占同一个事务；余额不足在**打上游之前**就拒。
    这里直接调领域层（不走 HTTP），因为「闸开不开」是调用方传的布尔，不必造 App 令牌。
    """

    def test_enforced_claim_without_money_is_rejected_before_any_upstream_call(self):
        stub = _StubClient()
        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(stub)):
            with self.assertRaises(ClaimError) as ctx:
                with transaction.atomic():
                    jidanpu_upstream.claim_upstream_mirror(
                        user_id=self.taker.id, mirror_id=self.mirror.pk,
                        enforce_deposit=True,
                    )
        self.assertIn("押金不足", str(ctx.exception))
        self.assertEqual(ctx.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertEqual(stub.calls, [])
        self.mirror.refresh_from_db()
        self.assertEqual(
            self.mirror.status, JidanpuUpstreamHallOrder.Status.LISTED
        )
        self.assertEqual(get_wallet(self.taker).frozen, Decimal("0"))

    def test_enforced_claim_freezes_the_same_amount_as_a_native_claim(self):
        wallet = get_wallet(self.taker)
        wallet.balance = Decimal("100.00")
        wallet.save(update_fields=["balance", "updated_at"])
        stub = _StubClient()
        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(stub)):
            with transaction.atomic():
                order = jidanpu_upstream.claim_upstream_mirror(
                    user_id=self.taker.id, mirror_id=self.mirror.pk,
                    enforce_deposit=True,
                )
        self.assertIsNotNone(order)
        self.assertEqual(get_wallet(self.taker).frozen, Decimal("50"))
        self.assertEqual(get_wallet(self.taker).balance, Decimal("50.00"))


class ConcurrentUpstreamHallClaimTests(TransactionTestCase):
    """⭐ 两条**并行**的接单请求打同一行镜像单 ⇒ 恰好一张我方订单 + 恰好一次上游调用。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command(
            "migrate_schemas", schema_name="public", interactive=False, verbosity=0
        )
        cls.tenant = get_tenant_model()(schema_name=TENANT_SCHEMA)
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(
            tenant=cls.tenant, domain=TENANT_DOMAIN
        )
        cls.domain.save()
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def test_two_parallel_claims_produce_exactly_one_order_and_one_upstream_call(self):
        game = Game.objects.create(name="测试游戏甲")
        owner_user = User.objects.create_user(username=ACCOUNT, password=PASSWORD)
        binding = JidanpuUpstreamBinding.objects.create(
            user=owner_user, jidanpu_base_url=BASE_URL, account=ACCOUNT,
            password=PASSWORD, enabled=True,
        )
        takers = [
            User.objects.create_user(username=f"up-hall-racer-{i}", password=PASSWORD)
            for i in range(COMPETITORS)
        ]
        mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id=REMOTE_ID,
            owner_binding=binding, status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=game, project_text="合成项目", remote_rec_fee=Decimal("88.00"),
            upstream_payload={"orderId": REMOTE_ID},
            first_seen_at=BASE_TIME,
        )
        mirror_id, taker_ids = mirror.pk, [u.pk for u in takers]

        barrier = threading.Barrier(COMPETITORS)
        results: dict[int, object] = {}
        stubs: list[_StubClient] = []
        lock = threading.Lock()

        # ⚠️ 同步点：**先真的做限制校验**，再在栅栏上对齐——所有线程此刻都还没碰那笔条件更新。
        #    它不改变被测行为（`check_grab_limits` 照常执行、照常可能抛 `ClaimError`），
        #    只是把「读 `listed`」阶段**全部**排到「写 `claiming`」阶段之前（造出竞争窗口）。
        real_check_grab_limits = jidanpu_upstream.check_grab_limits

        def _check_limits_then_align(**kwargs):
            real_check_grab_limits(**kwargs)
            barrier.wait(timeout=_BARRIER_TIMEOUT)

        def _open_client(_binding):
            stub = _StubClient()
            with lock:
                stubs.append(stub)
            return _ClientCtx(stub)

        def _try(index):
            # 每个线程一条独立连接：先关掉残留连接，再切 schema（照 `test_claim_concurrency`）。
            connections.close_all()
            with schema_context(TENANT_SCHEMA):
                try:
                    jidanpu_upstream.claim_upstream_mirror(
                        user_id=taker_ids[index], mirror_id=mirror_id,
                    )
                    results[index] = "ok"
                except Exception as exc:      # noqa: BLE001 —— 分类要落到结果里
                    results[index] = getattr(exc, "code", repr(exc))
                finally:
                    connections.close_all()

        threads = [threading.Thread(target=_try, args=(i,)) for i in range(COMPETITORS)]
        with mock.patch.object(
            jidanpu_upstream, "check_grab_limits", side_effect=_check_limits_then_align,
        ), mock.patch.object(jidanpu, "claim_session", side_effect=_open_client):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        # ⓪ 哨兵：所有线程都跑完、都留下结果（否则样本变少也会让下面的断言成立）。
        self.assertFalse(
            [t.name for t in threads if t.is_alive()], "有线程没在 30 秒内退出"
        )
        self.assertEqual(
            len(results), COMPETITORS, f"有线程没留下结果（死在 try 之外）: {results}"
        )
        # ① 恰好一个成功；② 其余全部是「已被接走」（不是别的错）。
        winners = [i for i, value in results.items() if value == "ok"]
        self.assertEqual(len(winners), 1, f"结果: {results}")
        losers = [value for value in results.values() if value != "ok"]
        self.assertEqual(
            set(losers), {ErrorCode.ORDER_ALREADY_TAKEN}, f"失败原因必须统一: {results}"
        )
        # ③ 真实出口之一：库里**恰好一张**我方订单，且镜像行指向它。
        self.assertEqual(Order.objects.count(), 1)
        order = Order.objects.get()
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)
        self.assertEqual(mirror.promoted_order_id, order.pk)
        self.assertEqual(mirror.claiming_by_id, taker_ids[winners[0]])
        # ④ 真实出口之二：上游**恰好被调用一次**（整段只允许一次 `RecOrderByInternal`）。
        self.assertEqual(sum(len(stub.calls) for stub in stubs), 1, stubs)
        self.assertEqual(
            [path for stub in stubs for path in stub.paths], [REC_ORDER_PATH]
        )


# ══ ④ 点卡先探一跳（僵尸窗治理二期③）：claim 前滞卡的新鲜段查复核 ═══════════════
#: 大厅段查端点路径**逐字**（⛔ 不从模块常量派生，E-0024 同族）。
RECHECK_HALL_PATH = "/OrderAjax/GetInternalOrderGrabbingList"
#: 绑定默认 `hall_poll_seconds`=30 ⇒ 阈值 90 秒；「落后 400 秒」稳超阈值、「落后 10 秒」稳不超。
STALE_LAG = timedelta(seconds=400)
FRESH_LAG = timedelta(seconds=10)


class _RecheckStubClient:
    """SDK 边界替身：段查复核（按 `segment` 真值表翻页）＋可选接单（成功码 `code=1`，E-0087 同族）。
    `fail_hall` ⇒ 段查询抛 `TransportError`；`on_hall_query` ⇒ 每次段查询取完行后回调
    （演并发：复核进行中行被别的打手占走；回调在本用例事务里跑，回滚由测试框架收）。"""

    def __init__(self, *, segment=None, fail_hall=False, on_hall_query=None, per_page=2,
                 shell_count=None):
        self.segment = [dict(row) for row in (segment or [])]
        self.fail_hall = fail_hall
        self.on_hall_query = on_hall_query
        self.per_page = per_page
        #: 覆盖段壳里的 `count`（演「上游自报 count 与行流矛盾」的假空页，E-0257 同族）。
        self.shell_count = len(self.segment) if shell_count is None else int(shell_count)
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {}

    def ensure_login(self):
        pass

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, dict(kwargs.get("query") or {})))
        if path == RECHECK_HALL_PATH:
            if self.fail_hall:
                raise TransportError("段查复核传输失败（合成异常）")
            if self.on_hall_query is not None:
                self.on_hall_query()
            page = int(kwargs["query"]["page"])
            start = (page - 1) * self.per_page
            page_rows = self.segment[start:start + self.per_page]
            self.last_envelope = {
                "code": 0, "msg": "success", "data": page_rows, "count": self.shell_count,
            }
            return [dict(row) for row in page_rows]
        if path == REC_ORDER_PATH:
            self.last_envelope = {
                "code": 1, "msg": "接单成功，请到「我的订单」里查看订单资料", "data": "",
            }
            return ""
        raise AssertionError(f"未预期的端点：{method} {path}")

    @property
    def paths(self) -> list[str]:
        return [path for _method, path, _kwargs in self.calls]

    @property
    def hall_calls(self) -> int:
        return sum(1 for _m, p, _k in self.calls if p == RECHECK_HALL_PATH)


class ClaimStaleRecheckTests(TenantTestCase):
    """⭐ 点卡先探一跳（2026-11-05 拍板，计划 docs/plans/2026-11-05-hall-claim-stale-recheck.md）：
    claim 在任何本地写入之前对滞卡做新鲜段查复核。判据打真实出口（响应信封 / 行终态 /
    台账行 / 桩的调用集合与次数，⛔ 不打 logger）；夹具全合成值（E-0077）。"""

    def setUp(self):
        self.game = Game.objects.create(name="复核测试游戏")
        self.owner_user = User.objects.create_user(
            username=ACCOUNT, password=PASSWORD, first_name="测试客服甲"
        )
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.owner_user, jidanpu_base_url=BASE_URL, account=ACCOUNT,
            password=PASSWORD, enabled=True,
        )
        self.taker_client = make_logged_in_client(
            username="up-recheck-taker", groups=("代练员",), games=[self.game],
            nodes=("grab_hall.take_order", "order.view_detail"),
        )
        self.taker = User.objects.get(username="up-recheck-taker")
        self.other_taker = User.objects.create_user(
            username="up-recheck-other", password=PASSWORD
        )
        self.mirror = self._mirror()

    def _mirror(self):
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id=REMOTE_ID,
            owner_binding=self.binding,
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game,
            project_text="合成项目", remote_remark="合成备注",
            remote_role_name="合成角色", remote_rec_fee=Decimal("88.00"),
            upstream_payload={
                "orderId": REMOTE_ID, "orderContent": "合成需求",
                "orderRemark": "合成备注", "roleName": "合成角色",
                "gameAcc": "acc-synth", "zgameAcc": "sub-synth", "recFee": "88.00",
            },
            first_seen_at=BASE_TIME,
        )

    def _set_lag(self, lag: timedelta) -> None:
        JidanpuUpstreamHallOrder.objects.filter(pk=self.mirror.pk).update(
            last_seen_at=timezone.now() - lag
        )

    def _segment_row(self, order_id: str) -> dict:
        return {"orderId": order_id, "gameName": self.game.name, "recFee": "88.00"}

    def _run(self, stub, *, pk=None):
        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(stub)):
            return self.taker_client.post(
                CLAIM_PATH.format(pk=self.mirror.pk if pk is None else pk)
            )

    def test_a_stale_card_absent_upstream_answers_1012_and_defuses_it(self):
        """滞卡＋复核答「不在」⇒ `1012` + 行 gone + 全套留痕 + 零接单写路径。
        桩只被问段查（第 1 页有行无此单 → 第 2 页空＝段尾），`RecOrderByInternal` 一次不打；
        台账成因句 = `CLAIM_STALE_RECHECK_GONE_REASON`（同 kind 不同因）。"""
        from apps.business.models import HallExitSnapshot

        stub = _RecheckStubClient(segment=[self._segment_row("20260928000000009999")])
        self._set_lag(STALE_LAG)
        response = self._run(stub)
        body = response.json()
        self.assertEqual(body["code"], ErrorCode.ORDER_ALREADY_TAKEN, body)
        self.assertEqual(body["msg"], jidanpu_common.ALREADY_TAKEN_MESSAGE, body)
        self.assertIsNone(body["data"])
        # 桩的调用集合：只有段查（第 1 页有行 → 翻第 2 页确认空段尾），⛔ 无接单写端点。
        self.assertEqual(stub.paths, [RECHECK_HALL_PATH, RECHECK_HALL_PATH])
        self.assertNotIn(REC_ORDER_PATH, stub.paths)
        self.assertEqual(Order.objects.count(), 0)
        # ⭐ DB 行终态（E-0009）。
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.GONE)
        self.assertIsNotNone(self.mirror.gone_at, "复核下架必须留 gone_at")
        snaps = HallExitSnapshot.objects.filter(mirror=self.mirror)
        self.assertEqual(snaps.count(), 1)
        self.assertEqual(snaps.first().exit_kind, HallExitSnapshot.ExitKind.WITHDRAWN)
        events = JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone")
        self.assertEqual(events.count(), 1)
        self.assertEqual(
            events.first().reason, jidanpu_upstream.CLAIM_STALE_RECHECK_GONE_REASON
        )
        self.assertEqual(events.first().payload.get("upstream_binding_id"), self.binding.pk)

    def test_a_stale_card_still_in_the_segment_walks_the_existing_chain(self):
        """滞卡＋复核答「在」⇒ 照常走既有链路（一字不改）：转正 + 建单 + 零消失台账。
        比现状只多一次段查查询（本线增量本身）；⛔ 无 hall_gone 台账、无 withdrawn 灰卡
        （复核成功 = 静默放行）。"""
        from apps.business.models import HallExitSnapshot

        stub = _RecheckStubClient(segment=[self._segment_row(REMOTE_ID)])
        self._set_lag(STALE_LAG)
        response = self._run(stub)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(stub.paths, [RECHECK_HALL_PATH, REC_ORDER_PATH])
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").count(), 0
        )
        self.assertEqual(
            HallExitSnapshot.objects.filter(
                mirror=self.mirror,
                exit_kind=HallExitSnapshot.ExitKind.WITHDRAWN,
            ).count(),
            0,
        )

    def test_a_fresh_card_runs_zero_recheck_queries(self):
        """非滞卡（`last_seen_at` 新鲜）⇒ **零复核查询**（钉桩的调用次数）：
        桩只被问 `RecOrderByInternal`，段查端点一次都不打。"""
        stub = _RecheckStubClient(segment=[])
        self._set_lag(FRESH_LAG)
        response = self._run(stub)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(stub.paths, [REC_ORDER_PATH])
        self.assertEqual(stub.hall_calls, 0)

    def test_a_failed_recheck_fails_open_into_the_existing_chain(self):
        """复核查询失败（传输层异常）⇒ **fail-open**：照常走既有抢单链路，⛔ 不因复核失败拒绝。

        段查第一跳就抛 `TransportError` ⇒ 放行：转正 + 建单，无消失台账、无下架。"""
        stub = _RecheckStubClient(fail_hall=True)
        self._set_lag(STALE_LAG)
        response = self._run(stub)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(stub.paths, [RECHECK_HALL_PATH, REC_ORDER_PATH])
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").count(), 0
        )

    def test_the_page_cap_fails_open_without_any_trace(self):
        """翻页撞上限仍未见 ⇒ **判不了、fail-open 放行**（2026-11-05 评审裁定，宁漏标勿误标）。
        7 行段（每页 2 行 ⇒ 4 页）前 3 页无此单 ⇒ 恰好 3 次段查后走既有链路（`REC_ORDER_PATH`），
        ⛔ 零 gone / 零台账 / 零灰卡（「没翻完」不构成下架证据）。"""
        from apps.business.models import HallExitSnapshot

        segment = [
            self._segment_row(f"2026092800000000{i:04d}") for i in range(1, 8)
        ]
        self.assertNotIn(REMOTE_ID, {row["orderId"] for row in segment})
        stub = _RecheckStubClient(segment=segment)
        self._set_lag(STALE_LAG)
        response = self._run(stub)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(stub.hall_calls, jidanpu_upstream.CLAIM_STALE_RECHECK_MAX_PAGES)
        self.assertIn(REC_ORDER_PATH, stub.paths)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").count(), 0
        )
        self.assertEqual(
            HallExitSnapshot.objects.filter(
                mirror=self.mirror,
                exit_kind=HallExitSnapshot.ExitKind.WITHDRAWN,
            ).count(),
            0,
        )

    def test_a_contradictory_empty_page_with_a_larger_count_fails_open(self):
        """空页但壳里 `count` 在手且 > 已扫行数 ⇒ 段中假空页（E-0257 同族）⇒ 判不了、放行。
        ⛔ 不把「count 与行流矛盾」当「确认不在」：零 gone / 零台账，照常走既有链路。"""
        stub = _RecheckStubClient(segment=[], shell_count=5)
        self._set_lag(STALE_LAG)
        response = self._run(stub)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(stub.paths, [RECHECK_HALL_PATH, REC_ORDER_PATH])
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").count(), 0
        )

    def test_a_row_moved_during_the_recheck_answers_taken_without_a_second_trace(self):
        """并发（复核进行中行已被他人改走）⇒ 回既有「已被接走」文案，**不写第二笔**。
        落库是条件更新（`WHERE status='listed'`）：拿不到 rowcount ⇒ 零写入（无台账、无灰卡），
        行归占走它的打手。"""
        from apps.business.models import HallExitSnapshot

        def steal():
            JidanpuUpstreamHallOrder.objects.filter(
                pk=self.mirror.pk, status=JidanpuUpstreamHallOrder.Status.LISTED,
            ).update(
                status=JidanpuUpstreamHallOrder.Status.CLAIMING,
                claiming_by_id=self.other_taker.pk, claiming_at=timezone.now(),
            )

        stub = _RecheckStubClient(
            segment=[self._segment_row("20260928000000009999")], on_hall_query=steal,
        )
        self._set_lag(STALE_LAG)
        response = self._run(stub)
        body = response.json()
        self.assertEqual(body["code"], ErrorCode.ORDER_ALREADY_TAKEN, body)
        self.assertEqual(body["msg"], jidanpu_common.ALREADY_TAKEN_MESSAGE, body)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.CLAIMING)
        self.assertEqual(self.mirror.claiming_by_id, self.other_taker.pk)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").count(), 0
        )
        self.assertEqual(
            HallExitSnapshot.objects.filter(mirror=self.mirror).count(), 0
        )

    def test_the_threshold_follows_the_binding_poll_interval(self):
        """触发判据钉值：阈值 = `max(2 × hall_poll_seconds, 90)` 秒（30/180/15 三档真值表
        ＋「80/100 秒两档」边界；直接钉领域判据 `_claim_row_is_stale`，端点级四档由上面用例覆盖）。
        另钉：`last_seen_at` 为空 ⇒ 不是滞卡（照现状放行）。"""
        row = self.mirror
        now = timezone.now()
        cases = [
            (30, timedelta(seconds=80), False),
            (30, timedelta(seconds=100), True),
            (180, timedelta(seconds=300), False),
            (180, timedelta(seconds=400), True),
            (15, timedelta(seconds=100), True),
            (15, timedelta(seconds=80), False),
        ]
        for poll_seconds, lag, expected in cases:
            JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
                hall_poll_seconds=poll_seconds
            )
            binding = JidanpuUpstreamBinding.objects.get(pk=self.binding.pk)
            JidanpuUpstreamHallOrder.objects.filter(pk=row.pk).update(
                last_seen_at=now - lag
            )
            fresh_row = JidanpuUpstreamHallOrder.objects.get(pk=row.pk)
            with self.subTest(poll=poll_seconds, lag=lag):
                self.assertIs(
                    jidanpu_upstream._claim_row_is_stale(fresh_row, binding, now=now),
                    expected,
                )
        # 边界自证：`last_seen_at` 为空（从没确认过在厅）⇒ 不是滞卡（照现状放行）。
        JidanpuUpstreamHallOrder.objects.filter(pk=row.pk).update(last_seen_at=None)
        fresh_row = JidanpuUpstreamHallOrder.objects.get(pk=row.pk)
        binding = JidanpuUpstreamBinding.objects.get(pk=self.binding.pk)
        self.assertFalse(
            jidanpu_upstream._claim_row_is_stale(fresh_row, binding, now=now)
        )
