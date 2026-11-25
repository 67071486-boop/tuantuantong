"""「未实名不允许接单」闸（D-0245）的判据网。

spec：`docs/specs/2026-10-16-realname-claim-gate-design.md` **§2**（设计）与 **§3**（判据 1–8，
本文件每条用例的 docstring 都标了对应判据号）。实现：`apps/realname/services.py`
（判定单一出处）+ `apps/business/claim.py::check_grab_limits`（接单钩子）+
`apps/business/order_writes.py::assign_order`（指派钩子）。

## 判据 ↔ 用例对照（spec §3）

| 判据 | 用例 |
|---|---|
| 1 开关关（默认）⇒ 未实名打手照常 | `DefaultOffTests` 的抢单腿 `test_switch_off_uncertified_worker_still_claims` ＋ 指派腿 `test_switch_off_uncertified_taker_can_still_be_assigned` |
| 2a 大厅抢单被拦、文案逐字 | `ClaimHttpTests.test_switch_on_..._hall_claim` |
| 2b 打手开放接口同文案 | `TakerApiClaimGateTests` |
| 2c 单个指派被拦、文案逐字 | `AssignGateHttpTests.test_switch_on_..._single_assign` |
| 2d 批量指派逐单语义 | `BatchAssignGateTests` |
| 2e 录单直指派被拦（1022） | `IntakeAssignGateTests` |
| 3 已实名打手照常 | `ClaimHttpTests` / `AssignGateHttpTests` 的 `test_switch_on_..._certified_...` |
| 4 非打手（客服）不受限 | `ClaimHttpTests` / `AssignGateHttpTests` 的 `test_switch_on_..._non_worker_...` |
| 5 机器自动接手照常（裁定 8） | `MachineClaimBoundaryTests` + `SingleSourceStructureTests` 的机器路径零引用断言 |
| 7 结构网（调用点清单 / 单一出处） | `SingleSourceStructureTests` |
| 6 / 8 | 6 = 既有实名网回归（`test_realname_admin_api` / `test_realname_verify_api`）；8 = 前端页面 spec |

## 约定

- 文案与业务码一律**字面量**（E-0024：从被测模块 import 会把"文案被改坏"藏起来）。
- 判据打**终态**（E-0009）：被拦的断言不只看响应，还断**库里那行没被写**（订单状态 /
  绩效行归属 / 留痕行数）。
- 身份证号是**合成值**（`110101…` 校验位合法、不指向真人，E-0077）。
- 批量的「一单被拦其它照常」在实名闸这一维**结构性不存在**：整批共用同一个 taker，
  判据只看人 ⇒ 同批要么全拦要么全过；本网钉的是**逐单落 `ineligible`**（不是整批
  请求级失败），spec §3-2d 的「或按既有批量语义，如实登记」就是这一条。
"""
from ast import AST, AsyncFunctionDef, Attribute, Call, FunctionDef, Name, parse, walk
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business import jidanpu_member
from apps.business.models import EmpType, Game, JidanpuMemberBinding, Order, OrderStatus, Performance
from apps.business.order_writes import assign_order as _assign_order_symbol
from apps.realname.models import RealNameSetting
from apps.realname.services import get_or_create_setting, is_real_name
from apps.takerapi.models import TakerApp
from tests.p5_auth_helpers import grant_games, grant_nodes, make_logged_in_client

User = get_user_model()

# ── 字面量（E-0024：文案与业务码不 import 被测模块）────────────────────────────
CLAIM_MSG = "请先完成实名认证后再接单"
ASSIGN_MSG = "该打手未完成实名认证，不能指派接单"
OK = 0
BUSINESS = 1002
GRAB_LIMIT = 1013
ASSIGN_FAILED = 1022  # ORDER_CREATED_ASSIGN_FAILED：单已建好、指派没成功

#: 合成身份证号（校验位合法、不指向任何真人；与 test_realname_admin_api 同值来源）。
SYNTHETIC_ID_CARD = "110101199003070011"

TAKE_ORDER_NODE = "grab_hall.take_order"
CLAIM_PATH = "/api/business/hall/{order_no}/claim/"
ASSIGN_PATH = "/api/business/orders/{order_no}/assign/"
CREATE_ORDER_PATH = "/api/business/orders/"
TOKEN_PATH = "/api/taker/oauth/token"
BATCH_ASSIGN_PATH = "/api/business/orders/batch/assign/"

#: 文档保留段（⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.71"
TAKER_APP_KEY = "rnclaimgatekey001"
TAKER_APP_SECRET = "rn-claim-gate-secret-0000001"


def _turn_on_block_claim() -> None:
    """打开「未实名不允许接单」（起点哨兵：打开后**当场回读**，E-0009）。"""
    setting = get_or_create_setting()
    setting.block_claim_without_cert = True
    setting.save(update_fields=["block_claim_without_cert", "updated_at"])
    assert RealNameSetting.objects.get().block_claim_without_cert is True


def _assert_uncertified(user) -> None:
    """起点判据：这个用户确实是「未实名」（拿已实名的人测"被拦"是恒真的）。"""
    profile = StaffProfile.objects.filter(user=user).first()
    assert not is_real_name(profile), "夹具起点不是未实名，本用例不成立"


def _certify(user) -> None:
    """给用户按「已实名」的口径填一个合成号（判据只看档案上的 `id_card_no`）。"""
    profile, _ = StaffProfile.objects.get_or_create(user=user)
    profile.id_card_no = SYNTHETIC_ID_CARD
    profile.save(update_fields=["id_card_no", "updated_at"])


# ══════════════════════════════════════════════════════════════════════════════
# 判据 1：开关关（默认）⇒ 行为一字不变
# ══════════════════════════════════════════════════════════════════════════════
class DefaultOffTests(TenantTestCase):
    """未开启租户（默认）不得有任何可观察变化（spec §3-1）。"""

    def setUp(self):
        self.game = Game.objects.create(name="实名闸游戏甲")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20261016000000000001",
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        self.worker = make_logged_in_client(
            username="gate-off-worker", groups=("代练员",), nodes=(TAKE_ORDER_NODE,)
        )
        grant_games(User.objects.get(username="gate-off-worker"), self.game)
        self.taker = User.objects.get(username="gate-off-worker")
        _assert_uncertified(self.taker)

    def test_switch_off_uncertified_worker_still_claims(self):
        """判据 1·抢单腿：开关**默认关** ⇒ 未实名打手照常抢单成功（对照组）。"""
        # 起点哨兵：确实没开（读路径 `get_or_create_setting` 建出的默认行就是 False）。
        self.assertIs(get_or_create_setting().block_claim_without_cert, False)

        response = self.worker.post(
            CLAIM_PATH.format(order_no=self.order.order_no), {}, content_type="application/json"
        )
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], OK, body)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(order=self.order, emp_type=EmpType.MAIN).user_id,
            self.taker.pk,
        )

    def test_switch_off_uncertified_taker_can_still_be_assigned(self):
        """判据 1·指派腿：开关**默认关** + 打手未实名 ⇒ 照常指派成功到终态（对照组）。"""
        # 起点哨兵：确实没开（与抢单腿同一条判据）。
        self.assertIs(get_or_create_setting().block_claim_without_cert, False)
        # 客户端与单照既有指派用例（AssignGateHttpTests）的形状：客服持指派节点、
        # UNASSIGNED 单 + 一条没人的 MAIN 绩效行。
        cs = make_logged_in_client(
            username="gate-off-cs",
            groups=("客服",),
            nodes=(
                "order.assign_taker",
                "order.change_status",
                "order.list",
                "order.submit_payout_price",
            ),
        )
        grant_games(User.objects.get(username="gate-off-cs"), self.game)
        taker = User.objects.create_user(username="gate-off-taker", password="pw-12345678")
        group, _ = Group.objects.get_or_create(name="代练员")
        taker.groups.add(group)
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 被指派者也要授权（本条考开关默认关）。
        grant_games(taker, self.game)
        _assert_uncertified(taker)
        order = Order.objects.create(
            game=self.game,
            order_no="20261016000000000008",
            status=OrderStatus.UNASSIGNED,
            hall_price=Decimal("100.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("20.00")
        )

        response = cs.post(
            ASSIGN_PATH.format(order_no=order.order_no),
            {"user_id": taker.pk},
            content_type="application/json",
        )
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], OK, body)  # 码不出现：不是 1002 / 1013 / 1022
        self.assertNotIn(ASSIGN_MSG, body["msg"], body)  # 文案不出现
        # 终态（E-0009）：单进「代练中」、MAIN 行归属本次的未实名打手。
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id, taker.pk
        )


# ══════════════════════════════════════════════════════════════════════════════
# 判据 2a / 3 / 4：大厅抢单端点的三档
# ══════════════════════════════════════════════════════════════════════════════
class ClaimHttpTests(TenantTestCase):
    """`POST /api/business/hall/{order_no}/claim/` 在开关开时的三档（spec §3-2a/3/4）。"""

    def setUp(self):
        self.game = Game.objects.create(name="实名闸游戏乙")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20261016000000000002",
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        _turn_on_block_claim()

    def _login_worker(self, username, *, groups=("代练员",)):
        client = make_logged_in_client(username=username, groups=groups, nodes=(TAKE_ORDER_NODE,))
        user = User.objects.get(username=username)
        grant_games(user, self.game)
        _assert_uncertified(user)
        return client, user

    def _claim(self, client):
        return client.post(
            CLAIM_PATH.format(order_no=self.order.order_no), {}, content_type="application/json"
        )

    def _assert_untouched(self):
        """被拦的**终态**：订单还在大厅、MAIN 行没人、零留痕（E-0009）。"""
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(
            Performance.objects.get(order=self.order, emp_type=EmpType.MAIN).user_id
        )
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_switch_on_blocks_uncertified_worker_with_the_exact_message(self):
        """判据 2a：开关开 + 未实名打手 ⇒ 200 信封 code=1013，文案**逐字**。"""
        client, _user = self._login_worker("gate-on-worker")
        response = self._claim(client)
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], GRAB_LIMIT, body)
        self.assertEqual(body["msg"], CLAIM_MSG, body)  # ⛔ 不是 assertIn：逐字
        self._assert_untouched()

    def test_switch_on_certified_worker_can_still_claim(self):
        """判据 3：开关开 + **已实名**打手（`id_card_no` 非空）⇒ 照常抢到。"""
        client, user = self._login_worker("gate-cert-worker")
        _certify(user)
        response = self._claim(client)
        body = response.json()
        self.assertEqual(body["code"], OK, body)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(order=self.order, emp_type=EmpType.MAIN).user_id, user.pk
        )

    def test_switch_on_non_worker_is_not_restricted(self):
        """判据 4：开关开 + **非打手**（客服）未实名 ⇒ 不受限（只限打手）。"""
        client, _user = self._login_worker("gate-kefu-worker", groups=("客服",))
        response = self._claim(client)
        body = response.json()
        self.assertEqual(body["code"], OK, body)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)


# ══════════════════════════════════════════════════════════════════════════════
# 判据 2b：打手侧开放接口同文案
# ══════════════════════════════════════════════════════════════════════════════
class TakerApiClaimGateTests(TenantTestCase):
    """`POST /api/taker/v1/hall/<order_no>/claim/` 的实名闸两档（spec §3-2b/3）。

    夹具照 `tests/test_takerapi_claim_api.py::_ClaimFixture` 的既有形态：
    令牌**从取令牌端点换**（⛔ 不手搓）；幂等键必带。
    """

    def setUp(self):
        self.domain_name = self.domain.domain
        self.anonymous = Client(SERVER_NAME=self.domain_name)
        self.game = Game.objects.create(name="实名闸游戏丙")
        self.order_no = "20261016000000000003"

    def _make_taker(self, username, *, certify):
        user = User.objects.create_user(username=username, password="pw-12345678")
        group, _ = Group.objects.get_or_create(name="代练员")
        user.groups.add(group)
        profile = StaffProfile.objects.create(user=user)
        profile.games.set([self.game])
        if certify:
            _certify(user)
        else:
            _assert_uncertified(user)
        grant_nodes(user, TAKE_ORDER_NODE)
        app = TakerApp.objects.create(
            name=f"{username}-app",
            app_key=TAKER_APP_KEY,
            app_secret=TAKER_APP_SECRET,
            taker=user,
            created_by=user,
        )
        return user, app

    def _issue_token(self, app) -> str:
        response = self.anonymous.post(
            TOKEN_PATH,
            {
                "grant_type": "client_credentials",
                "client_id": app.app_key,
                "client_secret": app.app_secret,
            },
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        return response.json()["access_token"]

    def _order(self):
        order = Order.objects.create(
            game=self.game,
            order_no=self.order_no,
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        return order

    def _claim(self, token):
        return self.anonymous.post(
            f"/api/taker/v1/hall/{self.order_no}/claim/",
            data="",
            content_type="application/json",
            REMOTE_ADDR=SOURCE_IP,
            HTTP_AUTHORIZATION=f"Bearer {token}",
            HTTP_IDEMPOTENCY_KEY="gate-key-0001",
        )

    def test_switch_on_blocks_the_uncertified_taker_with_the_same_message(self):
        """判据 2b：开关开 + 绑定账号未实名 ⇒ 同码 1013、**同一句**文案（原样透传）。"""
        _turn_on_block_claim()
        user, app = self._make_taker("gate-taker-uncert", certify=False)
        order = self._order()
        response = self._claim(self._issue_token(app))
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], GRAB_LIMIT, body)
        self.assertEqual(body["msg"], CLAIM_MSG, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )
        self.assertIsNotNone(user.pk)  # 夹具自证：绑定账号存在（防夹具写坏成恒真）

    def test_switch_on_certified_taker_can_still_claim(self):
        """判据 3（开放接口侧对照）：绑定账号**已实名** ⇒ 照常抢到。"""
        _turn_on_block_claim()
        user, app = self._make_taker("gate-taker-cert", certify=True)
        order = self._order()
        response = self._claim(self._issue_token(app))
        body = response.json()
        self.assertEqual(body["code"], OK, body)
        self.assertEqual(
            set(body["data"]), {"order_no", "status", "status_text"}
        )
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            user.pk,
        )


# ══════════════════════════════════════════════════════════════════════════════
# 判据 2c / 3 / 4：单个指派端点的三档
# ══════════════════════════════════════════════════════════════════════════════
class AssignGateHttpTests(TenantTestCase):
    """`POST /api/business/orders/{order_no}/assign/` 的实名闸三档（spec §3-2c/3/4）。

    错误形状 = `assign_order` 既有业务失败形态：200 信封 + `code=1002`（本仓口径：
    HTTP 状态码只表达传输 / 认证层）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="实名闸游戏丁")
        self.cs = make_logged_in_client(
            username="gate-cs",
            groups=("客服",),
            nodes=(
                "order.assign_taker",
                "order.change_status",
                "order.list",
                "order.submit_payout_price",
            ),
        )
        grant_games(User.objects.get(username="gate-cs"), self.game)
        _turn_on_block_claim()

    def _taker(self, username, *, groups=("代练员",)):
        user = User.objects.create_user(username=username, password="pw-12345678")
        for name in groups:
            group, _ = Group.objects.get_or_create(name=name)
            user.groups.add(group)
        # ⭐ D-0252：指派闸判「目标打手游戏范围」（无档案=无游戏=拒）
        # ⇒ 本类考的是实名闸，给打手授游戏让游戏范围闸放行（专钉：tests/test_order_assign_game_gate.py）。
        grant_games(user, self.game)
        _assert_uncertified(user)
        return user

    def _order(self, order_no):
        order = Order.objects.create(
            game=self.game,
            order_no=order_no,
            status=OrderStatus.UNASSIGNED,
            hall_price=Decimal("100.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("20.00")
        )
        return order

    def _assign(self, order, taker):
        return self.cs.post(
            ASSIGN_PATH.format(order_no=order.order_no),
            {"user_id": taker.pk},
            content_type="application/json",
        ).json()

    def _assert_not_taken(self, order):
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )

    def test_switch_on_blocks_the_uncertified_worker_with_the_exact_message(self):
        """判据 2c：开关开 + 未实名打手 ⇒ 200 信封 code=1002，文案**逐字**，零写入。"""
        taker = self._taker("gate-assign-uncert")
        order = self._order("20261016000000000004")
        body = self._assign(order, taker)
        self.assertEqual(body["code"], BUSINESS, body)
        self.assertEqual(body["msg"], ASSIGN_MSG, body)  # ⛔ 不是 assertIn：逐字
        self._assert_not_taken(order)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_switch_on_certified_worker_can_still_be_assigned(self):
        """判据 3：开关开 + **已实名**打手 ⇒ 照常指派成功。"""
        taker = self._taker("gate-assign-cert")
        _certify(taker)
        order = self._order("20261016000000000005")
        body = self._assign(order, taker)
        self.assertEqual(body["code"], OK, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id, taker.pk
        )

    def test_switch_on_non_worker_is_not_restricted(self):
        """判据 4：开关开 + **非打手**（客服）未实名 ⇒ 不受限（只限打手）。"""
        taker = self._taker("gate-assign-kefu", groups=("客服",))
        order = self._order("20261016000000000006")
        body = self._assign(order, taker)
        self.assertEqual(body["code"], OK, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id, taker.pk
        )


# ══════════════════════════════════════════════════════════════════════════════
# 判据 2d：批量指派（逐单语义，如实登记）
# ══════════════════════════════════════════════════════════════════════════════
class BatchAssignGateTests(TenantTestCase):
    """`POST /api/business/orders/batch/assign/`（spec §3-2d，**按既有批量语义如实登记**）。

    ⚠️ **两条既有批量语义（`run_batch`，本线一字未改）**：
    ① **实名闸的判据只看 taker 这一个人**（与哪张单无关），而批量请求整批共用一个 taker
       ⇒ 「一单被拦、其它单照常」在实名这一维**结构性不存在**——同批要么全拦要么全过；
    ② **apply 是一票否决式**：逐笔重校验、任一不合格整笔回滚（`set_rollback` + 1002 +
       `data.conflict` 点名第一笔 + `applied=[]`），`dry_run` 预检才是逐单 `ineligible`。
    本网把这两条**按实钉住**（改语义必须先改这里），不替既有行为圆场。
    """

    def setUp(self):
        self.game = Game.objects.create(name="实名闸游戏戊")
        self.cs = make_logged_in_client(
            username="gate-batch-cs",
            groups=("客服",),
            nodes=("order.assign_taker", "order.change_status", "order.list"),
        )
        grant_games(User.objects.get(username="gate-batch-cs"), self.game)
        _turn_on_block_claim()
        self.taker = User.objects.create_user(
            username="gate-batch-taker", password="pw-12345678"
        )
        group, _ = Group.objects.get_or_create(name="代练员")
        self.taker.groups.add(group)
        _assert_uncertified(self.taker)

    def _assignable_order(self, seq):
        order = Order.objects.create(
            game=self.game,
            order_no=f"2026101600000000010{seq}",
            status=OrderStatus.UNASSIGNED,
            hall_price=Decimal("100.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("20.00")
        )
        return order

    def _post_batch(self, order_nos, *, dry_run):
        return self.cs.post(
            BATCH_ASSIGN_PATH,
            {
                "order_nos": list(order_nos),
                "dry_run": dry_run,
                "user_id": self.taker.pk,
            },
            content_type="application/json",
        ).json()

    def _assert_nothing_written(self, orders):
        """终态：单没动、MAIN 行没人、零留痕（E-0009）。"""
        for order in orders:
            order.refresh_from_db()
            self.assertEqual(order.status, OrderStatus.UNASSIGNED)
            self.assertIsNone(
                Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
            )
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_dry_run_precheck_reports_the_denial_per_order(self):
        """`dry_run=True`：逐单 `ineligible`(1002) + 文案逐字（两段式的预检腿）。"""
        a = self._assignable_order(3)
        b = self._assignable_order(4)
        body = self._post_batch([a.order_no, b.order_no], dry_run=True)
        self.assertEqual(body["code"], OK, body)
        self.assertEqual(body["data"]["applied"], [])
        self.assertEqual(
            [row["order_no"] for row in body["data"]["ineligible"]],
            [a.order_no, b.order_no],
        )
        for row in body["data"]["ineligible"]:
            self.assertEqual(row["code"], BUSINESS, row)
            self.assertEqual(row["msg"], ASSIGN_MSG, row)  # ⛔ 逐字
        self._assert_nothing_written([a, b])

    def test_apply_is_all_or_nothing_and_the_conflict_names_the_message(self):
        """`dry_run=False`（既有语义 = 一票否决）：整笔回滚，conflict 点名 + 文案逐字。"""
        a = self._assignable_order(1)
        b = self._assignable_order(2)
        body = self._post_batch([a.order_no, b.order_no], dry_run=False)
        self.assertEqual(body["code"], BUSINESS, body)
        # 冲突信封：点名第一笔 + 逐单文案原文（⛔ 不是整批笼统一句话就完）。
        self.assertEqual(body["data"]["conflict"]["order_no"], a.order_no)
        self.assertEqual(body["data"]["conflict"]["code"], BUSINESS)
        self.assertEqual(body["data"]["conflict"]["msg"], ASSIGN_MSG)  # ⛔ 逐字
        self.assertIn("本批未做任何修改", body["msg"])
        self.assertEqual(body["data"]["applied"], [])
        self._assert_nothing_written([a, b])


# ══════════════════════════════════════════════════════════════════════════════
# 判据 2e：录单直接指派（1022）
# ══════════════════════════════════════════════════════════════════════════════
class IntakeAssignGateTests(TenantTestCase):
    """录单「建单 + 指派」一次请求（spec §3-2e）。

    既有语义（D-0179）：指派失败**带不走订单** ⇒ 单留下（UNASSIGNED）+
    `code=1022` + `data.assign_code=1002` + 原因。实名拒沿用同一条形状。
    """

    def setUp(self):
        self.game = Game.objects.create(name="实名闸游戏己")
        _turn_on_block_claim()
        # 打手：真实登录一次（建档案 + 职位），未实名。
        make_logged_in_client(username="gate-intake-taker", groups=("代练员",), nodes=("order.list",))
        self.taker = User.objects.get(username="gate-intake-taker")
        _assert_uncertified(self.taker)
        self.cs = make_logged_in_client(
            username="gate-intake-cs", groups=("客服",), nodes=("order.add_edit",)
        )

    def _post(self):
        return self.cs.post(
            CREATE_ORDER_PATH,
            {
                "game": self.game.id,
                "project_text": "实名闸合成项目",
                "hall_price": "66.00",
                "assignee": self.taker.pk,
            },
            content_type="application/json",
        ).json()

    def test_switch_on_the_order_survives_and_the_reason_is_the_exact_message(self):
        body = self._post()
        self.assertEqual(body["code"], ASSIGN_FAILED, body)
        self.assertEqual(set(body["data"]), {"order_no", "reason", "assign_code"})
        self.assertEqual(body["data"]["assign_code"], BUSINESS)
        self.assertEqual(body["data"]["reason"], ASSIGN_MSG)  # ⛔ 逐字
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )


# ══════════════════════════════════════════════════════════════════════════════
# 判据 5：机器自动接手不受闸影响（裁定 8 边界被钉住）
# ══════════════════════════════════════════════════════════════════════════════
class MachineClaimBoundaryTests(TenantTestCase):
    """`machine_claim_order`（`enforce_limits=False`）对未实名打手**照常成功**。

    判别力：谁把闸塞进机器路径（或把 `check_grab_limits` 的跳过条件改坏）⇒ 本用例红。
    """

    def setUp(self):
        self.game = Game.objects.create(name="实名闸游戏庚")
        _turn_on_block_claim()
        # 绑定账号：代练员 + 状态机那条边要的节点（机器路径**保留**节点门，裁定 8）。
        make_logged_in_client(
            username="gate-machine-worker", groups=("代练员",), nodes=(TAKE_ORDER_NODE,)
        )
        self.user = User.objects.get(username="gate-machine-worker")
        _assert_uncertified(self.user)
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url="https://jidanpu.example.invalid",
            account="gate-acc-a",
            account_name="实名闸合成绑定甲",
            password="pw-gate-a",
            source_name="团团通系统",
            enabled=True,
        )
        self.order = Order.objects.create(
            game=self.game,
            order_no="20261016000000000007",
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("12.34"),
            total_price="50.00",
            project_text="实名闸合成项目庚",
        )
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("12.34")
        )

    def test_machine_claim_succeeds_for_an_uncertified_worker_with_the_switch_on(self):
        outcome = jidanpu_member.machine_claim_order(
            binding=self.binding,
            order=self.order,
            remote_uid="203.0.113.9:uid-1",
            remote_name="实名闸合成远端打手",
            remote_order_id="GATE-JDP-1",
            source_note="合成测试：远端已接手",
        )
        self.assertTrue(outcome.claimed, outcome)
        self.assertIsNone(outcome.claim_error, outcome)
        self.assertIsNone(outcome.denial, outcome)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(order=self.order, emp_type=EmpType.MAIN).user_id,
            self.user.pk,
        )


# ══════════════════════════════════════════════════════════════════════════════
# 判据 7：结构网（AST）
# ══════════════════════════════════════════════════════════════════════════════
def _app_sources():
    """`src/apps` 下全部业务源码（跳过 `__init__.py` 与 migrations）。"""
    root = Path(__file__).resolve().parents[1] / "apps"
    for path in sorted(root.rglob("*.py")):
        if path.name == "__init__.py" or "migrations" in path.parts:
            continue
        yield path


def _rel(path: Path) -> str:
    return path.relative_to(Path(__file__).resolve().parents[1]).as_posix()


def _calls_of(tree: AST, names: set[str]) -> list[str]:
    """AST 里所有「函数名为 `names` 之一」的调用点，按出现顺序返回函数名
    （`Name` 直调或 `Attribute` 形式都算——`services.is_real_name(…)` 是后者）。"""
    found = []
    for node in walk(tree):
        if not isinstance(node, Call):
            continue
        if isinstance(node.func, Name) and node.func.id in names:
            found.append(node.func.id)
        elif isinstance(node.func, Attribute) and node.func.attr in names:
            found.append(node.func.attr)
    return found


class SingleSourceStructureTests(TenantTestCase):
    """结构网（spec §3-7）：判定唯一出处 + 指派咽喉的调用点清单 + 机器路径零引用。

    ⚠️ 结构网的断言本身必须能失败（AGENTS.md §5-G）：每条都配了「把它弄红」的变异
    （多抄一份内联 / 新增第四个指派调用点 / 把闸塞进机器路径），见本线弄红记录。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.trees = {_rel(path): parse(path.read_text()) for path in _app_sources()}

    def test_assign_order_is_called_only_from_registered_sites(self):
        """指派入口、修复命令和外部占位接单的 assign_order 调用点逐项登记。"""
        sites = {}
        for rel, tree in self.trees.items():
            calls = [node for node in _calls_of(tree, {"assign_order"})]
            if calls:
                sites[rel] = calls
        self.assertEqual(
            sites,
            {
                "apps/business/order_intake.py": ["assign_order"],
                "apps/business/api/order_views.py": ["assign_order", "assign_order"],
                # 运维命令 `fix_missing_taker_performance`
                #    里有两个 `assign_order` 调用（单笔 + 批量修复路径）。该调用点**不是**
                #    本网新增的指派入口，而是历史数据修复；本行只为让「调用点清单」如实。
                #    ⚠️ 新增**真正的**指派入口仍必须先过实名闸再登记本网。
                "apps/business/management/commands/fix_missing_taker_performance.py": [
                    "assign_order",
                    "assign_order",
                ],
                # ⭐ A2：代练通接单后把「接手人」指向占位账号
                #    （`代练通打手`）—— 走**同一条指派咽喉** `assign_order`（实名闸照旧生效）。
                "apps/business/dailiantong_push.py": ["assign_order"],
                # 直派接收、上家资格预检与回执落单都复用指派的实名判定。
                "apps/business/peer_direct_taker.py": ["assign_order"] * 3,
                "apps/business/jidanpu_first_assignment.py": ["assign_order"] * 2,
                # SDK 指派和改派共用一个视图调用点，沿用 assign_order 的实名与游戏判据。
                "apps/openapi/api/integration_orders.py": ["assign_order"],
            },
            "指派咽喉的调用点清单变了：新增 / 迁移调用点必须先过实名闸的判定再登记本网",
        )
        # 定义模块自己不调它（只有 def）。
        self.assertNotIn("apps/business/order_writes.py", sites)

    def test_is_real_name_is_implemented_exactly_once(self):
        """`bool(… .id_card_no …)` 的内联判真值在 apps 业务源码内**恰好一处**（扫描面 =
        `_app_sources()`，跳过 `__init__` / migrations；services.is_real_name）。"""
        inline = []
        for rel, tree in self.trees.items():
            for node in walk(tree):
                if not (isinstance(node, Call) and isinstance(node.func, Name)
                        and node.func.id == "bool"):
                    continue
                for sub in walk(node):
                    if isinstance(sub, Attribute) and sub.attr == "id_card_no":
                        inline.append(rel)
                        break
        self.assertEqual(
            inline,
            ["apps/realname/services.py"],
            "「已实名」判据出现了第二份内联：改调 realname.services.is_real_name（E-0021）",
        )

    def test_the_two_former_inline_sites_now_call_is_real_name(self):
        """两处原内联点（realname 状态端点 / 资料 payload）真的改调了单一出处。"""
        for rel in ("apps/realname/views.py", "apps/core/profile_views.py"):
            calls = _calls_of(self.trees[rel], {"is_real_name"})
            self.assertTrue(
                calls,
                f"{rel} 没有调 is_real_name（单一出处被绕开了）",
            )

    def test_both_hooks_sit_in_the_two_throats(self):
        """接单钩子在 `check_grab_limits` 里、指派钩子在 `assign_order` 里（防钩子被挪走）。"""
        claim_tree = self.trees["apps/business/claim.py"]
        checks = [
            node
            for node in walk(claim_tree)
            if isinstance(node, (FunctionDef, AsyncFunctionDef))
            and getattr(node, "name", "") == "check_grab_limits"
        ]
        self.assertEqual(len(checks), 1)
        self.assertTrue(
            _calls_of(checks[0], {"ensure_can_claim"}),
            "check_grab_limits 里没有 ensure_can_claim 钩子",
        )

        writes_tree = self.trees["apps/business/order_writes.py"]
        assigns = [
            node
            for node in walk(writes_tree)
            if isinstance(node, (FunctionDef, AsyncFunctionDef))
            and getattr(node, "name", "") == "assign_order"
        ]
        self.assertEqual(len(assigns), 1)
        self.assertTrue(
            _calls_of(assigns[0], {"claim_gate_blocks"}),
            "assign_order 里没有 claim_gate_blocks 钩子",
        )

    def test_the_machine_path_references_none_of_the_gate_functions(self):
        """机器路径（`jidanpu_member.py`）零引用闸函数——裁定 8 的结构面（防「顺手塞闸」）。"""
        tree = self.trees["apps/business/jidanpu_member.py"]
        self.assertEqual(
            _calls_of(tree, {"ensure_can_claim", "claim_gate_blocks", "is_real_name"}),
            [],
            "机器自动接手路径里出现了实名闸引用（裁定 8：上游同步不能卡死，spec §5）",
        )

    def test_the_gate_symbol_import_is_the_single_definition(self):
        """哨兵：`is_real_name` 的定义确实在 services（防止有人把实现搬走却留下网）。"""
        tree = self.trees["apps/realname/services.py"]
        defs = [
            node.name
            for node in walk(tree)
            if isinstance(node, FunctionDef) and node.name == "is_real_name"
        ]
        self.assertEqual(defs, ["is_real_name"])
        # `assign_order` 的定义也确实在 order_writes（import 指向它，别让咽喉搬家）。
        from apps.business import order_writes

        self.assertIs(order_writes.assign_order, _assign_order_symbol)


# ══════════════════════════════════════════════════════════════════════════════
# E-0252 同族边角：纯空格的档案号码**不算**「已实名」
# ══════════════════════════════════════════════════════════════════════════════
class WhitespaceProfileJudgmentTests(TenantTestCase):
    """档案 `id_card_no=" "` 必须判**未实名**（E-0252 边界判据；E-0021 同族）。

    现场：闸判据 `bool(profile.id_card_no)` 对 `" "` 为真 ⇒ 档案上躺着一个纯空格
    （历史脏数据 / 任何旁路写入）就会被判「已实名」，闸放行一个其实没实名的人。
    修后判据 = `(profile.id_card_no or "").strip()` 非空——只在**单一出处**
    `services.is_real_name` 改，两个消费钩子（抢单 / 指派）自动同变。
    ⚠️ 本类断言打在单一出处与 `claim_gate_blocks` / `ensure_can_claim` 上，
    ⛔ 不在测试里另写第二份内联判据。
    """

    def test_whitespace_only_id_card_is_uncertified_and_the_gate_blocks(self):
        from apps.realname.services import (
            RealNameClaimBlocked,
            claim_gate_blocks,
            ensure_can_claim,
        )

        user = User.objects.create_user(username="gate-ws-worker")
        # 与本文件其余夹具同口径：职位字面量（`apps/business/roles.py::WORKER` 的值）。
        group, _ = Group.objects.get_or_create(name="代练员")
        user.groups.add(group)
        profile = StaffProfile.objects.create(user=user, id_card_no="   ")
        _turn_on_block_claim()

        # 起点自证：档案确实「非空但全是空白」（E-0009：起点失真判据就失真）。
        self.assertEqual(StaffProfile.objects.get(pk=profile.pk).id_card_no, "   ")
        self.assertFalse(is_real_name(profile), '" " 被判已实名 ⇒ E-0252 的洞还在')
        self.assertTrue(claim_gate_blocks(user=user), '" " 档案没被闸拦 ⇒ 洞还在')
        with self.assertRaises(RealNameClaimBlocked):
            ensure_can_claim(user=user)
