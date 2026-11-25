"""权限节点 36「将订单发到内部抢单大厅」实装为**独占闸**的 8 条钉子。

计划：`docs/plans/2026-10-26-app-hall-filter-card-and-throw-rbac.md` §5（方案④）。

背景：改造前抛大厅的类级门是 any(`order.set_offer_price`(20) / `order.send_to_grab_hall`(36) /
`order.submit_payout_price`(1162)) ⇒「只持 20 或 1162、没勾 36」的账号照样能抛单，36 形同虚设。
本次把 36 收成独占闸，并把「改状态进大厅」（通用 `/transition/`、`/batch/transition/` 的
`WAITING_GRAB` 目标）一并改判 36（业务方拍板 A / F）。

六条钉子的编号与计划 §5.3 一致：
① 只持 20、无 36 ⇒ `throw-to-hall` **403**；
② 只持 36、无 1162 ⇒ 不带价 **200** / 带价 **403**（价格写入仍归 1162）；
③ 36 + 1162 ⇒ 带价 **200**（回归：真的把价写进 `hall_price`）；
④ 只持 16、无 36 ⇒ `transition` target=待抢单 **403**、target=问题单 **200**（目标分支两侧）；
⑤ `batch_transition` 同 ④；
⑥ 只持 16 的账号 `allowed_transitions` **不含**待抢单；持 36 的账号**含**（读/写同源）。

⭐ **⑦–⑧ 是 2026-10-26 独立评审补的读侧判别钉子**（不在计划 §5.3 的六条里）：pin6 里那个
「持 36」的账号**同时持 16** ⇒ 把 `order_status.GENERIC_TRANSITION_NODES` 的待抢单那一格
删掉，读侧会落回默认的 16、pin6 **照样绿**（那一格没有判别力）。⑦ 换一个**只持
`order.view_detail` + 36、不持 16** 的账号，钉 `allowed_transitions == ["WAITING_GRAB"]`
且端点真能 200 改成功；⑧ 是同一账号的负向：除待抢单外**看不到**任何目标态。

⚠️ 本文件只钉「节点判据的形状」，不重复各端点自己的业务用例：
状态真的落库、价真的写进去、留痕真的写——那些在 `test_dispatch_api` /
`test_order_hall_price_api` / `test_hall_api` 等各自的网里。
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.rbac.seed import seed_catalog
from tests.p5_auth_helpers import make_logged_in_client

#: 三个节点 code 的**字面量**（⛔ 不从生产模块派生：E-0024 的自指陷阱）。
NODE_OFFER_PRICE = "order.set_offer_price"                 # 20「设置或更新抛单价」
NODE_SEND_TO_GRAB_HALL = "order.send_to_grab_hall"         # 36「将订单发到内部抢单大厅」
NODE_SUBMIT_PAYOUT_PRICE = "order.submit_payout_price"     # 1162「改发单价提交操作」
NODE_CHANGE_STATUS = "order.change_status"                 # 16「更改订单状态」
NODE_VIEW_DETAIL = "order.view_detail"                     # 详情读（`allowed_transitions` 的门）


class _ThrowHallGateFixture(TenantTestCase):
    """一款游戏 + 建号/建单/发请求三个辅助（每个 client 用**独立组名**，⛔ 不蹭共享 Role）。"""

    _GROUP_SEQ = 0

    def setUp(self):
        seed_catalog()
        self.game = Game.objects.create(name="抛大厅独占闸钉子游戏")

    def _client(self, username, *codes):
        """真实登录（fail-closed）：节点挂在**本用例专属**组上，避免跨用例串节点。

        ⚠️ 组名必须 ≤ 5 字（`rbac.Role.name` 是 `varchar(5)`，实测写长会
        `StringDataRightTruncation`）⇒ 用 `钉<序号>`。
        """
        type(self)._GROUP_SEQ += 1
        group = f"钉{type(self)._GROUP_SEQ}"
        client = make_logged_in_client(username=username, groups=(group,), nodes=codes)
        # ⚠️ 这里**不**显式 `grant_games`：`make_logged_in_client` 默认给「当前租户全部游戏」
        # （D-0136）⇒ 读侧 `game_scope` 与写侧 `assert_game_allowed` 都过得去。
        return client, User.objects.get(username=username)

    def _order(self, order_no, status=OrderStatus.UNASSIGNED, hall_price="88.00"):
        order = Order.objects.create(
            game=self.game,
            order_no=order_no,
            status=status,
            hall_price=Decimal(hall_price),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal(hall_price)
        )
        return order

    def _post(self, client, url, payload=None):
        return client.post(url, payload or {}, content_type="application/json")

    @staticmethod
    def _throw_url(order_no):
        return f"/api/business/orders/{order_no}/throw-to-hall/"

    @staticmethod
    def _transition_url(order_no):
        return f"/api/business/orders/{order_no}/transition/"

    @staticmethod
    def _batch_transition_url():
        return "/api/business/orders/batch/transition/"

    @staticmethod
    def _detail_url(order_no):
        return f"/api/business/orders/{order_no}/"


class ThrowHallExclusiveGatePins(_ThrowHallGateFixture):
    """①–⑥：36 独占闸 + 改状态进厅收紧；⑦–⑧：评审补的读侧判别钉子（只持 36、不持 16）。"""

    # ── ① 只持 20、无 36 ⇒ 403 ──────────────────────────────────
    def test_pin1_offer_price_node_alone_cannot_throw_to_the_hall(self):
        order = self._order("20261026000000000001")
        only_20, _ = self._client("抛闸钉子-only20", NODE_OFFER_PRICE)

        denied = self._post(only_20, self._throw_url(order.order_no))

        self.assertEqual(denied.status_code, 403, denied.content[:200])
        self.assertEqual(denied.json()["code"], 403)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)   # 状态一行没动
        # 阳性对照：**同一条路径、同一个载荷**，换成只持 36 的账号 ⇒ 200。
        # 没有这一半，上面那条 403 可能只是「这个号本来就不该抛」凑出来的。
        only_36, _ = self._client("抛闸钉子-only36", NODE_SEND_TO_GRAB_HALL)
        ok = self._post(only_36, self._throw_url(order.order_no))
        self.assertEqual(ok.status_code, 200, ok.content[:200])
        self.assertEqual(ok.json()["code"], 0, ok.json())

    # ── ② 只持 36、无 1162 ⇒ 不带价 200 / 带价 403 ───────────────
    def test_pin2_send_to_grab_hall_alone_throws_plain_but_not_with_a_price(self):
        only_36, _ = self._client("抛闸钉子-36无价", NODE_SEND_TO_GRAB_HALL)
        order = self._order("20261026000000000002")

        priced = self._post(only_36, self._throw_url(order.order_no), {"price": "66.00"})
        self.assertEqual(priced.status_code, 403, priced.content[:200])
        self.assertIn("你没有改接单价的权限", priced.json()["msg"])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)   # 被拒时一行没写
        self.assertEqual(order.hall_price, Decimal("88.00"))

        plain = self._post(only_36, self._throw_url(order.order_no))
        self.assertEqual(plain.status_code, 200, plain.content[:200])
        self.assertEqual(plain.json()["code"], 0, plain.json())
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(order.hall_price, Decimal("88.00"))     # 没带价 ⇒ 沿用原价

    # ── ③ 36 + 1162 ⇒ 带价 200（回归）────────────────────────────
    def test_pin3_nodes_36_and_1162_can_throw_with_a_price(self):
        both, _ = self._client(
            "抛闸钉子-36加1162", NODE_SEND_TO_GRAB_HALL, NODE_SUBMIT_PAYOUT_PRICE
        )
        order = self._order("20261026000000000003")

        body = self._post(
            both, self._throw_url(order.order_no), {"price": "66.00"}
        ).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(order.hall_price, Decimal("66.00"))      # 价真的写进去了

    # ── ④ transition：待抢单要 36 / 问题单只要 16 ────────────────
    def test_pin4_transition_to_waiting_grab_needs_36_but_issue_needs_only_16(self):
        only_16, _ = self._client("抛闸钉子-转16", NODE_CHANGE_STATUS)
        order = self._order("20261026000000000004")

        denied = self._post(
            only_16,
            self._transition_url(order.order_no),
            {"target": OrderStatus.WAITING_GRAB},
        )
        self.assertEqual(denied.status_code, 403, denied.content[:200])
        self.assertEqual(denied.json()["code"], 403)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)   # 403 一行没写

        # 对照：同一个账号、同一条端点，改「问题单」照旧只需 16 ⇒ 放行。
        ok = self._post(
            only_16,
            self._transition_url(order.order_no),
            {"target": OrderStatus.ISSUE},
        )
        self.assertEqual(ok.status_code, 200, ok.content[:200])
        self.assertEqual(ok.json()["code"], 0, ok.json())
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.ISSUE)

    # ── ⑤ batch_transition：同 ④ ────────────────────────────────
    def test_pin5_batch_transition_to_waiting_grab_needs_36_but_issue_needs_only_16(self):
        only_16, _ = self._client("抛闸钉子-批16", NODE_CHANGE_STATUS)
        order = self._order("20261026000000000005")

        denied = self._post(
            only_16,
            self._batch_transition_url(),
            {
                "order_nos": [order.order_no],
                "target": OrderStatus.WAITING_GRAB,
                "dry_run": False,
            },
        )
        self.assertEqual(denied.status_code, 403, denied.content[:200])
        self.assertEqual(denied.json()["code"], 403)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)

        ok = self._post(
            only_16,
            self._batch_transition_url(),
            {
                "order_nos": [order.order_no],
                "target": OrderStatus.ISSUE,
                "dry_run": False,
            },
        )
        self.assertEqual(ok.status_code, 200, ok.content[:200])
        body = ok.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], [order.order_no])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.ISSUE)

    # ── ⑥ allowed_transitions：16-only 看不到「待抢单」，36 看得到 ──
    def test_pin6_allowed_transitions_hides_waiting_grab_without_node_36(self):
        order = self._order("20261026000000000006")

        only_16, _ = self._client(
            "抛闸钉子-集合16", NODE_CHANGE_STATUS, NODE_VIEW_DETAIL
        )
        allowed_16 = only_16.get(self._detail_url(order.order_no)).json()["data"][
            "allowed_transitions"
        ]
        # 阳性哨兵：集合**没整体变空**，否则「不含待抢单」是平凡成立的。
        self.assertIn(OrderStatus.IN_PROGRESS, allowed_16)
        self.assertNotIn(OrderStatus.WAITING_GRAB, allowed_16)

        # 读/写同源：36 在端点上**能**抛 ⇒ 列表里也必须**看得到**（否则前端不渲染入口）。
        with_36, _ = self._client(
            "抛闸钉子-集合36",
            NODE_CHANGE_STATUS,
            NODE_VIEW_DETAIL,
            NODE_SEND_TO_GRAB_HALL,
        )
        allowed_36 = with_36.get(self._detail_url(order.order_no)).json()["data"][
            "allowed_transitions"
        ]
        self.assertIn(OrderStatus.WAITING_GRAB, allowed_36)

    # ── ⑦ 只持 36、无 16：读侧必须看得到「待抢单」（判别格）──────
    def test_pin7_allowed_transitions_shows_waiting_grab_to_a_36_only_account(self):
        """只持 `order.view_detail` + 36、**不持 16** 的账号：UNASSIGNED 单的列表 == 待抢单。

        ⚠️ 为什么必须补这一条（独立评审 Important #2）：pin6 里「持 36」那个
        账号**同时持 16**（`NODE_CHANGE_STATUS`）⇒ 把 `order_status.GENERIC_TRANSITION_NODES`
        的 `WAITING_GRAB` 那一格删掉，读侧会落回默认的 16，而它恰好持 16 ⇒ pin6 **照样绿**，
        那一格没有判别力。本条的账号只有 `order.view_detail` + 36：
        - 正向：`allowed_transitions == ["WAITING_GRAB"]`（本字段的契约 = 端点真会接受的集合）；
        - 写侧同源：同一个账号打同一条端点 target=待抢单 ⇒ **200**（列表里的不是画上去的）。

        ⭐ 变异自证：删掉
        `order_status.GENERIC_TRANSITION_NODES[str(OrderStatus.WAITING_GRAB)]` 那一格
        ⇒ 本条的等号变成 `[] == ["WAITING_GRAB"]`、**必红**；还原后文件逐字节一致。
        """
        order = self._order("20261026000000000007")
        only_36, _ = self._client(
            "抛闸钉子-纯36", NODE_VIEW_DETAIL, NODE_SEND_TO_GRAB_HALL
        )

        detail = only_36.get(self._detail_url(order.order_no))
        self.assertEqual(detail.status_code, 200, detail.content[:200])
        allowed = detail.json()["data"]["allowed_transitions"]
        self.assertEqual(allowed, [OrderStatus.WAITING_GRAB])

        ok = self._post(
            only_36,
            self._transition_url(order.order_no),
            {"target": OrderStatus.WAITING_GRAB},
        )
        self.assertEqual(ok.status_code, 200, ok.content[:200])
        self.assertEqual(ok.json()["code"], 0, ok.json())
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)

    # ── ⑧ 只持 36、无 16：其它目标态一个都看不到（负向）──────────
    def test_pin8_a_36_only_account_sees_no_other_transition_target(self):
        """只持 36、无 16 的账号，除「待抢单」外**看不到**任何目标态。

        与 pin7 成对：pin7 钉「该有的有」，本条钉「不该有的一样没有」——给读侧放开
        `WAITING_GRAB` 那一格，不能顺带把 16 的其它可达目标漏给这个账号。
        `can_transition` 那一半用的是 `TRANSITIONS`（待抢单的入边已不含 16）
        ⇒ 这条负向同时是那句注释「只加这一格不会把 16 放回来」的可判据形态。
        """
        order = self._order("20261026000000000008")
        only_36, _ = self._client(
            "抛闸钉子-纯36负", NODE_VIEW_DETAIL, NODE_SEND_TO_GRAB_HALL
        )

        allowed = only_36.get(self._detail_url(order.order_no)).json()["data"][
            "allowed_transitions"
        ]
        # 阳性哨兵：集合非空，否则下面「都不含」是平凡成立的。
        self.assertIn(OrderStatus.WAITING_GRAB, allowed)
        for target in OrderStatus.values:
            if target == OrderStatus.WAITING_GRAB:
                continue
            self.assertNotIn(target, allowed)
