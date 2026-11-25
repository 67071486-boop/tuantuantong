"""四个派单动作（抛单 / 撤回 / 指派 / 清除接单人）。

对应 spec §4.1 / §4.1.2 / §4.2 / §5 与 §7.4 / §10 #10。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
    SettleStatus,
)
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: DRF 对**没有自定义 `message` 的权限类**给的默认 403 文案（本项目 zh-hans 下的实测值；
#: 同 `test_rbac_endpoint_enforcement` 里的同名常量）。类级节点门 `HasNode` 就是这一类
#: ⇒「msg 是不是这句」可以用来**指认拒绝来自节点门**；旧职位门抛的是自定义文案，
#: 不可能命中这一句。⚠️ 期望值写字面量（E-0024），不引用 DRF 内部符号。
NODE_DENIED_MSG = "您没有执行该操作的权限"

#: 本文件走到的订单端点门（`OrderViewSet.ORDER_PERMISSIONS`）。
#: `groups` 只建 Group、**不放行** ⇒ 每个真实登录的夹具都要显式声明它要过的门。
DISPATCH_NODES = (
    "order.add_edit",                 # 录单
    "order.list",                     # 列表
    "order.view_detail",              # 详情
    "order.change_status",            # `/transition/`
    "order.set_offer_price",          # `/hall-price/`（改发单价；⛔ 不再开抛大厅那道门）
    "order.send_to_grab_hall",        # `/throw-to-hall/`（2026-10-26 起**独占闸**）
    "order.submit_payout_price",
    "order.withdraw_from_grab_hall",  # `/withdraw/`
    "order.assign_taker",             # `/assign/`
    "order.clear_taker",              # `/clear-taker/`
)


class _LoginMixin:
    """真实登录夹具（fail-closed，spec D11）。

    ⚠️ **不能用 `force_login`**：它产出的会话里**没有 `identity_plane`**，会被守卫中间件与
    `TenantBoundAuthentication` 按 fail-closed 不变式拒掉——结果是**每个业务请求都 401**
    （完整说明见 `tests/p5_auth_helpers.py`）。本方法顺手把 `self.client` 换成已登录的 client，
    调用点直接写 `self._login("客服")`；需要用户对象时写 `u = self._login("客服")`。

    ⚠️ 同一条测试里登录**两种角色**时必须给不同 `username`：`make_logged_in_client` 会真的
    `create_user`，同一个测试的事务里重复用户名会撞 `auth_user_username_key`。
    """

    def _login(self, *groups, username="u1", nodes=DISPATCH_NODES):
        self.client = make_logged_in_client(
            username=username, groups=groups, nodes=nodes
        )
        user = User.objects.get(username=username)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据——本文件每条用例的**调用方**
        # 都要读/写 `self.game` 名下的订单，而 `make_logged_in_client` 刻意不建
        # `StaffProfile`（没有档案 ⇒ 空集 ⇒ 读侧空结果 / 写侧业务码 1000），期望值一个字没改。
        # ⚠️ 目标侧只在各自用例补授权（口径 D-0252）：`assign_order` 判「目标打手的档案游戏
        # 集合」（无档案=无游戏=拒）⇒ `taker1` / `other1` / `taker-claimed` 这些
        # `User.objects.create_user(...)` 是**被指派的目标**，不登录也不读订单，
        # 需要 `grant_games(<taker>, self.game)`。
        # ⚠️ `test_taker_cannot_assign_a_hall_order` 的代练员也经这里拿到游戏权限：
        # 该用例断的是 `Role.CS` 闸门的 HTTP 403，那道闸在视图里排在 `get_object()` **之前**
        # （`order_views.py::assign`）⇒ 补游戏权限**遮不住**它（实测仍断到 403 + msg，没变恒真）。
        grant_games(user, self.game)
        return user

    def _grant_override_price(self, user):
        """给用户补上 `business.override_taker_price`（spec §4.3 把**改接单价**单独收口）。

        ⚠️ 「客服能带价抛单」不再只靠职位：凡是要带 `price` 打派单接口的用例都必须显式给出
        这条权限——**不要**为了让旧用例变绿而给端点开口子。
        ⚠️ 权限在登录**之后**加是安全的：Django 每次请求都从会话重新取用户对象，
        `has_perm` 实时查库（`make_logged_in_client` 的 docstring 有说明）。
        """
        from django.contrib.auth.models import Permission

        user.user_permissions.add(
            Permission.objects.get(codename="override_taker_price")
        )
        return user


class DispatchApiTests(_LoginMixin, TenantTestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME="tenant.test.com")
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game, order_no="20260920000000000001",
            hall_price=Decimal("88.00"),
        )
        self.perf = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        self.base = f"/api/business/orders/{self.order.order_no}"

    def _post(self, path, payload=None):
        return self.client.post(
            f"{self.base}/{path}/", payload or {}, content_type="application/json"
        ).json()

    def _new_order(self, no, status=OrderStatus.UNASSIGNED):
        """另建一对「订单 + 打绩效」——本类有些用例需要各自独立的起点。

        ⚠️ 绩效不是可选的：**D-0032** 之后「迁入 `UNASSIGNED` / `WAITING_GRAB`」
        会经钩子读那条 MAIN 绩效行，生产路径由录单保证它一定存在（spec §7.4）。
        """
        order = Order.objects.create(
            game=self.game, order_no=no, status=status, hall_price=Decimal("88.00")
        )
        perf = Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        return order, perf

    def _log_row(self, action, **filters):
        """取**那条具体**的新表留痕行（按 action + 关键列定位）。

        判据是 DB 行，不是 `logger.info()`（E-0009：本项目没有 `LOGGING` 配置，
        logger 版本可以在生产一行都不写的情况下全绿）。没有这一行就抛
        `OperationLog.DoesNotExist`——这正是断言本身。
        """
        return OperationLog.objects.get(
            order_no=self.order.order_no, action=action, **filters
        )

    # —— 抛单到大厅 ——
    def test_throw_to_hall_sets_status_and_price(self):
        self._grant_override_price(self._login("客服"))  # 改接单价要细粒度权限（Task 7）
        body = self._post("throw-to-hall", {"price": "66.00"})
        self.assertEqual(body["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(self.order.hall_price, Decimal("66.00"))

    def test_throw_to_hall_without_price_keeps_existing(self):
        """不传价就用订单已有的默认接单价（spec §5）。

        ⚠️ 必须**同时**断状态变了：只断「价格还是 88」在端点根本没执行时也会通过
        （非判别性断言），状态那一行是它的哨兵。
        """
        self._login("客服")
        body = self._post("throw-to-hall", {})
        self.assertEqual(body["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(self.order.hall_price, Decimal("88.00"))

    def test_throw_to_hall_response_transitions_follow_caller_nodes(self):
        """响应的 `allowed_transitions` 必须按调用方**节点**算。

        判别力对照：把视图里 `OrderSerializer(order)` 的 `context={"request": request}` 去掉
        → 节点落空集 → 该字段对所有人变空，本用例的**正向** `assertIn` 变红
        （`assertNotIn` 单独留着会恒绿，所以正向那一行是必需的哨兵）。

        口径：`WAITING_GRAB → IN_PROGRESS` 的节点集含 `16`，客服持 16 ⇒ 列出
        （spec A3 登记的放宽，与 `/transition/` 的判据同源）；状态机整体放开（D-0147 / R8）后
        集合从五条变**六条**（多出 `PENDING_REVIEW` 边，target 节点集含 16、通用端点接受它），
        `COMPLETED` 仍不列出（客服不持节点 17）。本条同时断「撤回边在集合里」与
        「集合**恰好**是这六条」（逐字全表，防「顺手放宽成全集」）。
        """
        self._login("客服")
        body = self._post("throw-to-hall", {})
        transitions = body["data"]["allowed_transitions"]
        self.assertIn(OrderStatus.UNASSIGNED, transitions)       # 客服能撤回抛单
        self.assertEqual(
            transitions,
            [
                str(OrderStatus.UNASSIGNED),
                str(OrderStatus.IN_PROGRESS),
                str(OrderStatus.PENDING_REVIEW),
                str(OrderStatus.ISSUE),
                str(OrderStatus.PAUSED),
                str(OrderStatus.REFUNDED),
            ],
        )

    def test_throw_to_hall_writes_audit(self):
        self._login("客服")
        self._post("throw-to-hall", {})
        rows = OperationLog.objects.filter(
            order_no=self.order.order_no, action="order.throw_to_hall"
        )
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        # 抛单是**自己的**动作（不再记成通用状态迁移）；`to_value` 是发单价
        self.assertEqual(row.log_type, 10)
        self.assertEqual(row.to_value, "88.00")
        self.assertEqual(row.actor, "u1")

    # —— 撤回 ——
    def test_withdraw_returns_order_to_unassigned(self):
        self._login("客服")
        self._post("throw-to-hall", {})
        body = self._post("withdraw", {})
        self.assertEqual(body["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        # 留痕断言打在**具体的 DB 行**上——`actor` 取认证态用户名，
        # 不是请求体里那个可伪造的值。撤回的文案不含变量 ⇒ 行内没有前后值可断。
        self.assertEqual(
            self._log_row("order.withdraw").actor,
            "u1",
        )

    def test_withdraw_from_unassigned_is_rejected(self):
        """没抛出去的单不能撤回——`/withdraw` 只接受 `WAITING_GRAB`。

        断**具体码**（评审 Minor 4）：只断 `!= 0` 的话，401/404 这类
        「请求根本没走到业务判定」的信封也会通过。
        """
        self._login("客服")
        response = self.client.post(
            f"{self.base}/withdraw/", {}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], ErrorCode.BUSINESS)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)

    def test_withdraw_rejects_a_taken_order(self):
        """`IN_PROGRESS → UNASSIGNED` 是指向 `UNASSIGNED` 的边，但「撤回抛单」不该能迁走代练中的单。

        没有 §5 的前置状态闸门时，`/withdraw` 对代练中的单会**静默地**执行一次「清除接单人」
        （状态 + 三元组回退），与它自己的业务含义矛盾。
        ⚠️ 绩效断言的起点是**非初始态**（`/assign` 真的写上了人）——否则「没被清掉」与
        「本来就没有人接」不加区分（判别力判据 1）。
        """
        taker = User.objects.create_user(username="taker1")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        self._login("客服")
        self._post("assign", {"user_id": taker.id})
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.user_id, taker.id)          # 前置哨兵：非初始三元组
        body = self._post("withdraw", {})
        self.assertEqual(body["code"], ErrorCode.BUSINESS)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self.perf.user_id, taker.id)          # 没被当成 clear-taker

    def test_withdraw_rejects_an_issue_order(self):
        """`ISSUE → UNASSIGNED` 也是指向 `UNASSIGNED` 的边，但入口是 `/clear-taker` / `/transition/`。

        问题单**清接单人**是正当动作（D-0032），只是它不叫「撤回抛单」。
        """
        taker = User.objects.create_user(username="taker1")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        self.order.status = OrderStatus.ISSUE
        self.order.save(update_fields=["status"])
        self.perf.user = taker
        self.perf.save(update_fields=["user"])
        self._login("客服")
        body = self._post("withdraw", {})
        self.assertEqual(body["code"], ErrorCode.BUSINESS)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.ISSUE)
        self.assertEqual(self.perf.user_id, taker.id)          # 起点非初始，且没被动

    # —— 指派 ——
    def test_assign_sets_taker_and_status(self):
        taker = User.objects.create_user(username="taker1")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        self._login("客服")
        body = self._post("assign", {"user_id": taker.id})
        self.assertEqual(body["code"], 0)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self.perf.user_id, taker.id)
        # 留痕断言打到具体的 DB 行。首次指派 ⇒ `performance.assign`，
        # `to_value` 是**发单价**（不是状态码）。
        self.assertEqual(
            self._log_row("performance.assign", to_value="88.00").actor,
            "u1",
        )

    def test_assign_accepts_a_900_char_project_and_901_is_rejected(self):
        """E-0239：指派弹窗的「接手项目」预填 `Order.project_text`（已 900）⇒
        快照列与入参必须同宽 900。129–900 字的单此前**指派必败**（报「不超过 128 个字符」）。

        边界两侧都用**字面量长度**（不引列常量，E-0024）：900 放行且落库；
        901 ⇒ 业务错 1000（不是 500）。
        """
        taker = User.objects.create_user(username="taker-long")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        self._login("客服")
        ok = self._post("assign", {"user_id": taker.id, "project": "项" * 900})
        self.assertEqual(ok["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.project, "项" * 900)

        other = User.objects.create_user(username="taker-over")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(other, self.game)
        bad = self._post(
            "assign",
            {"user_id": other.id, "project": "项" * 901},
        )
        self.assertEqual(bad["code"], 1000)
        # 判别力：必须因**长度**被拒（DRF 的 max_length 文案带 900）——
        # 否则可能是改派规则拒的，边界就没测到。
        self.assertIn("900", bad["msg"])

    def test_assign_writes_the_claim_time(self):
        """`/assign` 也算「接手」⇒ 与 `/claim` 一样写「接单时刻」。

        ⚠️ 「指派也算一次接单」是**推断**（口径 =「这个人什么时候接手的单」），
        已登记进决策行的代价；业务方若要区分「抢单」与「指派」，另开一条。
        判别力：把 `set_taker` 里的 `claimed_at` 写入删掉 ⇒ `claimed_at` 是 `None` ⇒ 本用例红。
        """
        taker = User.objects.create_user(username="taker-claimed")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        self._login("客服")

        body = self._post("assign", {"user_id": taker.id})

        self.assertEqual(body["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.user_id, taker.id)      # 哨兵：人真的写上了
        self.assertIsNotNone(self.perf.claimed_at)

    def test_assign_pulls_a_hall_order_into_in_progress(self):
        """新语义（D-0147 / spec §4.3）：大厅里的单**也能**直接指派。

        来源态由**状态机边**回答（`WAITING_GRAB → IN_PROGRESS` 的节点集含
        `order.change_status`，客服持它）；业务方那句「先清接手人、然后指派」的落点
        在**任何**状态下都是同一个动作。
        判别力：订单真的进了 `IN_PROGRESS`、绩效真的换成新接单人（不是「200 但没写」）。
        """
        taker = User.objects.create_user(username="taker1")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        self._login("客服")
        self._post("throw-to-hall", {})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)   # 前置哨兵
        body = self._post("assign", {"user_id": taker.id})
        self.assertEqual(body["code"], 0, body)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self.perf.user_id, taker.id)

    def test_assign_requires_an_existing_user(self):
        self._login("客服")
        body = self._post("assign", {"user_id": 999999})
        self.assertEqual(body["code"], ErrorCode.VALIDATION)   # 用户不存在 = 入参问题
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)

    def test_assign_rejects_an_inactive_taker(self):
        """停用号不可被指派（业务方裁定）。

        停用号登不进来 ⇒ 单子落到它头上没人能推进（`/submit` 走不通），只能靠
        `/clear-taker` 自救。判据**只在账号维度**：**不**要求持「代练员」职位
        （兼岗账号允许被指派，spec §16.4 #18）。

        判别力：去掉 `if not taker.is_active` 那一段 → 本用例拿到 `code 0`
        且订单真的进 `IN_PROGRESS`（下面两行就是为此存在的哨兵）。
        """
        taker = User.objects.create_user(username="taker1", is_active=False)
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        self._login("客服")
        body = self._post("assign", {"user_id": taker.id})
        self.assertEqual(body["code"], ErrorCode.VALIDATION)     # 载荷指向了不可用的目标
        self.assertIn("停用", body["msg"])                       # 可辨来源，不是笼统的 1000
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)   # 状态没动
        self.assertIsNone(self.perf.user_id)                          # 绩效也没动

    def test_assign_allows_a_non_taker_active_account(self):
        """**反向对照**：不持任何职位（更没有「代练员」）的**活跃**账号仍可被指派。

        没有这一条，「拒绝了停用号」与「拒绝了所有非代练员号」在测试上不可区分——
        而业务方这次**只**裁定了「拒停用号」，职位维度刻意不收（兼岗）。
        """
        taker = User.objects.create_user(username="taker-no-group")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        self._login("客服")
        body = self._post("assign", {"user_id": taker.id})
        self.assertEqual(body["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.user_id, taker.id)

    def test_double_role_account_can_assign_a_hall_order(self):
        """双角色账号（客服+代练员）对大厅单指派**能成功**（D-0147 的新语义）。

        来源态由**状态机边**回答（该账号持 `order.change_status`，边放行）；「换人」这件事
        由 `/assign/` 自己做的「先回退、再指派」保证。

        ⚠️ 断言对着**非初始态**（判别力）：订单真的在 `WAITING_GRAB`（前置哨兵）、
        绩效行已被写成**另一个人**——否则「被改成 taker」在「本来没人接」时也成立。
        """
        taker = User.objects.create_user(username="taker1")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        other = User.objects.create_user(username="other1")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(other, self.game)
        self._login("客服", "代练员", username="both1")
        self._post("throw-to-hall", {})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)   # 前置哨兵
        self.perf.user = other
        self.perf.save(update_fields=["user"])                          # 起点非初始态
        body = self._post("assign", {"user_id": taker.id})
        self.assertEqual(body["code"], 0, body)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self.perf.user_id, taker.id)
        self.assertNotEqual(self.perf.user_id, other.id)

    def test_a_user_without_the_assign_node_cannot_assign_a_hall_order(self):
        """没有 `/assign/` 的类级节点 ⇒ **HTTP 403**（已认证但无权限 = 传输层语义）。

        判据是**节点口径**：只给查看 / 抢单类节点、**不给** `order.assign_taker`
        与 `order.submit_payout_price` ⇒ 类级门 403，一行都不动。
        （旧职位门防的「代练员把大厅单拉走并指定给别人」在 D-0147 / R8 之后**是允许的**；
        「换人」这一动作由 `test_double_role_account_can_assign_a_hall_order`
        单独钉住，与本条互补。）

        ⚠️ 两个否定断言的起点都是**非初始态**：订单真的在 `WAITING_GRAB`（前置哨兵），
        绩效行已被写成**另一个人**——否则 `assertIsNone(user_id)` 对「本来没人接」
        与「没被改动」不加区分（判别力判据 1）。
        """
        taker = User.objects.create_user(username="taker1")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        other = User.objects.create_user(username="other1")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(other, self.game)
        self._login("客服", username="cs1")
        self._post("throw-to-hall", {})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)   # 前置哨兵
        self.perf.user = other
        self.perf.save(update_fields=["user"])
        self._login(
            "代练员",
            username="tk1",
            nodes=("grab_hall.take_order", "order.view_detail", "order.change_status"),
        )
        response = self.client.post(
            f"{self.base}/assign/", {"user_id": taker.id},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)
        # ⚠️ **必须断 msg，不能只断 code**：
        # P5 的 403 有多个来源（CSRF 失败 / 权限类 / 节点门），`code` 全都是 403。
        # 判据取**节点门的默认文案**（`HasNode` 没有自定义 message ⇒ DRF 默认值），
        # 并显式排除 CSRF（与 `tests/test_p5_rbac.py` 的 403 可分辨口径同一形状）——
        # 只断「不含旧职位门文案」会被**任何别的** 403 来源满足（包括 CSRF 失败），
        # 那是恒真判据、证明不了拒绝来自节点门。
        msg = response.json()["msg"]
        self.assertIn(NODE_DENIED_MSG, msg)
        self.assertNotIn("CSRF", msg)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)   # 没被拉进 IN_PROGRESS
        self.assertEqual(self.perf.user_id, other.id)                  # 也没改成 taker
        # 否定性断言打在**具体动作**上，不用 count()（Global Constraints）；
        # 抛单那行已在库里 ⇒ 这不是「看着一张空表」
        self.assertTrue(
            OperationLog.objects.filter(
                order_no=self.order.order_no, action="order.throw_to_hall"
            ).exists()
        )
        self.assertFalse(
            OperationLog.objects.filter(
                order_no=self.order.order_no,
                action__in=["performance.assign", "performance.reassign"],
            ).exists()
        )

    # —— 清除接单人 ——
    def test_clear_taker_resets_performance_triple(self):
        taker = User.objects.create_user(username="taker1")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        self._login("客服")
        self._post("assign", {"user_id": taker.id})
        # ⚠️ **前置哨兵**：`/assign` 真的把接单人写进去了吗？
        # 没有这一行，本用例在 `/assign` 完全没写绩效时**照样全绿**
        # （`assertIsNone` 对「从头到尾都是 None」与「被清回 None」不加区分）。
        # 实测：注释掉视图里的 `set_taker(perf, taker)`，缺这一行时本条**不会变红**。
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.user_id, taker.id)
        # ⚠️ 同一哨兵也适用于「接单时刻」：回退要连它一起清。
        self.assertIsNotNone(self.perf.claimed_at)
        body = self._post("clear-taker", {})
        self.assertEqual(body["code"], 0)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(self.perf.user_id)
        self.assertIsNone(self.perf.review_status)
        self.assertEqual(self.perf.bonus_amount, Decimal("0.00"))
        self.assertIsNone(self.perf.claimed_at)
        # 留痕断言打到具体的 DB 行——「原接单人」进 `target_name`
        self.assertEqual(
            self._log_row("order.clear_taker", target_name="taker1").actor,
            "u1",
        )

    def test_clear_taker_resets_a_dirty_triple(self):
        """从**非初始态**回退三元组（spec §4.1.2）。

        判别力：上面那条只断言三个字段等于它们的**默认值**——若三元组本来就是
        默认值（或回退逻辑被删掉、只改了状态），它一样绿。这里先把
        `review_status` / `bonus_amount` 写成非初始值，回退才有可观测的位移。
        """
        taker = User.objects.create_user(username="taker1")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        self._login("客服")
        self._post("assign", {"user_id": taker.id})
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.user_id, taker.id)          # 哨兵：先真的接上了
        self.perf.review_status = ReviewStatus.REJECTED
        self.perf.bonus_amount = Decimal("12.34")
        self.perf.save(update_fields=["review_status", "bonus_amount"])
        body = self._post("clear-taker", {})
        self.assertEqual(body["code"], 0)
        self.perf.refresh_from_db()
        self.assertIsNone(self.perf.user_id)
        self.assertIsNone(self.perf.review_status)
        self.assertEqual(self.perf.bonus_amount, Decimal("0.00"))

    # —— 身份 ——
    def test_a_user_without_the_throw_nodes_cannot_throw_to_hall(self):
        """没有 `/throw-to-hall/` 的类级节点 ⇒ HTTP 403，状态不动。

        判据是**节点口径**（职位名不是判据）：只给查看类节点、不给抛单的独占节点
        （`order.send_to_grab_hall`）⇒ 类级门 403。夹具会把声明的节点挂到该组上，
        所以「代练员 + 给了 16 与抛单节点」**会**被放行。
        """
        self._login(
            "代练员",
            nodes=("grab_hall.take_order", "order.view_detail", "order.change_status"),
        )
        response = self.client.post(
            f"{self.base}/throw-to-hall/", {},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)

    def test_anonymous_cannot_assign(self):
        """匿名 = 未认证 = **传输/认证层**失败：HTTP 401 + `code` 401。

        断言具体状态码与具体 code：只断 `!= 0` 时，
        业务拒绝与「请求根本没走到业务判定」不可区分。
        """
        taker = User.objects.create_user(username="taker1")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        response = self.client.post(
            f"{self.base}/assign/", {"user_id": taker.id},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], 401)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)

    # —— 四个响应的 `context=` 陷阱——
    def test_all_four_actions_return_role_aware_transitions(self):
        """四个动作的响应都必须带 `context={"request": request}`。

        `OrderSerializer.allowed_transitions` 在 `request` 为 None 时回退**空集**
        （`serializers.py:137-138`）——漏传的响应是**静默丢功能**：所有人看到的可走迁移都变空
        （四个响应点都要钉，「correct by inspection」不算证据）。

        判别力：每个场景断言一个**该角色真的能走**的目标态在集合里（正向哨兵）——
        空集过不了；把对应响应点的 `context=` 去掉，该场景变红。
        """
        taker = User.objects.create_user(username="taker1")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        # (场景, 起始状态, 登录角色, 路径, 载荷, 调用后属于该角色的某个目标态)
        scenarios = [
            ("throw-to-hall", OrderStatus.UNASSIGNED, "客服", "throw-to-hall", {},
             OrderStatus.UNASSIGNED),
            ("withdraw", OrderStatus.WAITING_GRAB, "客服", "withdraw", {},
             OrderStatus.WAITING_GRAB),
            ("assign", OrderStatus.UNASSIGNED, "客服", "assign", {"user_id": taker.id},
             OrderStatus.UNASSIGNED),
            ("clear-taker", OrderStatus.IN_PROGRESS, "客服", "clear-taker", {},
             OrderStatus.WAITING_GRAB),
        ]
        for index, (name, start, role, path, payload, expected) in enumerate(scenarios):
            with self.subTest(action=name):
                order, _perf = self._new_order(
                    f"2026092000000000{index + 10:04d}", start
                )
                self._login(role, username=f"cs-{name}")
                body = self.client.post(
                    f"/api/business/orders/{order.order_no}/{path}/",
                    payload, content_type="application/json",
                ).json()
                self.assertEqual(body["code"], 0, f"{name} 场景本身没跑通：{body}")
                self.assertIn(expected, body["data"]["allowed_transitions"])


class DispatchAtomicityTests(_LoginMixin, TenantTestCase):
    """中途失败**不留脏行**（spec §7.4 / §10 #10）。

    做法：打桩让**块内最后一步**（`record`）抛异常，断言整个操作回滚。只断「状态没变」
    不够——必须**两个方向都断**：`Order` 的字段未变 **且** `Performance` 未变
    （`/withdraw`、`/clear-taker` 的 §7.4 行里第二笔写的就是绩效三元组）。

    ⚠️ **每一条都带哨兵**：桩在抛错前记下它看到的**数据库**状态——否则「第二笔没留下脏行」
    对「请求根本没走到第一笔写入」同样成立（判别力判据 2：桩必须在第二笔写入上，
    且要证明第一笔真的发生过）。
    """

    def setUp(self):
        self.client = Client(SERVER_NAME="tenant.test.com")
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game, order_no="20260920000000000001",
            hall_price=Decimal("88.00"),
        )
        self.perf = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        self.taker = User.objects.create_user(username="taker1")   # 只做目标人，不需登录
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=拒）
        # ⇒ 被指派的目标一律补授权（「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(self.taker, self.game)
        self._login("客服", username="cs1")

    def _post(self, path, payload=None):
        return self.client.post(
            f"/api/business/orders/{self.order.order_no}/{path}/",
            payload or {}, content_type="application/json",
        )

    def _record_then_boom(self, seen):
        """返回一个「先记下当前 DB 状态、再抛错」的 `record` 替身。

        记录用的是**数据库里的真实值**（同一个事务里读得到自己未提交的写入），
        而不是内存对象——否则哨兵证明不了「写入真的落到库里了」。
        """

        def _boom(**kwargs):
            seen["order_status"] = Order.objects.get(pk=self.order.pk).status
            seen["perf_user_id"] = Performance.objects.get(pk=self.perf.pk).user_id
            # ⭐ （D2）追加：`/withdraw/` 那条路上唯一还**会真的变**的绩效列。
            seen["perf_bonus"] = Performance.objects.get(pk=self.perf.pk).bonus_amount
            raise RuntimeError("boom")

        return _boom

    def test_assign_rolls_back_order_status_when_performance_write_fails(self):
        """第二步（写绩效）失败 → 第一步的状态写入必须回滚。

        ⚠️ 哨兵由桩自己记下：它证明 `apply_transition` 的第一笔写入**真的发生过**
        （被打桩那一刻库里已经是 `IN_PROGRESS`）——否则「状态回滚成 `UNASSIGNED`」
        对「请求根本没走到那一步」同样成立（判别力判据 2）。
        ⚠️ **绩效那一半不在本用例断言**：`set_taker` 被打了桩、**根本没执行过写入**，
        对没执行过的写入断言 `user_id is None` 是恒真的；绩效写入的回滚由兄弟用例
        `..._when_audit_write_fails` 证明（桩在 `set_taker` **之后**）。
        """
        url = f"/api/business/orders/{self.order.order_no}/assign/"
        seen = {}

        def _boom(performance, user, *, allow_missing_price=False):
            # ⭐ FX2：`set_taker` 多了 `allow_missing_price` 关键字
            #    （只有代练通同步线传 True）；桩签名与真实签名对齐。
            seen["order_status"] = Order.objects.get(pk=self.order.pk).status
            raise RuntimeError("boom")

        with mock.patch(
            # ⚠️ 桩打在**被查找的那个模块**（E-0029）：A4把 `/assign/` 的动作体
            #    搬进了 `apps.business.order_writes`（唯一判据出处，spec §D5），
            #    `order_views.set_taker` 不再是这条路径上的查找点。期望值一个字未改。
            "apps.business.order_writes.set_taker", side_effect=_boom
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    url, {"user_id": self.taker.id},
                    content_type="application/json",
                )
        self.assertEqual(seen["order_status"], OrderStatus.IN_PROGRESS)  # 第一笔真的写过
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)      # 回滚了

    def test_assign_rolls_back_performance_when_audit_write_fails(self):
        """打桩点放在**绩效写入之后**（`record`）——否则
        「绩效回滚」这一半是**同义反复**：`set_taker` 被打桩后根本没跑过。
        这里 `set_taker` 真的执行了，失败发生在它之后，两半才都被证明。
        """
        url = f"/api/business/orders/{self.order.order_no}/assign/"
        seen = {}
        with mock.patch(
            # ⚠️ 桩打在**被查找的那个模块**（E-0029）：A4把 `/assign/` 的动作体
            #    搬进了 `apps.business.order_writes`（唯一判据出处，spec §D5），
            #    `order_views.record` 不再是这条路径上的查找点。期望值一个字未改。
            "apps.business.order_writes.record",
            side_effect=self._record_then_boom(seen),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    url, {"user_id": self.taker.id},
                    content_type="application/json",
                )
        # 哨兵：两笔写入在抛错前都已经落到库里
        self.assertEqual(seen["order_status"], OrderStatus.IN_PROGRESS)
        self.assertEqual(seen["perf_user_id"], self.taker.id)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)   # 状态回滚了
        self.assertIsNone(self.perf.user_id)                          # 绩效写入也回滚了

    def test_throw_to_hall_rolls_back_status_and_price_when_audit_fails(self):
        """`/throw-to-hall` 的事务边界是 ① 状态 ② 抛单价（spec §7.4）——
        两样都要回滚（`hall_price` 纳入断言），且留痕不留行。
        """
        url = f"/api/business/orders/{self.order.order_no}/throw-to-hall/"
        seen = {}
        # 带价 ⇒ 走「改接单价」的细粒度闸门，所以先补权限。
        self._grant_override_price(User.objects.get(username="cs1"))
        with mock.patch(
            "apps.business.api.order_views.record",
            side_effect=self._record_then_boom(seen),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    url, {"price": "66.00"}, content_type="application/json"
                )
        self.assertEqual(seen["order_status"], OrderStatus.WAITING_GRAB)   # 第一笔真的写过
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)   # 状态回滚了
        self.assertEqual(self.order.hall_price, Decimal("88.00"))     # 抛单价也回滚了
        # 否定性断言打在**具体行**上，不用 count()（Global Constraints）
        self.assertFalse(
            OperationLog.objects.filter(order_no=self.order.order_no).exists()
        )

    def test_withdraw_rolls_back_status_performance_and_audit(self):
        """`/withdraw` 的事务边界（spec §7.4 该行）：① 状态 ② 清 `Performance` 三元组。

        桩打在块内**最后一笔**写入（`record`）上——在 ① ② 之后，
        一次打桩就把三样都证明了。
        ⚠️ 起点必须是**非初始**的绩效行：`assertIsNone(perf.user_id)` 对「从来没接上」
        与「被清回 None」都成立（判别力判据 1）。

        ⚠️ 可观测量是钩子**仍然会写**的 `bonus_amount`（`reset_taker_performance` 照旧归零，
        与行上有没有 `user` 无关）：「池内却有接手人」的起点已被「有接手人 ⇒ 不许回池」
        的闸拒绝，`user_id` / `review_status` 半条的回滚改由
        `test_clear_taker_rolls_back_status_performance_and_audit`（走例外①，真的会清人）
        覆盖——本用例钉的「`atomic()` 一删就红」一字未减。
        """
        self._post("throw-to-hall", {})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)   # 前置哨兵
        self.assertFalse(
            OperationLog.objects.filter(
                order_no=self.order.order_no, action="order.withdraw"
            ).exists()
        )   # 否定断言的「表非空」对照见下面那行 assertTrue
        self.assertTrue(
            OperationLog.objects.filter(
                order_no=self.order.order_no, action="order.throw_to_hall"
            ).exists()
        )   # 抛单那笔留痕已经落库 ⇒ 上面的否定断言不是「看着一张空表」
        self.perf.bonus_amount = Decimal("66.00")
        self.perf.save(update_fields=["bonus_amount"])

        seen = {}
        with mock.patch(
            "apps.business.api.order_views.record",
            side_effect=self._record_then_boom(seen),
        ):
            with self.assertRaises(RuntimeError):
                self._post("withdraw", {})

        # 哨兵：桩被调用时，① 状态与 ② 绩效列**都已经写进数据库**
        self.assertEqual(seen["order_status"], OrderStatus.UNASSIGNED)
        self.assertEqual(seen["perf_bonus"], Decimal("0.00"))

        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)     # ① 回滚
        self.assertEqual(self.perf.bonus_amount, Decimal("66.00"))        # ② 回滚
        self.assertFalse(
            OperationLog.objects.filter(
                order_no=self.order.order_no, action="order.withdraw"
            ).exists()
        )   # 留痕不留行（且抛单那笔仍在 —— 见上面的 assertTrue）

    def test_clear_taker_rolls_back_status_performance_and_audit(self):
        """`/clear-taker` 的事务边界（spec §7.4 的 `IN_PROGRESS → UNASSIGNED`）。

        这是 §7.4 那一行里**第二笔写入（清三元组）真正发生**的端点
        （`/withdraw` 只是 `WAITING_GRAB` 的来源，池内本没有接单人）。
        起点是**脏三元组**，两次写入都由哨兵与回滚断言钉住。
        """
        self._post("assign", {"user_id": self.taker.id})
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)   # 前置哨兵
        self.assertEqual(self.perf.user_id, self.taker.id)
        self.perf.review_status = ReviewStatus.REJECTED
        self.perf.bonus_amount = Decimal("66.00")
        self.perf.save(update_fields=["review_status", "bonus_amount"])

        seen = {}
        with mock.patch(
            "apps.business.api.order_views.record",
            side_effect=self._record_then_boom(seen),
        ):
            with self.assertRaises(RuntimeError):
                self._post("clear-taker", {})

        # 哨兵：桩被调用时，① 状态与 ② 三元组**都已经写进数据库**
        self.assertEqual(seen["order_status"], OrderStatus.UNASSIGNED)
        self.assertIsNone(seen["perf_user_id"])

        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)      # ① 回滚
        self.assertEqual(self.perf.user_id, self.taker.id)                # ② 回滚
        self.assertEqual(self.perf.review_status, ReviewStatus.REJECTED)  # ② 回滚
        self.assertEqual(self.perf.bonus_amount, Decimal("66.00"))        # ② 回滚
        self.assertFalse(
            OperationLog.objects.filter(
                order_no=self.order.order_no, action="order.clear_taker"
            ).exists()
        )   # 清除接单人那笔留痕不留行
        self.assertTrue(
            OperationLog.objects.filter(
                order_no=self.order.order_no, action="performance.assign"
            ).exists()
        )   # 指派那笔留痕仍在 ⇒ 上面的否定断言不是「看着一张空表」


class PoolEntrySettledGuardTests(_LoginMixin, TenantTestCase):
    """**R-10a**（D-0147 / spec §4.5）：已结账的 MAIN 行 ⇒ 「回池」这一族入口一律拒绝。

    R8 把 `COMPLETED → UNASSIGNED/WAITING_GRAB` 变成**一步可达**，而回池的钩子
    （`performance._on_pool_entry` → `reset_taker_performance`）会把 `bonus_amount` /
    `taker_price` 清掉 ⇒「已结账不许动钱」这道闸必须在**唯一咽喉**
    （`order_status.apply_transition` 跑目标钩子之前）生效，而不是只在 `/assign/`、`/delete/`
    两处生效。本类把四条回池入口逐个打一遍（`/clear-taker/` 缺省 `isDelEmp=N`、
    `/throw-to-hall/`、通用 `/transition/` 到两个池内态），判据一律打在**数据库行**上。
    """

    #: 三条人造结账态各只落**一个**字段（与 `test_performance_delete_api` 同形）：
    #: 只看 `settle_status` 一列的实现会被后两条抓住。
    SETTLED_SHAPES = (
        ("settle_status", {"settle_status": SettleStatus.SETTLED}),
        ("settled_at", {"settled_at": timezone.now()}),
        ("settled_amount", {"settled_amount": Decimal("123.00")}),
    )

    def setUp(self):
        self.game = Game.objects.create(name="池内闸测试游戏")

    def _make_order(self, no):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=OrderStatus.COMPLETED,
            total_price=Decimal("200.00"),
            hall_price=Decimal("100.00"),
        )
        perf = Performance.objects.create(
            order=order,
            user=User.objects.create_user(username=f"池内闸打手{no[-3:]}"),
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("25.00"),
            bonus_amount=Decimal("7.77"),
            taker_price=Decimal("55.00"),
            hours=12,
            project="三角洲",
        )
        return order, perf

    def _paths(self, order_no):
        base = f"/api/business/orders/{order_no}"
        return (
            ("clear-taker", f"{base}/clear-taker/", {}),
            ("throw-to-hall", f"{base}/throw-to-hall/", {}),
            (
                "transition-unassigned",
                f"{base}/transition/",
                {"target": OrderStatus.UNASSIGNED},
            ),
            (
                "transition-waiting-grab",
                f"{base}/transition/",
                {"target": OrderStatus.WAITING_GRAB},
            ),
        )

    def _rows(self, order, perf):
        """该单与那条绩效的**九列快照**（值全取库里的，不是内存里的）。"""
        order.refresh_from_db()
        perf.refresh_from_db()
        return (
            order.status,
            order.hall_price,
            perf.user_id,
            perf.temp_amount,
            perf.bonus_amount,
            perf.taker_price,
            perf.settle_status,
            perf.settled_at,
            perf.settled_amount,
        )

    def _post(self, url, payload):
        return self.client.post(
            url, payload, content_type="application/json"
        ).json()

    #: ⭐ **D4b**（R4 逐字，spec §4.4.3）：⛔ 不许改写、⛔ 不许补
    #: `非法状态迁移：<当前> → <目标>` 前缀。前端 `msg(errText(e))` 原样显示这一整句。
    D4B_MSG = "该订单的绩效已结账，不能改状态；请先撤销结账后再操作"

    def test_every_pool_entry_path_is_refused_on_a_settled_row(self):
        """三条结账形态 × 四条回池入口 ⇒ 业务码 + 九列一行不写 + 无留痕。"""
        self._login("客服")
        for index, (field_name, shape) in enumerate(self.SETTLED_SHAPES):
            order, perf = self._make_order(f"2026092400000000095{index}")
            for key, value in shape.items():
                setattr(perf, key, value)
            perf.save(update_fields=[*shape, "updated_at"])
            with self.subTest(settled_field=field_name):
                before = self._rows(order, perf)
                for path_name, url, payload in self._paths(order.order_no):
                    with self.subTest(path=path_name):
                        body = self._post(url, payload)
                        self.assertEqual(body["code"], ErrorCode.BUSINESS, body)
                        self.assertIn("已结账", body["msg"], body)
                        self.assertEqual(self._rows(order, perf), before)
                        self.assertFalse(
                            OperationLog.objects.filter(
                                order_no=order.order_no
                            ).exists()
                        )

    def test_an_unsettled_row_still_returns_to_the_pool(self):
        """反向对照：**未结账**的行上 `/clear-taker/` 仍照旧成功（⛔ 不是一刀切拒绝）。"""
        self._login("客服")
        order, perf = self._make_order("20260924000000000960")
        body = self._post(
            f"/api/business/orders/{order.order_no}/clear-taker/", {}
        )
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(perf.user_id)
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))
        self.assertIsNone(perf.taker_price)

    def test_a_settled_row_cannot_reach_any_target_and_can_after_the_revoke(self):
        """⭐ D4b 把闸门扩张到**一切状态变更**（不再只覆盖池内两态），两条都要钉：

        ① 已结账 ⇒ `/transition/` → `PAUSED` **被拒**：1002 + `msg` **逐字** == R4 的 D4b 那句
           + 九列一行不写 + 无留痕；
        ② ⭐ **反向对照（必须有）**：撤销结账之后同一次迁移 ⇒ `code == 0`
           —— 没有它，「有结账就全拦」那种恒真实现抓不出来。

        **杀法**：把新判据只加在池内两态上（= 现状）⇒ ① 红；
        把判据写成「有 `settled_*` 痕迹就全拦」/ 恒真 ⇒ ② 红。
        """
        self._login("客服")
        order, perf = self._make_order("20260924000000000961")
        perf.settle_status = SettleStatus.SETTLED
        perf.save(update_fields=["settle_status", "updated_at"])
        before = self._rows(order, perf)

        body = self._post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.PAUSED},
        )
        self.assertEqual(body["code"], ErrorCode.BUSINESS, body)
        self.assertEqual(body["msg"], self.D4B_MSG, body)
        self.assertEqual(self._rows(order, perf), before)
        self.assertFalse(
            OperationLog.objects.filter(order_no=order.order_no).exists()
        )

        # ② 反向对照：撤销结账（三字段一起复位；生产入口是
        #    `POST /api/business/settlements/revoke`，本文件另有一条钉那个端点本身）
        perf.settle_status = SettleStatus.UNSETTLED
        perf.settled_at = None
        perf.settled_amount = None
        perf.save(
            update_fields=["settle_status", "settled_at", "settled_amount", "updated_at"]
        )
        body = self._post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.PAUSED},
        )
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAUSED)

    def test_the_two_guards_report_the_settled_one_first(self):
        """⭐ R4 的顺序：一笔单**同时**「已结账 + 有接手人」+ `/transition/` → `UNASSIGNED`
        ⇒ 报的是**已结账族**那句（既有 R-10a 的池内闸），**不是** D2 那句「请先清除接手人」。

        ⚠️ spec **§4.1.3** 与 **§4.4.1** 都定死「既有 `settled_pool_entry_denial` 在前、
        D4b 排在它之后」，而**池内两态恰好是 D2 唯一的覆盖域** ⇒ 目标态落池内时，
        池内那道既有闸**必然先命中**、回的是它那句（含「已结账」）——本条钉**可观测的那一半**：
        「**已结账族先于 D2**」（把 D2 那段挪到两道结账闸**之前** ⇒ 本用例红）。
        ⛔ 单把 D4b 段与 D2 段**对调**在本设计下**不可观测**（池内闸已把答案定死），
        已登记为残差，⛔ 不写成「已覆盖」。
        """
        self._login("客服")
        order, perf = self._make_order("20260924000000000962")   # 该形状本身有接手人
        perf.settle_status = SettleStatus.SETTLED
        perf.save(update_fields=["settle_status", "updated_at"])
        body = self._post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.UNASSIGNED},
        )
        self.assertEqual(body["code"], ErrorCode.BUSINESS, body)
        # 字面值（E-0024：不从被测模块 import 常量）：既有 R-10a 那句，
        # 它是 `settled_pool_entry_denial` 的措辞，spec §4.4.1 明写「保留不动」。
        self.assertEqual(
            body["msg"],
            "非法状态迁移：COMPLETED → UNASSIGNED（该单绩效已结账，不能改回池内）",
            body,
        )
        self.assertNotIn("请先清除接手人", body["msg"], body)

    def test_the_batch_dry_run_reports_the_settled_denial_too(self):
        """⭐ `dry_run` 与真写**同答案**（漏了 `order_writes.py` 的预检 ⇒ 本用例红）。

        目标态取 `PAUSED`（**非池内**）⇒ 池内那道既有闸不参与，回的就是 D4b 那句。
        ⚠️ 先断「不是合格」再取 `ineligible[0]`（与 T3 那条同形）：反过来写会在预检说合格时
        先露 `IndexError`（读起来像崩溃），而不是一条能指认「两段式契约破了」的 FAIL。
        """
        self._login("客服")
        order, perf = self._make_order("20260924000000000963")
        perf.settle_status = SettleStatus.SETTLED
        perf.save(update_fields=["settle_status", "updated_at"])
        body = self._post(
            "/api/business/orders/batch/transition/",
            {
                "order_nos": [order.order_no],
                "target": OrderStatus.PAUSED,
                "dry_run": True,
            },
        )
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            body["data"]["eligible"], [], "预检说合格，而真写会被 D4b 拒 ⇒ 两段式契约破了"
        )
        self.assertEqual(
            [row["order_no"] for row in body["data"]["ineligible"]],
            [order.order_no],
        )
        self.assertEqual(body["data"]["ineligible"][0]["msg"], self.D4B_MSG)

    def test_the_revoke_endpoint_is_not_blocked(self):
        """⭐ D4b **不拦撤销结账**（spec §4.4.2 #1）：它只改绩效行的结账字段、不改订单状态、
        不走状态机（`grep -n "apply_transition" src/apps/business/performance_writes.py`
        零命中）⇒ 本闸**管不到它**，⛔ 不需要为它开例外。

        **杀法**：把闸塞进 `performance_writes.revoke_settlements` ⇒ 本用例红。
        """
        self._login(
            "客服", nodes=DISPATCH_NODES + ("finance.change_settlement_status",)
        )
        order, perf = self._make_order("20260924000000000964")
        perf.settle_status = SettleStatus.SETTLED
        perf.save(update_fields=["settle_status", "updated_at"])
        body = self._post(
            "/api/business/settlements/revoke",
            {"performance_ids": [perf.id], "reason": "对账有误，撤销"},
        )
        self.assertEqual(body["code"], 0, body)
        perf.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(perf.settle_status, SettleStatus.UNSETTLED)
        self.assertEqual(order.status, OrderStatus.COMPLETED)   # 订单状态一个字没动


class TakerPresentPoolEntryGuardTests(_LoginMixin, TenantTestCase):
    """**D2**（spec §3.2 / §4.1 / §7.2 #7–#10）：订单**当前有接手人** ⇒ 不许改成
    `WAITING_GRAB` / `UNASSIGNED`。

    今天改成池内两态时，回池钩子（`performance._on_pool_entry` → `reset_taker_performance`）
    会**静默把接手人清掉**；业务方要的是「拒绝 + 提示『请先清除接手人』」。判据打在
    **数据库行**上（九列快照 + 留痕行），⛔ 不打响应文案单侧——只看文案会漏掉
    「拒了、但人还是被清了」这种半新半旧。

    例外 = **「显式清除当前接单人」这个动作**，**恰好两处**（R1，spec §4.1.2 枚举表）：
    `/clear-taker/` 与绩效垃圾桶的清人支——各一条正面对照；再加一条**反向钉子**：
    `/transition/` 直接改成 `UNASSIGNED`（**不传**开关）⇒ 仍被拒
    （杀「按 `target == UNASSIGNED` 放行」那种模糊实现）。
    """

    #: R4 逐字（spec §4.1.2）：前端 `msg(errText(e))` 原样显示这一整句 ⇒
    #: ⛔ 不许改写、⛔ 不许补 `非法状态迁移：<当前> → <目标>` 前缀。
    D2_MSG = "该订单已有接手人，请先清除接手人，再改为「待抢单」或「未分配」"

    def setUp(self):
        # 游戏名租户内唯一（模型约束）；合成值，⛔ 不抄真实游戏名（E-0077）。
        self.game = Game.objects.create(name="D2 闸测试游戏")

    def _make_order(self, no):
        """一张 `IN_PROGRESS` + **有接手人**的单。

        `claimed_at` 也写上：这样「例外真的清了人」那条判据不是对着一个**本来就空**的列断言
        （E-0024 同族——期望值与初始值相同的断言没有判别力）。
        """
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("200.00"),
            hall_price=Decimal("100.00"),
        )
        perf = Performance.objects.create(
            order=order,
            user=User.objects.create_user(username=f"测试打手{no[-3:]}"),
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("25.00"),
            bonus_amount=Decimal("0.00"),
            taker_price=Decimal("55.00"),
            hours=12,
            project="合成项目甲",
            claimed_at=timezone.now(),
        )
        return order, perf

    def _rows(self, order, perf):
        """该单与那条绩效的**九列快照**（值全取库里的，不是内存里的）。"""
        order.refresh_from_db()
        perf.refresh_from_db()
        return (
            order.status,
            order.hall_price,
            perf.user_id,
            perf.temp_amount,
            perf.bonus_amount,
            perf.taker_price,
            perf.settle_status,
            perf.settled_at,
            perf.settled_amount,
        )

    def _refused_paths(self, order_no):
        """**不进例外**的三条回池入口（⛔ `/clear-taker/` 刻意不在里面——它是例外①）。"""
        base = f"/api/business/orders/{order_no}"
        return (
            (
                "transition-unassigned",
                f"{base}/transition/",
                {"target": OrderStatus.UNASSIGNED},
            ),
            (
                "transition-waiting-grab",
                f"{base}/transition/",
                {"target": OrderStatus.WAITING_GRAB},
            ),
            ("throw-to-hall", f"{base}/throw-to-hall/", {}),
        )

    def _post(self, url, payload):
        return self.client.post(url, payload, content_type="application/json").json()

    def test_the_three_non_exception_paths_are_refused_verbatim(self):
        """有接手人 × {`/transition/`→`UNASSIGNED`, `/transition/`→`WAITING_GRAB`,
        `/throw-to-hall/`} ⇒ 1002 + `msg` **逐字** + 九列一行不写 + 无留痕。"""
        self._login("客服")
        order, perf = self._make_order("20260928000000000101")
        before = self._rows(order, perf)
        for name, url, payload in self._refused_paths(order.order_no):
            with self.subTest(path=name):
                body = self._post(url, payload)
                self.assertEqual(body["code"], ErrorCode.BUSINESS, body)   # 1002
                self.assertEqual(body["msg"], self.D2_MSG, body)           # ⛔ 逐字，不用 assertIn
                self.assertEqual(self._rows(order, perf), before)
                self.assertFalse(
                    OperationLog.objects.filter(order_no=order.order_no).exists()
                )

    def test_the_clear_taker_endpoint_is_the_exception(self):
        """例外① `/clear-taker/`（未结账、有接手人）⇒ `code == 0` + 状态落 `UNASSIGNED`
        + 七列回退。

        **杀法**：去掉 `/clear-taker/` 那处的 `allow_taker_release=True` ⇒ 本用例变 1002。
        """
        self._login("客服")
        order, perf = self._make_order("20260928000000000102")
        body = self._post(f"/api/business/orders/{order.order_no}/clear-taker/", {})
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(perf.user_id)
        self.assertIsNone(perf.taker_price)      # 七列回退里的一个代表
        self.assertIsNone(perf.claimed_at)       # 前置哨兵：它进用例时是**非空**的

    def test_the_trash_bin_path_is_the_exception(self):
        """例外② 垃圾桶删**当前 MAIN 行**：调用那一刻该行的 `user` 仍非空
        ⇒ `code == 0` + 软删 + 钩子把人清空。

        ⭐ **这条就是「不加例外会死循环」的判别用例**（R1 的附带风险）：不加例外时它会拿到
        1002 +「请先清除接手人」，而用户除了先去 `/clear-taker/` 清人之外无路可走
        ——那时这条行已不再是「当前接单人」，删除语义已经不是同一件事。
        **杀法**：例外只接进 `/clear-taker/`、漏 `performance_views.py` 那处 ⇒ 本用例变 1002。
        """
        self._login("客服")
        order, perf = self._make_order("20260928000000000103")
        body = self._post(f"/api/business/performances/{perf.id}/delete/", {})
        self.assertEqual(body["code"], 0, body)
        self.assertTrue(body["data"]["taker_cleared"], body)
        perf.refresh_from_db()
        order.refresh_from_db()
        self.assertIsNotNone(perf.deleted_at)
        self.assertIsNone(perf.user_id)          # 钩子在第 ③ 步清的人
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)

    def test_a_bare_transition_to_unassigned_is_still_refused(self):
        """⭐ **反向钉子**（杀「按 `target == UNASSIGNED` 放行」那种模糊实现）。

        `/transition/` 与 `/clear-taker/` 的**目标态完全相同**（都是 `UNASSIGNED`）
        ⇒ 用目标态当判据的实现会把它一起放行。这里额外钉住「人**没**被清」——
        放行会顺带静默清人，只看业务码抓不到。
        """
        self._login("客服")
        order, perf = self._make_order("20260928000000000104")
        body = self._post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.UNASSIGNED},
        )
        self.assertEqual(body["code"], ErrorCode.BUSINESS, body)
        self.assertEqual(body["msg"], self.D2_MSG, body)
        perf.refresh_from_db()
        self.assertIsNotNone(perf.user_id)       # 静默清人 = 没修

    def test_an_order_without_a_taker_still_returns_to_the_pool(self):
        """反向对照：**没有接手人**的单改成池内两态照旧成功（防「一刀切全拒」恒真）。

        **杀法**：把判据写成「目标 ∈ 池内两态 ⇒ 一律拒」⇒ 本用例红。
        """
        self._login("客服")
        order = Order.objects.create(
            game=self.game,
            order_no="20260928000000000106",
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("200.00"),
            hall_price=Decimal("100.00"),
        )
        Performance.objects.create(
            order=order, user=None, emp_type=EmpType.MAIN,
            temp_amount=Decimal("25.00"),
        )
        body = self._post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.WAITING_GRAB},
        )
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)

    def test_the_batch_dry_run_gives_the_same_verdict(self):
        """⭐ `dry_run` 与真写**同答案**（漏了 `order_writes.py` 的预检 ⇒ 本用例红）。

        「预检说合格、执行被拒」是这条两段式契约最贵的失效形态（用户在清单层点完
        「确定生成」才被拒）。
        """
        self._login("客服")
        order, _perf = self._make_order("20260928000000000105")
        body = self._post(
            "/api/business/orders/batch/transition/",
            {
                "order_nos": [order.order_no],
                "target": OrderStatus.WAITING_GRAB,
                "dry_run": True,
            },
        )
        self.assertEqual(body["code"], 0, body)
        # ⚠️ 先断「不是合格」再取 `ineligible[0]`：反过来写，预检说合格时拿到的是
        #    `IndexError`（读起来像崩溃），而不是一条能指认「两条分支答案不一致」的 FAIL。
        self.assertEqual(
            body["data"]["eligible"], [], "预检说合格，而真写会被 D2 拒 ⇒ 两段式契约破了"
        )
        self.assertEqual(
            [row["order_no"] for row in body["data"]["ineligible"]],
            [order.order_no],
        )
        self.assertEqual(body["data"]["ineligible"][0]["msg"], self.D2_MSG)
