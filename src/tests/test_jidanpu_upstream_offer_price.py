"""修复轮 F2（上家鸡蛋铺线）：**发单价按绑定百分比勾兑**的三处口径收口。

业务口径（业务方）：「**打手在大厅里看到的就是接这单能拿多少**」⇒
本线的**发单价** = `recFee × 绑定.hall_price_percent / 100`（`ROUND_HALF_UP` 两位），
算法唯一出处 `jidanpu_upstream.upstream_hall_price`。
⇒ 凡是**读「这张单的价」**的地方都必须是这个勾兑价，本文件钉其中三处（另两路大厅的
先例是「payload、筛选、抢单闸门**三处同源**」）：

| # | 判据 | 修前（本条红） | 修后 |
|---|---|---|---|
| **I-1a** | **抢单价格闸门**读勾兑价 | `GrabCandidate(hall_price=row.remote_rec_fee)`（**上家原价**）⇒ 比例 50 / `recFee=88`（展示 44.00）时，`grab_price_max=60` 的打手**看到 44 却被拒**、`grab_price_min=60` 的打手**看到 44 低于下限却放行并建单** | 与投影**同一个** helper、**同一份**绑定 |
| **I-1b** | **大厅价格筛选**按展示价 | `?price_min/?price_max` 筛 `remote_rec_fee`（客户价）⇒ 展示价正好 50.00 的那行被 `?price_min=48&price_max=52` **筛掉** | 按 `upstream_hall_price` 的展示价筛（内存里，见下） |
| **M-1** | 候选集**不按行数付费** | `select_related("game")` ⇒ 投影读 `row.owner_binding` 时**每行一次查绑定表**（N+1；大厅是「整个候选集逐行序列化后内存分页」） | `select_related("game", "owner_binding")` ⇒ **N 行 1 次** |

## I-1b 的实现选择与代价（任务书要求说清）

展示价是**算出来的**（要乘**关联表**上的比例），SQL 层照抄那条公式 = 把勾兑算法写出第二份
⇒ 价格区间**不在 SQL 层**，而是**先把候选集取回来、再在内存里按展示价筛**
（`upstream_hall_price_in_range`，与投影**同一个** helper）。
**代价**：带 `?price_min/?price_max` 的大厅请求会把「其余筛选命中的整批候选行」取进内存
（被价格排除的行也取回来了）。不新增数量级——大厅本来就要把整批候选集序列化后
**内存分页**（既有残差 §14-12）。⛔ 没有「为了省事把筛选放宽/收紧」：
判定与另两路同一语义（按展示价、两端含）；⚠️ **口径修订（业务方）**：
上家没给 `recFee` 那一行的展示价是 **`0.00`**（⛔ 不是 NULL）⇒ `?price_min=0` **命中**它。

## 变异读数（本文件的三条网各自能红）

- I-1a：把 `claim_upstream_mirror` 里 `upstream_hall_price(row.remote_rec_fee, binding)`
  改回 `row.remote_rec_fee` ⇒ **两个方向同时翻转**、两条用例同时红；
- I-1b：把 `upstream_hall_price_in_range` 里的 `upstream_hall_price(...)` 换成
  `row.remote_rec_fee` ⇒ 两条筛选用例同时红；
- M-1：把 `upstream_hall_queryset` 的 `select_related` 去掉 `owner_binding`
  ⇒ 查询计数用例红（5 行镜像 ⇒ 5 次查绑定表）。

⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `kefu-up-offer`、
密码 `pw-canary-offer`、单号全零段。
⚠️ 判据打**真实出口**：端点响应信封 / 数据库行 / SQL 计数，⛔ 不打 logger。
"""
from __future__ import annotations

from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business import jidanpu
from apps.business.models import (
    Game,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    Order,
)
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import make_logged_in_client

HALL_URL = "/api/business/hall/"
#: 本线接单路径**逐字**（spec §6.1 的端点面）。
CLAIM_PATH = "/api/business/hall/upstream/{pk}/claim/"
#: 端点路径**逐字**抄 spec §2.3（⛔ 不从模块常量派生）。
REC_ORDER_PATH = "/OrderAjax/RecOrderByInternal"

BASE_URL = "https://jidanpu-offer-price.test.invalid"
ACCOUNT = "kefu-up-offer"
PASSWORD = "pw-canary-offer"
BASE_TIME = datetime(2026, 9, 29, 10, 0, tzinfo=dt_timezone.utc)

#: 勾兑比例 50%（业务方原话那一档：上家给 100 ⇒ 我方发 50）。
PERCENT = 50
#: `recFee=100.00` ⇒ 展示价 **50.00**（实测那一行）。
REC_FEE_AT_50 = Decimal("100.00")
#: `recFee=88.00` ⇒ 展示价 **44.00**（实测那一行）。
REC_FEE_AT_44 = Decimal("88.00")

#: 三路共用的门（大厅 = `grab_hall.take_order`）。
NODES = ("grab_hall.take_order",)


class _StubClient:
    """SDK 边界替身：只服务 `RecOrderByInternal`，记录每一次调用（⛔ 不接真网络）。

    ⚠️ `last_envelope` **必须给**：本仓的写成功判据（`_require_write_success`）读的是
    **业务壳**（`code > 0` = 成功，spec §2.3 实测），⛔ 不是 `call()` 的返回值
    ⇒ 桩只回一个空串、不给壳，生产代码会把它判成失败（症状是 `1017`「暂时无法接单」，
    看起来像被测路径坏了）。形状逐字照 `test_jidanpu_upstream_hall.py::_StubClient`。
    """

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


class _OfferPriceBase(TenantTestCase):
    """公共夹具：一款游戏 + 一份**比例 50** 的启用绑定 + 建镜像行的工厂。"""

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.owner_user = User.objects.create_user(
            username=ACCOUNT, password=PASSWORD, first_name="测试客服甲"
        )
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.owner_user, jidanpu_base_url=BASE_URL, account=ACCOUNT,
            password=PASSWORD, enabled=True,
        )
        # ⚠️ 前提交互可测：比例确实写进去了（不然整份夹具会以「原价接手」的样子全绿）。
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_price_percent=PERCENT
        )
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_price_percent, PERCENT)

    def _mirror(self, upstream_order_id, *, rec_fee, first_seen_at=BASE_TIME):
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id=upstream_order_id,
            owner_binding=self.binding,
            status=JidanpuUpstreamHallOrder.Status.LISTED, game=self.game,
            project_text="上家合成项目", remote_remark="上家合成备注",
            remote_role_name="上家合成角色",
            remote_rec_fee=None if rec_fee is None else Decimal(rec_fee),
            remote_game_area="安卓", remote_game_server="微信区",
            upstream_payload={"orderId": upstream_order_id},
            first_seen_at=first_seen_at,
        )


# ══ I-1a：抢单价格闸门读的是**勾兑后的发单价** ═══════════════════════════════════


class UpstreamGrabPriceGateTests(_OfferPriceBase):
    """⭐ 判据 I-1a：打手在大厅里**看到**的价与抢单闸门**判**的价必须是同一个数。

    夹具：比例 50、`recFee=88.00` ⇒ **展示价 44.00**。下面两条用例的方向相反，
    合起来把「读错了价」的两种后果都钉住（**宽松一面**与**严格一面**）：

    - `grab_price_max=60`：44 在区间内 ⇒ **必须放行并真的建单**（修前读 88 ⇒ 被拒）；
    - `grab_price_min=60`：44 低于下限 ⇒ **必须拒绝且零建单**（修前读 88 ⇒ 放行建单）。

    判别力：把 `claim_upstream_mirror` 的闸门入参改回 `row.remote_rec_fee` ⇒
    **两条同时红**（这正是本轮要修的错位）。
    """

    def setUp(self):
        super().setUp()
        self.mirror = self._mirror("UP-GATE-1", rec_fee=REC_FEE_AT_44)
        self.taker_client = make_logged_in_client(
            username="up-offer-taker", groups=("代练员",), games=[self.game],
            nodes=NODES,
        )
        self.taker = User.objects.get(username="up-offer-taker")

    def _set_limits(self, **fields):
        StaffProfile.objects.filter(user=self.taker).update(**fields)

    def _claim(self):
        stub = _StubClient()
        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(stub)):
            response = self.taker_client.post(CLAIM_PATH.format(pk=self.mirror.pk))
        return stub, response

    def test_a_taker_whose_max_is_above_the_display_price_can_claim(self):
        """⭐ 方向一（**不许误拒**）：展示价 44.00 ≤ `grab_price_max=60` ⇒ 放行 + 真的建单。

        修前的读数：闸门读上家原价 88.00 ⇒ `88 > 60` ⇒ `code=1013`、零建单
        （打手看到 44 明明买得起，却被拒）。
        """
        self._set_limits(grab_price_max=Decimal("60"))
        stub, response = self._claim()
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(stub.paths, [REC_ORDER_PATH])   # 上游**真的**被打了
        order = Order.objects.get()
        # ⭐ 拿到的就是**展示的那个数**（44.00 = 88 × 50%）——闸门与钱包同源。
        self.assertEqual(order.hall_price, Decimal("44.00"))
        self.assertEqual(Order.objects.count(), 1)

    def test_a_taker_whose_min_is_above_the_display_price_is_refused(self):
        """⭐ 方向二（**不许误放行**）：展示价 44.00 < `grab_price_min=60` ⇒ 拒 + 零建单。

        修前的读数：闸门读上家原价 88.00 ⇒ `88 ≥ 60` ⇒ **放行并真的建单**（订单
        `hall_price=44.00`）——打手设的下限被一个他看不到的数绕过去了。
        ⚠️ 断言**终态**（零订单 + 镜像行仍 `listed` + 上游零调用），⛔ 不只断响应码。
        """
        self._set_limits(grab_price_min=Decimal("60"))
        stub, response = self._claim()
        body = response.json()
        self.assertEqual(body["code"], ErrorCode.GRAB_LIMIT_EXCEEDED, body)
        self.assertIn("接单价必须在", body["msg"])       # 同码四支 ⇒ 必须能看出是哪一支
        self.assertEqual(stub.calls, [])                # 一行都没往上家打
        self.assertEqual(Order.objects.count(), 0)      # 零建单
        self.assertEqual(
            JidanpuUpstreamHallOrder.objects.get(pk=self.mirror.pk).status,
            JidanpuUpstreamHallOrder.Status.LISTED,     # 连预占都没发生
        )


# ══ I-1b：大厅价格筛选按**展示价**（与另两路同一语义）════════════════════════════


class UpstreamHallPriceFilterTests(_OfferPriceBase):
    """⭐ 判据 I-1b：`?price_min/?price_max` 命中的是**大厅里显示的那个价**。

    夹具：比例 50、两行镜像单 —— `recFee=100.00`（展示 **50.00**）与
    `recFee=88.00`（展示 **44.00**）。
    两条用例各钉一个**区间命中**（同时也就钉住了另一行被**排除**）：

    - `?price_min=48&price_max=52` ⇒ **只有**展示 50.00 那一行（评审实测的那个区间：
      修前它返回**空集**，因为 100 落在 `[48,52]` 之外）；
    - `?price_min=43&price_max=45` ⇒ **只有**展示 44.00 那一行。

    ⚠️ 判据同时断该行的 `hall_price` 字面值，把「筛的就是显示的那个数」钉死
    （⛔ 不是「筛了个碰巧同区间的别的数」）。
    """

    def setUp(self):
        super().setUp()
        self.at_50 = self._mirror("UP-PRICE-50", rec_fee=REC_FEE_AT_50)
        self.at_44 = self._mirror("UP-PRICE-44", rec_fee=REC_FEE_AT_44)
        self.viewer = make_logged_in_client(
            username="up-offer-viewer", groups=("代练员",), games=[self.game],
            nodes=NODES,
        )

    def _upstream_rows(self, **params):
        body = self.viewer.get(HALL_URL, params).json()
        self.assertEqual(body["code"], 0, body)
        return {
            item["remote_order_id"]: item
            for item in body["data"]["items"]
            if item["origin"] == "upstream"
        }

    def test_the_price_band_hits_the_row_whose_display_price_is_inside_it(self):
        """`[48, 52]` ⇒ 展示 50.00 那一行**在**、展示 44.00 那一行**不在**。"""
        rows = self._upstream_rows(price_min="48", price_max="52")
        self.assertEqual(set(rows), {"UP-PRICE-50"}, rows)
        self.assertEqual(rows["UP-PRICE-50"]["hall_price"], "50.00")   # 100 × 50%

    def test_another_band_hits_the_other_display_price(self):
        """`[43, 45]` ⇒ 反向再钉一次：展示 44.00 那一行**在**、展示 50.00 那一行**不在**。"""
        rows = self._upstream_rows(price_min="43", price_max="45")
        self.assertEqual(set(rows), {"UP-PRICE-44"}, rows)
        self.assertEqual(rows["UP-PRICE-44"]["hall_price"], "44.00")   # 88 × 50%

    def test_a_row_without_a_rec_fee_has_a_zero_display_price(self):
        """⭐ 判据 ④（**口径修订，业务方 2026-09-29**）：上家没给 `recFee` 那一行的展示价是
        **`0.00`**（⛔ 不再是「没有展示价」）⇒ 筛选取的是这个 0：

        - 不筛 ⇒ 在；
        - `?price_min=0` ⇒ **在**（`0 ≥ 0`，两端含 —— ⛔ 不是「给了任一端就不命中」）；
        - `?price_min=1` ⇒ **不在**（0 低于下界）；
        - `?price_max=0` ⇒ 在（`0 ≤ 0`）。

        失败方向：把 `upstream_hall_price` 的 `None` 分支改回 `return None` ⇒ 这条行的展示价变
        `None`（`?price_min=0` 那两条断言立刻红）。
        """
        self._mirror("UP-PRICE-NONE", rec_fee=None)
        rows = self._upstream_rows()
        self.assertIn("UP-PRICE-NONE", rows)                                   # 不筛 ⇒ 在
        self.assertEqual(rows["UP-PRICE-NONE"]["hall_price"], "0.00")          # 展示价是 0
        self.assertIn(
            "UP-PRICE-NONE", self._upstream_rows(price_min="0")                # 下界 0 ⇒ 在
        )
        self.assertIn(
            "UP-PRICE-NONE", self._upstream_rows(price_max="0")                # 上界 0 ⇒ 在
        )
        self.assertNotIn(
            "UP-PRICE-NONE", self._upstream_rows(price_min="1")                # 下界 1 ⇒ 不在
        )


# ══ M-1：候选集大小**不**决定查绑定表的次数 ══════════════════════════════════════


class UpstreamHallBindingQueryTests(_OfferPriceBase):
    """绑定读取次数不随候选单或每页行数增长。"""

    ROWS = 5

    def setUp(self):
        super().setUp()
        for index in range(self.ROWS):
            self._mirror(f"UP-Q{index}", rec_fee=Decimal("66.00"))
        self.viewer = make_logged_in_client(
            username="up-offer-q-viewer", groups=("代练员",), games=[self.game],
            nodes=NODES,
        )

    def test_binding_reads_stay_constant_when_page_size_grows(self):
        table = JidanpuUpstreamBinding._meta.db_table
        for page_size in (1, self.ROWS):
            with self.subTest(page_size=page_size):
                with CaptureQueriesContext(connection) as captured:
                    body = self.viewer.get(HALL_URL, {"page_size": page_size}).json()
                self.assertEqual(body["code"], 0, body)
                items = [item for item in body["data"]["items"] if item["origin"] == "upstream"]
                self.assertEqual(len(items), page_size)
                self.assertEqual(body["data"]["total"], self.ROWS)
                self.assertEqual({item["hall_price"] for item in items}, {"33.00"})
                binding_queries = [
                    query["sql"] for query in captured.captured_queries if table in query["sql"]
                ]
                self.assertEqual(len(binding_queries), 2, binding_queries)
