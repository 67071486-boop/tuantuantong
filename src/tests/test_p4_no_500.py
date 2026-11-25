"""P4 的结构兜底（spec §9 #16 / `AGENTS.md` §5 的 E-0010）。

**这张网抓什么**：请求来源的值（路径段 / 查询参数 / 请求体）在没走过校验时，
进了类型化的列或查询 ⇒ **HTML 500**。P2 的 `tests/test_no_route_500.py` 只覆盖
「已注册集合 + `GET <collection>/` 的畸形查询参数」，P4 新增的端点**逃出了那张网**：

- **带路径转换器的 detail 路由**（`orders/<order_no>/refunds`、
  `performances/<int:performance_id>/adjustments`）被 `_explicit_routes()` **刻意跳过**
  （那里拼不出合法 URL，硬拼会得到 405/404 的**假红**）⇒ 必须像 P3b B1-9 那样**显式补例**。
- **写路径的请求体**（`settle` / `refunds` / `adjustments` / `transition`）原来只在各自的
  `test_*_api.py` 里按字段钉，没有一张「**这一类**请求」的网。

**三个正向对照**（缺了它们，这张网可能是空的）：

1. `test_a_p4_router_route_is_not_500_even_for_a_wrong_target` 之外，本文件先把
   「每条路由在**合法**输入下能答 200」自证一遍——否则「不是 500」可以被 404/405 凑出来。
2. ⭐ **字面 `500` 的正向对照**（spec §17.4 #3）：打桩让接口内部抛**未预期**异常 ⇒
   必须真的答 `HTTP 500`。P4 各端点原来只用 `assertRaises(RuntimeError)` 证明「没有大
   `except Exception` 吞掉」，那**没有**证明 HTTP 层是 500。同一打桩位置上另有一条
   「业务异常 ⇒ 200 + 业务码」的对照，防止把「什么都 500」也读成合格。
3. **NUL 路径**（清单 2）：`%00` 是 `unquote_to_bytes` 之后的**真 NUL 字符**，
   旧的 `<str:order_no>` 会匹配它并把 `\\x00` 送进 PostgreSQL ⇒ `DataError` ⇒ 500。
   修复是 `apps/business/converters.py` 的共享转换器（两条显式路由共用），
   畸形路径**不匹配路由** ⇒ **404**。**这不是「200 + 业务码」，而是 404**——
   形状在此写死，`assertEqual(..., 404)` 是判据。

## 为什么用**超级管理员**账号

`/settlements*` 要 `finance.payroll`、`/refunds` 要 `order.online_refund`、
`/adjustments` 的 POST 要 `finance.change_settlement_status`（RBAC 节点，非管理员 **403**）。
这条网问的是「畸形入参会不会 500」，**403 是另一个关注点**（授权）——混进来只会让判据
从「没有 500」退化成「200 或 403」。超管在 `nodes_of` / `has_node_now` 上短路，
是本用例唯一不按业务授权的账号前提（与 `test_no_route_500` 的处置同因）。
"""
import json
import pathlib
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
)
from tests.p5_auth_helpers import make_logged_in_client

#: 20 位订单号（`OrderViewSet.lookup_value_regex = r"\d{20}"`）。
ORDER_NO = "20260920000000009201"
#: ⛔ 这里**没有** `REMOTE_ORDER_ID`：P2 镜像单接单路由（第二段夹具）已随租户级鸡蛋铺线
#: 拆除 ⇒ 唯一读它的用例也删了 ⇒ 这个常量不再有人用。


class _P4RouteFixture(TenantTestCase):
    """P4 的路由 + 一份能走到各视图深处的最小数据（订单 / 绩效）。"""

    def setUp(self):
        self.client = make_logged_in_client(is_superuser=True)
        # 控制器：本文件默认**不让测试客户端把服务端异常变成测试异常**
        # ——「未预期异常 ⇒ 500」正是要断言的形状。
        self.client.raise_request_exception = False

        self.game = Game.objects.create(name="P4 兜底游戏")
        self.order = Order.objects.create(
            game=self.game,
            order_no=ORDER_NO,
            status=OrderStatus.COMPLETED,
            received_price=Decimal("100.00"),
        )
        taker = User.objects.create_user(username="p4-n500-taker")
        sales = User.objects.create_user(username="p4-n500-sales")
        self.taker_perf = Performance.objects.create(
            order=self.order,
            emp_type=EmpType.MAIN,
            user=taker,
            temp_amount=Decimal("60.00"),
            bonus_amount=Decimal("60.00"),
            review_status=ReviewStatus.APPROVED,
        )
        self.sales_perf = Performance.objects.create(
            order=self.order,
            emp_type=EmpType.SALES,
            user=sales,
            temp_amount=Decimal("40.00"),
            bonus_amount=Decimal("8.00"),
            commission_rate=Decimal("20.00"),
            review_status=ReviewStatus.APPROVED,
        )

    # ── 请求工具 ────────────────────────────────────────────────
    def _post_json(self, url, payload):
        return self.client.post(
            url, data=json.dumps(payload), content_type="application/json"
        )

    def _assert_envelope_200(self, response, label):
        """畸形入参的**合格形状**：HTTP 200 + 标准信封（业务失败也走 200）。"""
        self.assertNotEqual(
            response.status_code, 500,
            f"{label}：畸形入参造成了 HTML 500（body={response.content[:300]!r}）",
        )
        self.assertEqual(response.status_code, 200, f"{label}：{response.content[:300]!r}")
        self.assertIn("code", response.json(), f"{label}：响应不是标准信封")


class P4RouteMalformedInputTests(_P4RouteFixture):
    """每条 P4 路由 × 每个方法 × 畸形入参 ⇒ **不是 500**（T2 的主体）。"""

    def test_settlement_list_survives_malformed_query_params(self):
        """`GET /settlements`：?user=abc / ?emp_type=BOGUS / ?date_from=garbage / ?page=abc。"""
        malformed = {
            "user": "abc",
            "emp_type": "BOGUS",
            "review_status": "BOGUS",
            "settle_status": "BOGUS",
            "order_no": "x" * 100,
            "date_from": "garbage",
            "date_to": "garbage",
            "page": "abc",
        }
        self._assert_envelope_200(
            self.client.get("/api/business/settlements", malformed), "GET /settlements"
        )

    def test_unattributed_survives_malformed_query_params(self):
        """`GET /settlements/unattributed`：同一条腿（它由 `FinanceReadPermissionMixin` 守）。"""
        self._assert_envelope_200(
            self.client.get("/api/business/settlements/unattributed", {"page": "abc"}),
            "GET /settlements/unattributed",
        )

    def test_settle_survives_malformed_bodies(self):
        """`POST /settlements/settle`：`performance_ids` 的每种畸形形状。

        ⚠️ `performance_ids` 是 spec §7.1 表格**逐字点名**的第一个入参：
        元素不是整数 / 是字符串 / 空列表 / 超长列表都会让 `filter(id__in=...)` 抛
        `ValueError`/`DataError` ⇒ 500。这里逐条实测它们都走 `1000`。
        """
        url = "/api/business/settlements/settle"
        cases = [
            ("空对象", {}),
            ("null", {"performance_ids": None}),
            ("空列表", {"performance_ids": []}),
            ("字符串而非列表", {"performance_ids": "abc"}),
            ("元素不是整数", {"performance_ids": ["x"]}),
            ("元素是 null", {"performance_ids": [None]}),
            ("超长列表", {"performance_ids": list(range(1, 600))}),
            ("嵌套列表", {"performance_ids": [[1]]}),
            ("超长 reason", {"performance_ids": [self.sales_perf.id], "reason": "z" * 500}),
        ]
        for label, payload in cases:
            with self.subTest(case=label):
                self._assert_envelope_200(
                    self._post_json(url, payload), f"POST {url} / {label}"
                )

    def test_adjustment_chain_get_survives_malformed_query_params(self):
        """`GET /performances/{id}/adjustments`：带转换器的 detail 路由（显式补例）。"""
        url = f"/api/business/performances/{self.sales_perf.id}/adjustments"
        self._assert_envelope_200(
            self.client.get(url, {"page": "abc", "settle_status": "BOGUS"}),
            f"GET {url}",
        )

    def test_adjustment_chain_post_survives_malformed_bodies(self):
        """`POST /performances/{id}/adjustments`（P4-11 的新写路径）：同上，**两个方法都覆盖**。

        ⚠️ 这一条是 P4-11 评审追加的义务：`test_no_route_500` 的 explicit-routes 腿
        会跳过带转换器的路由，而这条路径现在是 **GET + POST 两个方法**——
        只补 GET 会让 P4 唯一的新写路径**静默逃出**这张网。
        """
        url = f"/api/business/performances/{self.sales_perf.id}/adjustments"
        cases = [
            ("空对象", {}),
            ("amount 非十进制", {"amount": "abc", "reason": "x", "request_id": "n1"}),
            ("amount 超列宽", {"amount": "9" * 20, "reason": "x", "request_id": "n1"}),
            ("amount 是 null", {"amount": None, "reason": "x", "request_id": "n1"}),
            ("reason 超列宽", {"amount": "1.00", "reason": "z" * 500, "request_id": "n1"}),
            ("request_id 超列宽", {"refund_total": "1.00", "reason": "x", "request_id": "z" * 100}),
            ("request_id 是 null", {"amount": "1.00", "reason": "x", "request_id": None}),
        ]
        for label, payload in cases:
            with self.subTest(case=label):
                self._assert_envelope_200(
                    self._post_json(url, payload), f"POST {url} / {label}"
                )

    def test_refund_records_get_survives_malformed_query_params(self):
        """`GET /orders/{order_no}/refunds`：带转换器的 detail 路由（显式补例）。"""
        url = f"/api/business/orders/{ORDER_NO}/refunds"
        self._assert_envelope_200(
            self.client.get(url, {"page": "abc"}), f"GET {url}"
        )

    def test_refund_post_survives_malformed_bodies(self):
        """`POST /orders/{order_no}/refunds`：`refund_total` / `reason` / `request_id` 的畸形形状。"""
        url = f"/api/business/orders/{ORDER_NO}/refunds"
        cases = [
            ("空对象", {}),
            ("refund_total 非十进制", {"refund_total": "abc", "reason": "x", "request_id": "r1"}),
            ("refund_total 超列宽", {"refund_total": "9" * 20, "reason": "x", "request_id": "r1"}),
            ("reason 超列宽", {"refund_total": "1.00", "reason": "z" * 500, "request_id": "r1"}),
            ("request_id 超列宽", {"refund_total": "1.00", "reason": "x", "request_id": "z" * 100}),
            ("request_id 缺失", {"refund_total": "1.00", "reason": "x"}),
        ]
        for label, payload in cases:
            with self.subTest(case=label):
                self._assert_envelope_200(
                    self._post_json(url, payload), f"POST {url} / {label}"
                )

    def test_my_orders_settle_requests_survive_malformed_bodies(self):
        """`POST /my-orders/settle-requests/`（D-0365，2026-11-02；POST-only 字面路由）。

        ⚠️ 与 `/settlements/settle` 同一条路数：只收 POST 的字面路由会被
        `test_no_route_500` 的 explicit-routes 腿按 `_request_method` 推导成 POST
        并打畸形体，但那一腿的形状是**一条通用畸形体**；这里按本网的惯例把
        `performance_ids` 的每种畸形形状逐条量一遍（列表 / 元素 / null / 超长）。
        """
        url = "/api/business/my-orders/settle-requests/"
        cases = [
            ("空对象", {}),
            ("null", {"performance_ids": None}),
            ("空列表", {"performance_ids": []}),
            ("字符串而非列表", {"performance_ids": "abc"}),
            ("元素不是整数", {"performance_ids": ["x"]}),
            ("元素是 null", {"performance_ids": [None]}),
            ("超长列表", {"performance_ids": list(range(1, 600))}),
            ("嵌套列表", {"performance_ids": [[1]]}),
        ]
        for label, payload in cases:
            with self.subTest(case=label):
                self._assert_envelope_200(
                    self._post_json(url, payload), f"POST {url} / {label}"
                )

    def test_my_orders_export_survives_malformed_query_params(self):
        """`GET /my-orders/export`（D-0365）：接受 GET ⇒ 进 explicit-routes 腿 + 本网补例。

        ⚠️ 成功时这个端点回的是 **xlsx 字节、不是信封**（照 `PerformanceExportView` 的
        「同一个 URL 两种形状」约定）⇒ 合格形状仍是「200 + 信封」——畸形参数必须走
        参数校验那条出口（`200 + code 1000`），⛔ 不能吐半截文件。
        """
        url = "/api/business/my-orders/export"
        cases = [
            {"settle_status": "BOGUS"},
            {"order_status": "IN_PROGRESS,BOGUS"},
            {"order_status": ",,"},
            {"date_type": "BOGUS"},
            {"date_from": "garbage"},
            {"date_to": "garbage"},
            {"game": "x" * 100},
            {"key1": "y" * 200},
        ]
        for query in cases:
            with self.subTest(query=query):
                self._assert_envelope_200(
                    self.client.get(url, query), f"GET {url} / {query}"
                )

    def test_transition_survives_malformed_bodies(self):
        """`POST /orders/{order_no}/transition/`：`target` 的畸形值。

        ⚠️ 这条路径是 **P2 的**，但 P4 给它挂了 `REFUNDED` 的副作用（§5.2） ⇒
        畸形 `target` 不许在「翻译钩子失败」这条新路径上变成 500。
        """
        url = f"/api/business/orders/{ORDER_NO}/transition/"
        cases = [
            ("空对象", {}),
            ("target 是未知状态", {"target": "BOGUS"}),
            ("target 是 null", {"target": None}),
            ("target 是数字", {"target": 123}),
            ("target 是列表", {"target": ["REFUNDED"]}),
            ("超长 actor", {"target": "PAUSED", "actor": "z" * 500}),
        ]
        for label, payload in cases:
            with self.subTest(case=label):
                self._assert_envelope_200(
                    self._post_json(url, payload), f"POST {url} / {label}"
                )


class P4NulPathTests(_P4RouteFixture):
    """⭐ 清单 2：路径参数里的 NUL 字节（`%00`）不许 500。

    修复点在 `apps/business/converters.py`（**共享**转换器，两条显式路由共用）。
    形状是 **404**（路由不匹配）——**不是** 500，也**不是**「200 + 业务码」。
    """

    #: `<entity>/%00/...`：`unquote_to_bytes` 之后是**真 NUL 字符**。
    NUL = "%00"

    def test_refund_post_with_nul_order_no_is_not_500(self):
        """P4-9 评审实测的原始形态：`POST /orders/%00/refunds` 曾经是 `DataError` → 500。"""
        response = self._post_json(
            f"/api/business/orders/{self.NUL}/refunds",
            {"amount": "1.00", "reason": "x", "request_id": "nul-1"},
        )
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, 404, response.content[:300])

    def test_refund_get_with_nul_order_no_is_not_500(self):
        """同一路径的 GET 方法（`RefundView.get_queryset` 也会把 NUL 送进查询）。"""
        response = self.client.get(f"/api/business/orders/{self.NUL}/refunds")
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, 404, response.content[:300])

    def test_hall_claim_with_nul_order_no_is_not_500(self):
        """⭐ **同一形态的既有端点**：`hall/<str:order_no>/claim/`（P4-9 评审同样实测到 `DataError`）。

        它**不是 P4 引入的**，但既然是同一个 `order_no` 输入面，修复必须一起覆盖——
        只在 `orders/.../refunds` 上打补丁会留下这条同型路径（E-0010 的教训）。
        """
        response = self.client.post(f"/api/business/hall/{self.NUL}/claim/")
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, 404, response.content[:300])

    # ⛔ 原 `test_remote_hall_claim_with_nul_in_either_segment_is_not_500` 已退场：它测的
    # **P2（租户级）** 镜像单接单路由（`hall/remote/<source_system>/<remote_order_id>/claim/`）
    # 连同 `OrderViewSet.remote_claim` 随租户级鸡蛋铺线整条拆除
    # （`docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md`）。
    # ⚠️ **它今天是一条恒真判据**：路由不存在 ⇒ 任何 URL 都回 404 ⇒ 那两条断言**永远通过**
    # （E-0065 同族），⛔ 留着它比删掉更糟。`remote_ref` 转换器本身仍被保留下来的
    # `hall/upstream/<int:pk>/claim/` 那类路由用着，兜底面**没有消失**（只是不再是这一条）。

    def test_the_remote_hall_route_set_is_empty_after_the_teardown(self):
        """结构判据（**2026-10-08 改准**）：全仓路径里带 `hall/remote/` 的路由集合**现在是空的**。

        ⛔ **原名 `test_every_remote_hall_route_uses_the_shared_remote_ref_converter`**：
        原判据断言那个集合**恰好等于** `["hall/remote/<remote_ref:source_system>/"
        "<remote_ref:remote_order_id>/claim/"]`——也就是「凡这一族路由，两个段都必须用共享的
        `remote_ref` 转换器（裸 `<str:...>` 会让 `%00` 漏进 PostgreSQL ⇒ 500）」。
        那条路由随租户级鸡蛋铺线整条拆除 ⇒ 集合变空、原断言**在空集合上红**
        （这正是「恰好集合」不该被放宽成「包含」的现场）。

        ⚠️ **改准后的判据不是放宽、也不是空转**：它钉的是「**这一族路由不再存在**」。
        将来谁把一条裸 `<str:...>` 的 `hall/remote/...` 路由加回来 ⇒ 集合非空 ⇒ 本用例红
        （那时要照原判据的形状把「两段都用 `remote_ref`」那条判据一并补回来）。
        """
        remote_routes = sorted(
            str(p.pattern)
            for p in self._all_url_leaves()
            if "hall/remote/" in str(p.pattern)
        )
        # ⚠️ 仍是**恰好集合**断言（空集合也是恰好集合）：⛔ 不是 `assertFalse` 之外的放宽，
        #    也不是 `>=` / `toContain`。多一条 ⇒ 红（说明有人把那一族路由加回来了）。
        self.assertEqual(
            remote_routes,
            [],
            "`hall/remote/` 这一族路由不该再存在——它随租户级鸡蛋铺线整条拆除"
            "（docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md）。"
            "若有人把它加回来：请照原判据恢复「两个段都必须用 <remote_ref:...>」那条钉子"
            "（裸 <str:...> 会让 %00 逃进 PostgreSQL ⇒ 500，见 apps/business/converters.py）",
        )

    #: 本仓对「路径段里不许出现 NUL」的两道已知防线（见 `apps/business/converters.py`
    #: 的「只覆盖显式 path() 里用 <order_no:...> 的那两条路由」那一段边界）：
    #: ①共享转换器 `OrderNoConverter`；②`OrderViewSet.lookup_value_regex = r"\d{20}"`。
    SAFE_ORDER_NO_MARKERS = ("<order_no:order_no>", r"(?P<order_no>\d{20})")

    def _all_url_leaves(self):
        """递归展开 `get_resolver()`：**全仓**（含所有 app 的 urls 与 router 生成的路由）。

        ⚠️ **为什么不直接数 `apps.business.urls.urlpatterns`**（F-6）：那样只能看见
        **显式 `path()`**，看不见 DRF router 从 `OrderViewSet` 生成的 18 条
        `orders/(?P<order_no>…)` 路由，也看不见其它 app 的 urlconf——
        注释里那句「全仓没有裸 `<str:order_no>`」**比断言宽**（E-0011 的同族）。

        展开后同时得到 `RegexPattern` 与 `RoutePattern` 两种叶子，**不需要**
        给 `re_path` 另写分支：判据只读 `str(pattern)`，对两者一致。
        """
        from django.urls import URLResolver, get_resolver

        leaves = []

        def walk(patterns):
            for pattern in patterns:
                if isinstance(pattern, URLResolver):
                    walk(pattern.url_patterns)
                else:
                    leaves.append(pattern)

        walk(get_resolver().url_patterns)
        return leaves

    def _all_url_leaves_with_prefix(self):
        """同 `_all_url_leaves`，但每片叶子带上**它自己的挂载前缀**（`(前缀, 叶子)`）。

        ⚠️ 前缀 = 从根到这片叶子沿途所有 `URLResolver` 的 `str(pattern)` **顺序拼接**
        （本仓的 `include()` 全是字面前缀，⛔ 没有正则 / 参数化的 `include` ⇒ 拼出来的就是
        那条路由真正的前缀）。
        ⭐ **为什么需要它**（T6）：本仓从 T6 起有**两条对外通道**
        （`api/open/` 与 `api/taker/`），而它们各自的 `v1/…` 路径族**按契约逐字同形**
        ⇒ 只按 `str(pattern)` 数会把"两个不同挂载点下的两条不同路由"误读成"重复注册"。
        判据要看的是**同一个前缀下有没有重复**，所以必须先带上前缀。
        """
        from django.urls import URLResolver, get_resolver

        leaves = []

        def walk(patterns, prefix):
            for pattern in patterns:
                if isinstance(pattern, URLResolver):
                    walk(pattern.url_patterns, prefix + str(pattern.pattern))
                else:
                    leaves.append((prefix, pattern))

        walk(get_resolver().url_patterns, "")
        return leaves

    def test_the_shared_converter_routes_are_exactly_these(self):
        """结构判据：**全仓**（`get_resolver()` 展开）用 `<order_no:...>` 的**恰好**是这些。

        ⚠️ 这条不是同义反复：它挡住「有人新增一条 `<str:order_no>` 路由」——
        那种路由**逃出**共享转换器，NUL 会重新变成 500，而上面三条用例**不会红**
        （它们只打已知的那几条路径）。判据从解析器里数，不手抄名单。

        ⚠️ **为什么是「这三条」**（订单列表第一批）：第三条
        `orders/<order_no:order_no>/copy` 是**新端点**，它按既有手法用了**共享转换器**
        ⇒ 期望集合按实测扩一项，**判据一个字没放宽**（仍是等号）。
        ⚠️ 方法名原先叫 `test_the_two_shared_converter_routes_are_the_only_order_no_path_params`
        ——「two」在加第三条路由之后就是一句假话，故改名。
        （`.agents/notes/implemented/bug-fix/2026-09-21-nul-path-500-only-on-path-converters.md`
        里引用的是**旧名**；那是历史记录，按 AGENTS.md §4「不删历史记录」保持原样。）

        ## 键形状：**挂载前缀 + pattern 串**（不是裸 pattern 串）

        **原口径**（本网一直用到 T5 为止）是把 `str(pattern)` 收成一个**扁平列表**再等号。
        它当时成立是因为**全仓只有一条 `v1/` 通道**（`api/open/`）。

        T6 落了**第二条 `v1/` 通道**（`api/taker/`，spec §2：两条通道各自的认证类只查自己
        那张表），而 spec §5 端点表第 4–6 行给它的路径**与上家侧那几条逐字同形**
        （`v1/orders/<order_no:order_no>/`、`…/images/`、`…/images/ticket/`、`…/status/`）。
        ⇒ 扁平列表里同一个 pattern 串**出现两次**（两个**不同**挂载点下的两条**不同**路由），
        等号当场红（T6 执行期实测：`First list contains 4 additional elements.
        First extra element 12: 'v1/orders/<order_no:order_no>/images/ticket/'`）。

        ⛔ **这四条重复不是"有人加了裸 `<str:order_no>`"**：实测（`get_resolver()` 按
        `URLResolver` 逐层累加前缀）每一条 `(前缀, pattern)` 的**命中数都恰好是 1**
        —— 也就是说**没有任何一条路由逃出共享转换器**。真正的兜底是下面那条
        **全仓按参数名扫**的 `test_no_repo_route_lets_nul_through_on_an_order_no_path_param`
        （它按"模板里同时含 `order_no` 与两道防线之一的标记"判，与挂载点无关，
        T6 执行期用一条**临时加的裸 `<str:order_no>` 路由**实测过它会红）。

        ⇒ 本行**只改键的粒度**（`pattern` → `(挂载前缀, pattern)`），**等号一字未动**：
        它照样能抓"新增的 pattern 串不在名单里"，而**同时**把"两条通道各有自己的
        `v1/` 路径族"这件事表达出来。⚠️ 每格的值仍是**命中数**（`Counter`）：同一个前缀下
        同一个 pattern 出现两次照样红（那才是真的"重复注册"）。
        """
        from collections import Counter

        found = Counter(
            (prefix, str(pattern.pattern))
            for prefix, pattern in self._all_url_leaves_with_prefix()
            if "<order_no:order_no>" in str(pattern.pattern)
        )
        self.assertEqual(
            found,
            Counter(
                {
                    ("", "api/auth/wxpusher/orders/<order_no:order_no>/continue"): 1,
                    ("api/internal-chat/", "orders/<order_no:order_no>/"): 1,
                    ("api/internal-chat/", "orders/<order_no:order_no>/messages/"): 1,
                    ("api/internal-chat/", "orders/<order_no:order_no>/messages/<int:message_id>/image/"): 1,
                    ("api/internal-chat/", "orders/<order_no:order_no>/join/"): 1,
                    ("api/internal-chat/", "orders/<order_no:order_no>/reopen/"): 1,
                    ("api/wecom/", "orders/<order_no:order_no>/groups/"): 1,
                    ("api/wecom/", "orders/<order_no:order_no>/group-qr/"): 1,
                    ("api/wecom/", "orders/<order_no:order_no>/chat/groups/"): 1,
                    ("api/wecom/", "orders/<order_no:order_no>/chat/messages/"): 1,
                    ("api/wecom/", "orders/<order_no:order_no>/chat/signature/"): 1,
                    ("api/wecom/", "orders/<order_no:order_no>/chat/login/"): 1,
                    # ── 业务面（`api/business/`）────────────────────────────
                    ("api/business/", "hall/<order_no:order_no>/claim/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/copy"): 1,
                    ("api/business/", "orders/<order_no:order_no>/refunds"): 1,
                    # ⭐ 交易平台抛单线（T9b）：两条新路由都吃共享转换器
                    # ⇒ 必须列进来（漏了的话畸形 order_no 会逃出 NUL 兜底）。
                    ("api/business/", "orders/<order_no:order_no>/push-to-platform/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/"): 1,
                    # ⭐ 代练通平台动作线（T10b）：四条新路由也吃共享转换器
                    # ⇒ 必须列进来（漏了的话畸形 order_no 会逃出 NUL 兜底）。
                    ("api/business/", "orders/<order_no:order_no>/platform-push/withdraw/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/price/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/time/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/message/"): 1,
                    # ⭐ 代练通「有、我们没用上」的动作线（dlt-d）：六条新路由
                    # 也吃共享转换器 ⇒ 必须列进来（漏了的话畸形 order_no 会逃出 NUL 兜底）。
                    ("api/business/", "orders/<order_no:order_no>/platform-push/up-price/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/arbitration/"): 1,
                    # ⭐ h13：同意撤销也吃共享转换器 ⇒ 同批登记。
                    ("api/business/", "orders/<order_no:order_no>/platform-push/agree-cancel/"): 1,
                    # ⭐ 取消撤销（Flag=1）同批登记。
                    ("api/business/", "orders/<order_no:order_no>/platform-push/cancel-request/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/value/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/off-sell/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/lock/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/blacklist/"): 1,
                    # ⭐ 一键抛单 B8：发单页默认值预览也吃共享转换器
                    # ⇒ 必须列进来（漏了的话畸形 order_no 会逃出 NUL 兜底）。
                    ("api/business/", "orders/<order_no:order_no>/platform-push/preview/"): 1,
                    # ⭐ dl-A：详情页补充动作 + 从所有平台撤回 / 重发 /
                    # 确认完单 / 留言读 / 进度读——八条新路由，全部吃共享 `order_no`
                    # 转换器 ⇒ 漏一条，畸形 order_no 就会逃出 NUL 兜底。
                    ("api/business/", "orders/<order_no:order_no>/peer-direct-assignment/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/peer-direct-assignment/search/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/jidanpu-direct-assignment/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/jidanpu-direct-assignment/search/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/jidanpu-direct-assignment/verify/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/finish/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/repush/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/messages/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/progress/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-pushes/withdraw-all/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/amount/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/edit/"): 1,
                    # ⚠️ `modify/` 与 `edit/` 是**同一个视图**的两个拼写（前端两条线分别
                    # 报过契约）⇒ 两条都要在集合里。
                    ("api/business/", "orders/<order_no:order_no>/platform-push/modify/"): 1,
                    ("api/business/", "orders/<order_no:order_no>/platform-push/screenshot/"): 1,
                    # ⭐ 图片存储线：三条新路由也吃共享转换器 ⇒ 必须列进来
                    # （漏了的话畸形 order_no 会逃出 NUL 兜底）。
                    ("api/business/", "orders/<order_no:order_no>/screenshots"): 1,
                    ("api/business/", "orders/<order_no:order_no>/screenshots/ticket"): 1,
                    ("api/business/", "orders/<order_no:order_no>/screenshots/zip"): 1,
                    # ⭐ booster-h5：打手受限订单详情——路径段用**共享**转换器
                    # （与 `my-orders/` 集合那条同前缀，但这条带转换器 ⇒ 在
                    # `test_no_route_500` 的 explicit-routes 腿之外、在本网之内）。
                    ("api/business/", "my-orders/<order_no:order_no>/detail/"): 1,
                    # ⭐ 打手 App 按需同步群二维码：同样吃共享转换器
                    # ⇒ 必须列进来（漏了的话畸形 order_no 会逃出 NUL 兜底）。
                    ("api/business/", "my-orders/<order_no:order_no>/screenshots/sync/"): 1,
                    # ── 上家侧对外通道（`api/open/`）────────────────────────
                    # ⭐ 开放平台对外查单端点（T4，spec §7.1）：详情路由用**共享**转换器
                    # （`apps/openapi/urls.py` 里显式 import `apps.business.converters`）。
                    ("api/open/", "v1/orders/<order_no:order_no>/"): 1,
                    # ⭐ 开放平台 v2 的**写**端点（T5，设计文档 §6.1 / §6.3）：改发单价那条
                    # 是本批**新**路径。⚠️ 同批的 `POST .../status/` **不新增** pattern
                    # （它与只读的 `status/` **同路径**、两个方法挂同一个视图类 —— 理由 =
                    # Django 路由先匹配先赢）。
                    ("api/open/", "v1/orders/<order_no:order_no>/assign-price/"): 1,
                    # ⭐ 开放平台 v2 的两条只读扩展端点（T4）：`images/` 与它同路径的认领
                    # **共用一条** pattern（同上）。
                    ("api/open/", "v1/orders/<order_no:order_no>/images/"): 1,
                    # ⭐⭐ 开放平台 v2 的**取票**端点（T10a）：本批**唯一新出现的** pattern。
                    ("api/open/", "v1/orders/<order_no:order_no>/images/ticket/"): 1,
                    ("api/open/", "v1/orders/<order_no:order_no>/status/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/status/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/assign-price/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/assign/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/reassign/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/publish/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/withdraw/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/clear-taker/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/delete/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/restore/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/audit/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/notes/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/images/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/images/ticket/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/images/<int:image_id>/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/split/"): 1,
                    ("api/integration/v1/", "orders/<order_no:order_no>/split-peer/"): 1,
                    ("api/integration/v1/", "taker/hall/<order_no:order_no>/claim/"): 1,
                    ("api/integration/v1/", "taker/orders/<order_no:order_no>/"): 1,
                    ("api/integration/v1/", "taker/orders/<order_no:order_no>/status/"): 1,
                    ("api/integration/v1/", "taker/orders/<order_no:order_no>/images/"): 1,
                    ("api/integration/v1/", "taker/orders/<order_no:order_no>/images/ticket/"): 1,
                    # ── 打手侧对外通道（`api/taker/`，T5 / T6）───────────────
                    # ⭐ T5 的**抢单**路由（spec §5 端点表第 2 行）：路径段用**共享**
                    # `order_no` 转换器（`apps/takerapi/urls.py` 里显式 import
                    # `apps.business.converters`）。⚠️ 拉大厅那条（`v1/hall/`）**不带**转换器
                    # ⇒ 它在这份集合之外，而在 `tests/test_no_route_500.py` 的显式路由腿。
                    ("api/taker/", "v1/hall/<order_no:order_no>/claim/"): 1,
                    # ⭐⭐ T6 的五条（spec §5 端点表第 4–6 行）：查单 / 改状态 / 进度图三条
                    # （含 `images/` 那条**同路径两方法**）。⚠️ 本批是**本网先报出来**的
                    # （读数见上面「改准」那一节），⛔ 不是按"应该要登记"推的。
                    ("api/taker/", "v1/orders/<order_no:order_no>/"): 1,
                    ("api/taker/", "v1/orders/<order_no:order_no>/images/"): 1,
                    ("api/taker/", "v1/orders/<order_no:order_no>/images/ticket/"): 1,
                    ("api/taker/", "v1/orders/<order_no:order_no>/status/"): 1,
                }
            ),
            "用共享转换器的 order_no 路由集合变了——新增的 `<str:order_no>` 路由"
            "会逃出 NUL 兜底（见 apps/business/converters.py）",
        )

    def test_no_repo_route_lets_nul_through_on_an_order_no_path_param(self):
        """结构判据（**全仓**）：凡路由模板里带 `order_no` 参数名的，都必须落在两道防线之一。

        这道判据比上一条**宽**：上一条只数「用了共享转换器的显式路由」，本条扫**所有**
        带 `order_no` 参数名的叶子——包括 router 从 `OrderViewSet` 生成的那 18 条
        `orders/(?P<order_no>…)`，以及将来任何 app 新加的 urlconf。

        ⚠️ **为什么这条才是「全仓没有裸 `<str:order_no>`」的正确判据**：`<str:order_no>`
        经 `path()` 之后在解析器里是 **`(?P<order_no>[^/]+)`** 这个正则（读
        `django/urls/converters.py` 的 `StringConverter.regex`），而 NUL 匹配 `[^/]+`
        正是 500 的来源（`apps/business/converters.py` 的 docstring 记着实测）。
        所以「安全」的机械形状是**模板里同时含 `order_no` 与两道防线之一的标记**：
        裸 `str` 转换器的形态不含任何标记 ⇒ 本条断言红。
        这与 `apps/business/converters.py` 自己列的两条边界一一对应。
        """
        unsafe = []
        for pattern in self._all_url_leaves():
            text = str(pattern)
            if "order_no" not in text:
                continue
            if not any(marker in text for marker in self.SAFE_ORDER_NO_MARKERS):
                unsafe.append(text)
        self.assertEqual(
            unsafe,
            [],
            f"这些路由带 order_no 路径参数却不在两道防线之内（NUL ⇒ 500）：{unsafe}",
        )


class P4UnexpectedErrorIsStillA500Tests(_P4RouteFixture):
    """⭐ 清单 4 / spec §9 #16 的**字面 500** 正向对照（§17.4 #3）。

    P4 各端点原来只用 `assertRaises(RuntimeError)` 证明「**没有**大 `except Exception` 吞掉」——
    那是**测试进程内**的形状，没有证明 HTTP 层。这里把桩打在**同一个位置**上问两种问题：

    | 桩抛什么 | 期望 | 证明了什么 |
    |---|---|---|
    | `RuntimeError`（未预期） | **HTTP 500** | 没有被伪装成业务结果（E-0010：那会让监控失明） |
    | `SettleError`（预期） | **HTTP 200 + 业务码** | 也没有「什么都 500」 |

    ⚠️ **桩打在「被查找的那个模块」**（E-0029）：`settlement_views` 是
    `from apps.business.settlement import settle_performances` ⇒ 符号住在
    `apps.business.api.settlement_views`。
    """

    URL = "/api/business/settlements/settle"

    def _post(self):
        return self._post_json(self.URL, {"performance_ids": [self.sales_perf.id]})

    def test_an_unexpected_exception_really_is_http_500(self):
        """未预期异常 ⇒ **字面 `500`**（期望值是数字字面量，不是常量）。"""
        from unittest import mock

        with mock.patch(
            "apps.business.api.settlement_views.settle_performances",
            side_effect=RuntimeError("兜底探针：未预期异常"),
        ):
            response = self._post()
        self.assertEqual(response.status_code, 500)

    def test_a_business_exception_stays_200_with_a_business_code(self):
        """同位置的**业务**异常 ⇒ 200 + 业务码（防「什么都被吞成 500」的对照）。"""
        from unittest import mock

        from apps.business.settlement import SettleError

        with mock.patch(
            "apps.business.api.settlement_views.settle_performances",
            side_effect=SettleError("探针：业务失败", 1006),
        ):
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1006)
        self.assertEqual(response.json()["msg"], "探针：业务失败")


class A2RegressionSentinelTests(TenantTestCase):
    """**A2 回归哨兵**（交接清单 A2 / 计划测试清单 T4）：`/transition/` 不许再用
    `ValueError` 翻译非法迁移。

    P3a 的 A2 把 `order_views.py` 的 `except ValueError` 改成了 `except IllegalTransition`
    ——`ValueError` 会把**写库路径**冒出的任何 `ValueError` 也翻译成业务码 1002，
    包括真正的程序错误（E-0010 的「把未预期错误伪装成业务结果」）。

    计划 T4 要求这条判据「可机械复跑」（`grep -n "except ValueError" src/apps/business/api/order_views.py`
    为空）。**把 grep 落成用例**（AGENTS §5：教训要回流到测试或规则，只写在文档里等于没写）。

    ⚠️ **必须走 AST，不能读文本**：P4-12 实测——把变异写成
    `except (IllegalTransition, ValueError) as exc:` 时，**文本版 `assertNotIn("except ValueError", …)`
    照样绿**（子串根本不匹配），哨兵给出**假保证**（同 `test_rbac_structure` B2 的假保证路径）。
    AST 版按 `ExceptHandler.type` 收集元组里的每个名字，两种写法都抓得到。
    另有两条正向对照，防止否定判据真空绿：文件读到了、且**确实**仍在
    `except IllegalTransition` 上翻译迁移失败。
    """

    #: 被测源码（相对 `src/`；本文件在 `src/tests/` 下）。
    ORDER_VIEWS = pathlib.Path(__file__).resolve().parent.parent / "apps/business/api/order_views.py"

    def test_order_views_does_not_translate_valueerror(self):
        import ast

        source = self.ORDER_VIEWS.read_text(encoding="utf-8")
        # 正向对照①：文件读到了、而且**确实**还在用 IllegalTransition 翻译迁移失败。
        self.assertIn(
            "except IllegalTransition",
            source,
            "order_views.py 里找不到 `except IllegalTransition` —— 判据的正向对照失效",
        )
        self.assertIn("class OrderViewSet", source)

        # 否定判据（AST）：任何 `except` 子句里出现 `ValueError` 都算（含元组写法）。
        offenders = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ExceptHandler) or node.type is None:
                continue
            if "ValueError" in _except_handler_names(node.type):
                offenders.append(node.lineno)
        self.assertEqual(
            offenders,
            [],
            "这些 `except` 子句又翻译 `ValueError` 了（行号见左）——它会把写库路径的"
            "ValueError 也伪装成业务码（E-0010；A2 的机械判据见计划测试清单 T4）",
        )


def _except_handler_names(node):
    """从一个 `except` 子句的类型表达式里收集被捕获的异常名（支持元组与限定名）。

    `except ValueError` → `["ValueError"]`；
    `except (IllegalTransition, builtins.ValueError)` → `["IllegalTransition", "ValueError"]`。
    ⛔ 不读文本：文本匹配漏掉元组写法（P4-12 实测的假保证路径）。
    """
    import ast

    if isinstance(node, ast.Tuple):
        names = []
        for element in node.elts:
            names.extend(_except_handler_names(element))
        return names
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.Name):
        return [node.id]
    return []
