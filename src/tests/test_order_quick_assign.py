"""录单页「快速指派」三件套的后端判据（业务方 2026-10-03 裁定；决策见 `docs/DECISIONS.md`）。

三件套 = **选人**（候选名单）+ **给打手发单价**（业务方裁定 = `Order.hall_price`）+
**默认指派此人**（`StaffProfile.default_assignee`，跟录单的客服走）。

本文件钉的四件事（缺一条都不算落地）：
1. 建单 + 指派**串成一次**（`POST /orders/` 带 `assignee`），页面只提交一次；
2. **指派失败不回滚订单**，且回一个**可分辨**的业务码 + 已建单号 + 原因（⛔ 不伪装成功）；
3. `remember_assignee` 落 `StaffProfile.default_assignee` / 清除，并在 `assignee-options` 回传；
4. 目标不可用（不存在 / 停用）在**建单之前**拦下（业务码 1000，一行不写）。

⭐ **本线追加（业务方裁定 R1/R2，`feat/default-assign-hall`）**：那一格由**三态**变
**四态**（多一档 `"hall"` = 记住「**默认抛内部抢单**」），落点是 `StaffProfile.default_throw_to_hall`
一列，读面每多一个 `defAssIsHall` 键 ⇒ 顶层键**五 → 六**；`"N"` 的「无条件清除」还补齐了
抛单失败（`1023`）那条路径。判据见 `DefaultThrowToHallPreferenceTests` 与 spec
`docs/specs/2026-10-08-default-assign-hall-design.md`。

⚠️ 夹具一律合成值（E-0077）；判据用**单号 / 用户名**这类可指认的字段，不用主键指认跨租户的行。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.business.order_status import IllegalTransition
from apps.business.order_writes import set_default_assign_preference
from apps.platform.provisioning import provision_tenant, seed_tenant
from apps.rbac.models import Role
from tests.p5_auth_helpers import grant_nodes, make_logged_in_client

User = get_user_model()

CREATE_URL = "/api/business/orders/"
OPTIONS_URL = "/api/business/orders/assignee-options/"

#: 建一个「能录单 + 能指派」的客服（业务方口径里快速指派就是客服的动作）。
#: `order.add_edit` = 建单的类级门；`order.assign_taker` = `UNASSIGNED → IN_PROGRESS`
#: 那条边的节点集里的一个（`apps/business/order_status.py` 的 `_ASSIGN`）。
CS_NODES = ("order.add_edit", "order.assign_taker")


def _make_game(name="原神"):
    return Game.objects.create(name=name)


def _make_taker(username, *, first_name="", active=True, order_by=0):
    """候选人：真实登录一次（建档案 + 职位），再回填昵称 / 排序值 / 启停。"""
    make_logged_in_client(username=username, groups=("代练员",), nodes=("order.list",))
    user = User.objects.get(username=username)
    if first_name:
        user.first_name = first_name
        user.save(update_fields=["first_name"])
    if not active:
        user.is_active = False
        user.save(update_fields=["is_active"])
    if order_by:
        profile, _ = StaffProfile.objects.get_or_create(user=user)
        profile.order_by = order_by
        profile.save(update_fields=["order_by", "updated_at"])
    return user


class CreateAndAssignTests(TenantTestCase):
    """① 建单 + 指派一次请求完成；`hall_price` 就是指派价（业务方裁定）。"""

    def setUp(self):
        self.game = _make_game()
        self.taker = _make_taker("测试打手甲", first_name="测试昵称甲")
        self.cs = make_logged_in_client(username="测试客服甲", groups=("客服",), nodes=CS_NODES)
        self.cs_user = User.objects.get(username="测试客服甲")

    def _post(self, **overrides):
        payload = {
            "game": self.game.id,
            "project_text": "满星",
            "hall_price": "88.50",
            "assignee": self.taker.pk,
        }
        payload.update(overrides)
        return self.cs.post(CREATE_URL, payload, content_type="application/json")

    def test_create_with_an_assignee_creates_and_assigns_in_one_request(self):
        response = self._post()
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], 0, body)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        # 单建出来了
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(order.project_text, "满星")
        # 「给打手发单价」= `Order.hall_price`（业务方裁定：发单价 = 大厅价 = 指派价）
        self.assertEqual(order.hall_price, Decimal("88.50"))
        # 指派真的发生了：MAIN 绩效行挂在这个人身上
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, self.taker.pk)
        # 响应回的是**指派之后**的单（不是建单那一刻的快照）
        self.assertEqual(body["data"]["status"], OrderStatus.IN_PROGRESS)

    def test_without_an_assignee_the_order_is_just_created(self):
        """不传 `assignee` ⇒ 与既有录单逐字同形（UNASSIGNED、没有接单人）。"""
        body = self.cs.post(
            CREATE_URL,
            {"game": self.game.id, "project_text": "满星"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertIsNone(perf.user_id)


class AssignmentFailureTests(TenantTestCase):
    """② 指派失败 ⇒ **订单留下** + 可分辨的业务码 + 已建单号 + 原因（⛔ 不回滚、⛔ 不伪装成功）。"""

    def setUp(self):
        self.game = _make_game()
        self.taker = _make_taker("测试打手甲", first_name="测试昵称甲")
        # ⚠️ **只有 `order.add_edit`**：够建单，**不够**走 `UNASSIGNED → IN_PROGRESS`
        #    （那条边的节点集是 `order.change_status` / `order.assign_taker` / … 的并集）
        #    ⇒ 指派必然失败，而失败必须发生在**订单已经建好之后**。
        self.cs = make_logged_in_client(username="测试客服乙", groups=("客服",), nodes=("order.add_edit",))
        self.cs_user = User.objects.get(username="测试客服乙")

    def _post(self, **overrides):
        payload = {
            "game": self.game.id,
            "project_text": "满星",
            "hall_price": "66.00",
            "assignee": self.taker.pk,
        }
        payload.update(overrides)
        return self.cs.post(CREATE_URL, payload, content_type="application/json")

    def test_the_order_survives_and_the_code_is_distinguishable(self):
        response = self._post()
        body = response.json()
        self.assertEqual(response.status_code, 200)
        # 可分辨的业务码（不是 0、也不是笼统的 1002）
        self.assertEqual(body["code"], 1022, body)
        # 已建单号 + 原因都在 `data` 里
        self.assertEqual(set(body["data"]), {"order_no", "reason", "assign_code"})
        self.assertEqual(body["data"]["assign_code"], 1002)
        self.assertTrue(body["data"]["reason"])
        # ⛔ 不回滚：单在库里，而且就是响应里那个号
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(order.hall_price, Decimal("66.00"))
        # ⛔ 没有接单人（指派确实没成功）
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertIsNone(perf.user_id)
        # msg 里带单号与原因（页面「原样显示后端 msg」的那条路要能给出中间态）
        self.assertIn(order.order_no, body["msg"])
        self.assertIn(body["data"]["reason"], body["msg"])

    def test_the_same_target_as_the_sales_user_is_a_1022_not_a_500(self):
        """指派失败的另一支（`taker == sales_user`）也走 1022 —— 不是异常、不是 500。"""
        body = self._post(assignee=self.cs_user.pk).json()
        self.assertEqual(body["code"], 1022, body)
        self.assertIn(body["data"]["order_no"], body["msg"])
        self.assertTrue(Order.objects.filter(order_no=body["data"]["order_no"]).exists())

    def test_an_unexpected_error_in_the_assign_step_still_leaves_the_order(self):
        """⭐ 评审 Important-1 的后端判据：指派那一步**抛未预期异常**时（⛔ 不是可预期的业务失败）。

        形状：`assign_order` 抛 `RuntimeError` ⇒ 后端**响亮 500**（⛔ 不伪装成 `1022`/业务码，
        那是本仓明令禁止的方向），而**订单已经提交在库里**——这正是「指派失败不回滚订单」这条
        保证在**异常档**的样子（可预期那一档由上面那条 `1022` 的用例钉住）。
        ⚠️ 500 是**如实**的：调用方（页面）据此走「结果未知、先确认再重试」那条路
        （前端用例见 `order-addorder-quick-assign.spec.ts` 的「结果未知」一组）。
        """
        seen = {}

        def boom(order, **_kwargs):
            seen["order_no"] = order.order_no
            raise RuntimeError("boom：指派那一步炸了（模拟未预期异常）")

        # ⚠️ Django 测试 client 默认把未处理异常**抛回测试**；这里要的是**响应**
        #    （≥500），所以显式关掉 re-raise（`Client.raise_request_exception`）。
        self.cs.raise_request_exception = False
        with mock.patch(
            # ⛔ 指派那一步的动作体住在领域核心
            # `apps/business/order_intake.py::create_order` ⇒ `assign_order` 现在是在
            # **那个模块**里被查找的（E-0029：桩要打在**被查找的那个模块**上）。
            # 打在 `order_views` 上会变成**死桩**（本用例会当场红：订单会被真的指派掉、
            # 响应也不是 500 —— 实测，⛔ 不是静默通过）。判据一字未改。
            "apps.business.order_intake.assign_order",
            side_effect=boom,
        ):
            response = self._post()
        self.assertGreaterEqual(response.status_code, 500)
        # 订单**一行没少**（这是「不回滚」的判据，⛔ 不是「脚本没报错」）
        order = Order.objects.get(order_no=seen["order_no"])
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(order.hall_price, Decimal("66.00"))
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )
        # 建单那条留痕也在（订单是**完整**地落下来的，不是半截）
        self.assertTrue(
            OperationLog.objects.filter(order_no=order.order_no, action="order.create").exists()
        )

    def test_an_unknown_assignee_is_rejected_before_anything_is_created(self):
        body = self._post(assignee=999999).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(Order.objects.count(), 0)

    def test_an_inactive_assignee_is_rejected_before_anything_is_created(self):
        self.taker.is_active = False
        self.taker.save(update_fields=["is_active"])
        body = self._post().json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(Order.objects.count(), 0)


class DefaultAssigneeTests(TenantTestCase):
    """③「默认指派此人」：落 `StaffProfile.default_assignee`、随 `assignee-options` 回传、可清除。"""

    def setUp(self):
        self.game = _make_game()
        self.taker = _make_taker("测试打手甲", first_name="测试昵称甲")
        self.cs = make_logged_in_client(username="测试客服甲", groups=("客服",), nodes=CS_NODES)
        self.cs_user = User.objects.get(username="测试客服甲")

    def _post(self, **overrides):
        payload = {
            "game": self.game.id,
            "project_text": "满星",
            # ⚠️ **接单价必给**：`assign_order` → `set_taker` 会按「有效基价 × 提成率」重算，
            #    没有价（`null`）时它响亮拒绝（「该单没有接单价」）⇒ 指派那一步失败。
                        #    这正是「发单价 = 大厅价 = 指派价」那条裁定的实际后果（业务方）。
            "hall_price": "88.50",
            "assignee": self.taker.pk,
        }
        payload.update(overrides)
        return self.cs.post(CREATE_URL, payload, content_type="application/json")

    def _options(self, client=None):
        body = (client or self.cs).get(OPTIONS_URL).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def test_ticking_it_stores_the_person_on_the_operators_own_profile(self):
        body = self._post(remember_assignee="Y").json()
        self.assertEqual(body["code"], 0, body)
        profile = StaffProfile.objects.get(user=self.cs_user)
        self.assertEqual(profile.default_assignee_id, self.taker.pk)

    def test_the_next_recording_load_brings_the_person_back(self):
        self._post(remember_assignee="Y")
        data = self._options()
        self.assertEqual(data["defAssRecManUid"], self.taker.pk)
        self.assertEqual(data["defAssRecMan"], "测试昵称甲")

    def test_no_default_is_reported_as_zero_and_an_empty_name(self):
        data = self._options()
        self.assertEqual(data["defAssRecManUid"], 0)
        self.assertEqual(data["defAssRecMan"], "")

    def test_unticking_and_submitting_clears_it(self):
        self._post(remember_assignee="Y")
        self.assertEqual(StaffProfile.objects.get(user=self.cs_user).default_assignee_id, self.taker.pk)
        # 取消勾选并提交：**不带 assignee** 的「清除」也要成立（页面上选了「暂不指派」就是这一支）
        body = self._post(assignee=None, remember_assignee="N").json()
        self.assertEqual(body["code"], 0, body)
        self.assertIsNone(StaffProfile.objects.get(user=self.cs_user).default_assignee_id)
        data = self._options()
        self.assertEqual(data["defAssRecManUid"], 0)
        self.assertEqual(data["defAssRecMan"], "")

    def test_clearing_still_happens_when_the_assignment_fails(self):
        """清「默认」是操作人对自己偏好的要求，与这一单指派成不成功无关（⛔ 不静默丢掉用户意图）。"""
        self._post(remember_assignee="Y")
        no_assign_node = make_logged_in_client(
            username="测试客服丙", groups=("客服",), nodes=("order.add_edit",)
        )
        body = no_assign_node.post(
            CREATE_URL,
            {
                "game": self.game.id,
                "project_text": "满星",
                "assignee": self.taker.pk,
                "remember_assignee": "N",
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1022, body)
        # 清除仍然发生了 —— 但记在**操作人自己**的档案上（测试客服丙没有默认，所以没被改）
        self.assertEqual(StaffProfile.objects.get(user=self.cs_user).default_assignee_id, self.taker.pk)
        self.assertIsNone(
            StaffProfile.objects.get(user=User.objects.get(username="测试客服丙")).default_assignee_id
        )

    def test_ticking_without_a_person_is_rejected_and_creates_nothing(self):
        body = self._post(assignee=None, remember_assignee="Y").json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(Order.objects.count(), 0)

    def test_remembering_only_ever_touches_the_operators_own_profile(self):
        """默认接手人**跟录单的客服走**：别人的默认不受影响、也不被读出来。"""
        other = make_logged_in_client(username="测试客服丁", groups=("客服",), nodes=CS_NODES)
        self._post(remember_assignee="Y")
        self.assertEqual(StaffProfile.objects.get(user=self.cs_user).default_assignee_id, self.taker.pk)
        self.assertIsNone(StaffProfile.objects.get(user=User.objects.get(username="测试客服丁")).default_assignee_id)
        self.assertEqual(self._options(other)["defAssRecManUid"], 0)

    def test_the_remember_flag_does_not_leak_a_value_that_was_not_assigned(self):
        """指派失败时**不**记默认（记了下一单还会自动带出一个指派不成功的人）。"""
        no_assign_node = make_logged_in_client(
            username="测试客服戊", groups=("客服",), nodes=("order.add_edit",)
        )
        body = no_assign_node.post(
            CREATE_URL,
            {
                "game": self.game.id,
                "project_text": "满星",
                "assignee": self.taker.pk,
                "remember_assignee": "Y",
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1022, body)
        self.assertIsNone(
            StaffProfile.objects.get(user=User.objects.get(username="测试客服戊")).default_assignee_id
        )


class DefaultThrowToHallPreferenceTests(TenantTestCase):
    """④「默认抛内部抢单」那一档（业务方 2026-10-08 裁定 R1/R2；本线 `feat/default-assign-hall`）。

    这一档与「默认指派此人」是**同一颗勾选框记住的两档**：记人 ⇒ `default_assignee`；
    记内部抢单 ⇒ `default_throw_to_hall`。**两列不许同时为真**（由唯一写入口保证，无 DB 约束）。

    本类钉的六件事（缺一条都不算落地）：
    1. `remember_assignee="hall"` + `throw_to_hall=true` + 价 > 0 ⇒ 建单成功，且那一列变真；
    2. 已经记着某个人 ⇒ 再发 `"hall"` ⇒ 那个人被清（**互斥**在写入口一处生效）；
    3. 已经记着 hall ⇒ 发 `"Y"` + `assignee` ⇒ hall 变假、那个人被记上（反方向）；
    4. `"N"` ⇒ 两列都清 —— **三条路径各一条**（无后续动作 / 指派失败 `1022` / 抛单失败 `1023`）；
    5. `"hall"` 但不带 `throw_to_hall` ⇒ 业务码 `1000` + **一行都没写**（订单与偏好都不动）；
    6. 抛单失败（`1023`）时 `"hall"` **不落库**（照 `"Y"` 只在指派成功之后落的先例）。

    ⭐ **修复轮 2026-10-08 追加（总指挥裁决 4 / 5）**：
    - 第 5 条的判据换成**没有档案行**的账号（租户管理员）跑同一条闸 ⇒ 断言
      `StaffProfile.objects.filter(user=…).exists()` 仍为 `False`（「连 `get_or_create`
      都不该发生」这半句原来没有判据 —— 旧夹具那个档案行**本来就存在**）；
    - 新增 `test_a_pre_create_gate_does_not_clear_the_saved_preference`：`"N"` 的「无条件清除」
      是**建单之后**无条件的 —— **建单之前**那几道 `1000` 的闸**不清**偏好；
    - 本类第 7 条（「同值不写库」）的判据由「比 `updated_at`」换成**数 `UPDATE` 语句 = 0 条**
      （自带反向控制：真改了 ⇒ 恰好 1 条）。

    ⚠️ 夹具一律合成值（E-0077）；判据用**用户名 / 昵称**这类可指认的字段，不用主键指认跨租户的行。
    """

    def setUp(self):
        self.game = _make_game()
        self.taker = _make_taker("测试打手甲", first_name="测试昵称甲")
        self.cs = make_logged_in_client(
            username="测试客服甲", groups=("客服",),
            nodes=(*CS_NODES, "order.send_to_grab_hall"),
        )
        self.cs_user = User.objects.get(username="测试客服甲")

    def _post(self, **overrides):
        """不带 `assignee` 的建单底稿（这一档刻意不指派任何人）。"""
        payload = {
            "game": self.game.id,
            "project_text": "满星",
            "hall_price": "88.50",
        }
        payload.update(overrides)
        return self.cs.post(CREATE_URL, payload, content_type="application/json")

    def _profile(self, username="测试客服甲"):
        return StaffProfile.objects.get(user=User.objects.get(username=username))

    def _options(self, client=None):
        body = (client or self.cs).get(OPTIONS_URL).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def _failing_throw(self):
        """让抛单那一步抛**可预期**的域层拒绝（= `1023` 要接的那一档）。

        桩打在被查找的模块上：录单（含抛单）在 `order_intake.create_order` 里找
        `apply_transition`。打在 `order_views` 上是死桩，本组会在 `code=0` 上红。
        """
        return mock.patch(
            "apps.business.order_intake.apply_transition",
            side_effect=IllegalTransition("测试：这张单现在不能进大厅"),
        )

    def test_hall_with_a_price_stores_the_preference_and_reports_it_back(self):
        """① 建单 + 抛单同一次成功 ⇒ `default_throw_to_hall=True`，且读面回 `defAssIsHall=True`。"""
        body = self._post(throw_to_hall=True, remember_assignee="hall").json()
        self.assertEqual(body["code"], 0, body)
        profile = self._profile()
        self.assertTrue(profile.default_throw_to_hall)
        self.assertIsNone(profile.default_assignee_id)
        # ⭐ 判据打在**调用方真正看到的载荷**上（E-0176 的口径）：下次录单据此预选中那一档
        data = self._options()
        self.assertIs(data["defAssIsHall"], True)
        self.assertEqual(data["defAssRecManUid"], 0)
        self.assertEqual(data["defAssRecMan"], "")

    def test_revoked_hall_permission_hides_the_saved_default(self):
        body = self._post(throw_to_hall=True, remember_assignee="hall").json()
        self.assertEqual(body["code"], 0, body)
        self.assertIs(self._options()["defAssIsHall"], True)

        role = Role.objects.get(name="客服")
        role.permissions.remove(role.permissions.get(code="order.send_to_grab_hall"))

        data = self._options()
        self.assertIs(data["defAssIsHall"], False)
        self.assertEqual(data["defAssRecManUid"], 0)
        self.assertEqual(data["defAssRecMan"], "")
        self.assertTrue(self._profile().default_throw_to_hall)

        grant_nodes(self.cs_user, "order.send_to_grab_hall")
        self.assertIs(self._options()["defAssIsHall"], True)

    def test_sending_hall_clears_the_person_that_was_remembered(self):
        """② 互斥：先记着一个人，再发 `"hall"` ⇒ 那个人被清掉（两列不同时为真）。"""
        first = self._post(assignee=self.taker.pk, remember_assignee="Y").json()
        self.assertEqual(first["code"], 0, first)
        # 正向前置：那个人**确实**记上了（否则下面「被清掉」可能只是夹具没建出来）
        self.assertEqual(self._profile().default_assignee_id, self.taker.pk)

        body = self._post(throw_to_hall=True, remember_assignee="hall").json()
        self.assertEqual(body["code"], 0, body)
        profile = self._profile()
        self.assertTrue(profile.default_throw_to_hall)
        self.assertIsNone(profile.default_assignee_id)

    def test_sending_a_person_after_hall_switches_the_preference_back(self):
        """③ 反方向：已经记着 hall ⇒ 发 `"Y"` + `assignee` ⇒ hall 变假、那个人被记上。"""
        self._post(throw_to_hall=True, remember_assignee="hall")
        self.assertTrue(self._profile().default_throw_to_hall)

        body = self._post(assignee=self.taker.pk, remember_assignee="Y").json()
        self.assertEqual(body["code"], 0, body)
        profile = self._profile()
        self.assertFalse(profile.default_throw_to_hall)
        self.assertEqual(profile.default_assignee_id, self.taker.pk)
        data = self._options()
        self.assertIs(data["defAssIsHall"], False)
        self.assertEqual(data["defAssRecManUid"], self.taker.pk)
        self.assertEqual(data["defAssRecMan"], "测试昵称甲")

    def test_unticking_clears_both_columns_without_any_follow_up_action(self):
        """④-a `"N"` 的第一条路径：这一单既没指派也没抛单 ⇒ 两列照样清。"""
        self._post(throw_to_hall=True, remember_assignee="hall")
        self.assertTrue(self._profile().default_throw_to_hall)

        body = self._post(remember_assignee="N").json()
        self.assertEqual(body["code"], 0, body)
        profile = self._profile()
        self.assertFalse(profile.default_throw_to_hall)
        self.assertIsNone(profile.default_assignee_id)
        self.assertIs(self._options()["defAssIsHall"], False)

    def test_unticking_clears_both_columns_when_the_assignment_fails(self):
        """④-b `"N"` 的第二条路径：指派失败（`1022`）也照清 —— 与这一单成不成功无关。"""
        self._post(throw_to_hall=True, remember_assignee="hall")
        self.assertTrue(self._profile().default_throw_to_hall)
        # ⚠️ 这个账号**只有** `order.add_edit`：够建单，不够走 `UNASSIGNED → IN_PROGRESS`
        #    （那条边的节点集是 `_ASSIGN` / `_CHANGE_STATUS` 等的并集）⇒ 指派必然被**节点门**拒。
        #    ⚠️ **组名刻意不复用「客服」**：夹具是往组上**并集**加节点的，复用同名组会把上面
        #    `setUp` 里 `CS_NODES`（含 `order.assign_taker`）一起并进来 ⇒ 指派会成功，用例**假绿**
        #    （实测踩到过一次：断言 `1022` 却拿到 `code=0`）。
        #    ⚠️ 组名必须 **≤ 5 字**（`Role.name` 的 `max_length=5`，超了会 `DataError`）。
        no_assign_node = make_logged_in_client(
            username="测试客服乙", groups=("建单组",), nodes=("order.add_edit",)
        )
        # 先在**这个账号自己**的档案上记上 hall（否则「清了两列」在一个本来就空的档案上恒真）
        set_default_assign_preference(User.objects.get(username="测试客服乙"), mode="hall")
        body = no_assign_node.post(
            CREATE_URL,
            {
                "game": self.game.id,
                "project_text": "满星",
                "hall_price": "88.50",
                "assignee": self.taker.pk,
                "remember_assignee": "N",
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1022, body)
        # 指认**是哪扇门**拒绝的（否则「指派失败」可能来自别的成因，用例就钉错了东西）
        self.assertIn("缺少所需权限节点", body["data"]["reason"])
        # 指派真的没发生（判据打在终态上，⛔ 不看「响应码是 1022」就完事）
        self.assertEqual(Order.objects.get(order_no=body["data"]["order_no"]).status, OrderStatus.UNASSIGNED)
        profile = self._profile("测试客服乙")
        self.assertFalse(profile.default_throw_to_hall)
        self.assertIsNone(profile.default_assignee_id)
        # 反向控制：**别的**客服的默认不受影响（只动操作人自己那一行）
        self.assertTrue(self._profile().default_throw_to_hall)

    def test_unticking_clears_both_columns_when_the_throw_fails(self):
        """④-c ⭐ `"N"` 的第三条路径：抛单失败（`1023`）也照清 —— **本线补的缺口**。

        ⚠️ 改之前这条路径**不清** `"N"`（只有 `1022` 那条清了），而全仓没有用例钉住「不清」
        ⇒ 属同一条语义的缺口，本线补齐（核查记录见 spec §2.4 第 3 条）。
        这条用例就是它的判据：拿掉那块清理，它会红。
        """
        set_default_assign_preference(self.cs_user, mode="hall")
        self.assertTrue(self._profile().default_throw_to_hall)
        with self._failing_throw():
            body = self._post(throw_to_hall=True, remember_assignee="N").json()
        self.assertEqual(body["code"], 1023, body)
        profile = self._profile()
        self.assertFalse(profile.default_throw_to_hall)
        self.assertIsNone(profile.default_assignee_id)
        self.assertIs(self._options()["defAssIsHall"], False)

    def test_hall_without_throwing_is_rejected_and_writes_nothing(self):
        """⑤ `"hall"` 但这一单没抛 ⇒ `1000` + 定稿文案 + **订单与偏好一行都不写**。

        ⚠️ **修复轮 裁决 5 的 L1）：判据换成一个「本来就没有档案行」的账号。**
        原来这条用的是 `setUp` 里那个客服（`make_logged_in_client` 建过 `StaffProfile`），
        于是 `assertFalse(profile.default_throw_to_hall)` 打在一个**本来就存在**的行上 ——
        「连 `get_or_create` 都不该发生」这半句**根本没有判据**（那行本来就在，读到的
        `False` 是列的默认值，不是「没被碰过」）。
        现在改用**租户管理员**（`is_superuser=True` + `games=None` ⇒ 按构造**没有档案行**，
        而管理员拿全部游戏 ⇒ 能过 `assert_game_allowed` 走到本闸）。
        判据 ⇒ 闸拒绝之后 `StaffProfile.objects.filter(user=…).exists()` 仍为 `False`。
        """
        admin = make_logged_in_client(
            username="测试管理员甲", is_superuser=True, games=None
        )
        gate_user = User.objects.get(username="测试管理员甲")
        # 前置：这个账号**真的**没有档案行（否则下面那条断言可能只是「没有这一列」的恒真形状）
        self.assertFalse(StaffProfile.objects.filter(user=gate_user).exists())
        before_orders = Order.objects.count()

        body = admin.post(
            CREATE_URL,
            {"game": self.game.id, "project_text": "满星", "remember_assignee": "hall"},
            content_type="application/json",
        ).json()

        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "勾选「默认抛内部抢单」时必须选择内部抢单")
        self.assertEqual(Order.objects.count(), before_orders)
        # ⭐ 连 `get_or_create` 都不该发生 ⇒ 档案行仍然不存在（这是本条的**真判据**）
        self.assertFalse(StaffProfile.objects.filter(user=gate_user).exists())

    def test_a_pre_create_gate_does_not_clear_the_saved_preference(self):
        """④-d ⭐ 「`"N"` 是无条件清除」的**边界**：建单之前那几道 `1000` 的闸**不清**偏好。

        修复轮 2026-10-08 裁决 4：`"N"` 的「无条件」是**在「已经建单」之后**无条件的
        （无后续动作 / `1022` / `1023` 三条路径都清）；**建单之前**那几道闸一行都不写
        ⇒ 也不清（那一档用户会改完再提交，偏好不该在中途被抹掉）。

        本条走**载荷自相矛盾**那道闸（`throw_to_hall` 与 `assignee` 同时给 ⇒ `1000`）：
        这是最容易读错的一格 —— 载荷里明明带着 `remember_assignee="N"`。
        """
        # 先记着一个人（有东西可清，否则「没被清」在那条路上恒真）
        self._post(assignee=self.taker.pk, remember_assignee="Y")
        self.assertEqual(self._profile().default_assignee_id, self.taker.pk)

        body = self._post(
            assignee=self.taker.pk, throw_to_hall=True, remember_assignee="N"
        ).json()

        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "内部抢单与指派接手人只能二选一")
        # ⭐ 判据：那一道闸**没有**替他清偏好（他还没提交过一个合法的载荷）
        self.assertEqual(self._profile().default_assignee_id, self.taker.pk)
        self.assertFalse(self._profile().default_throw_to_hall)

    def test_conflicting_payloads_are_rejected_before_anything_is_written(self):
        """④-e ⭐ **组合闸**（评审建议补的判据）：`throw_to_hall` 与指派那一侧**同时给** ⇒ 建单之前 `1000` + 一行不写。

        为什么还要这一条：上面 ④-d / ⑤ 钉的都是**单键**（`throw_to_hall`+`assignee`、
        `"hall"` 没抛单）。这里钉**两个键的组合**，并顺带把「哪一句文案先出」也钉住
        —— 载荷形状错（二选一）要**先**报，⛔ 不能被「勾了记住却没选人」那句抢先
        （三道闸的顺序是刻意的，见 `order_intake.create_order`）。

        ① `throw_to_hall=true` + `assignee` + `remember_assignee="Y"`
           ⇒ `1000`「内部抢单与指派接手人只能二选一」（⛔ 不是「勾选「默认指派此人」时必须同时选择接手人」）；
        ② `remember_assignee="hall"` + `assignee`（没抛单）
           ⇒ `1000`「勾选「默认抛内部抢单」时必须选择内部抢单」。

        两发都**一行没写**，判据三条（⛔ 不看「响应码是 1000」就完事）：
        订单数不变 · 偏好仍是**前置写下的那个非默认值** · 这两发的 SQL 里打在
        `accounts_staffprofile` 上的 `UPDATE` **一条都没有**（用 `CaptureQueriesContext` 数）。
        """
        # 前置：先记上 hall（**非默认值** —— 若被清掉，下面那条 `assertTrue` 会红）
        set_default_assign_preference(self.cs_user, mode="hall")
        self.assertTrue(self._profile().default_throw_to_hall)
        before_orders = Order.objects.count()
        table = StaffProfile._meta.db_table

        with CaptureQueriesContext(connection) as captured:
            first = self._post(
                assignee=self.taker.pk, throw_to_hall=True, remember_assignee="Y"
            ).json()
            second = self._post(assignee=self.taker.pk, remember_assignee="hall").json()

        self.assertEqual(first["code"], 1000, first)
        self.assertEqual(first["msg"], "内部抢单与指派接手人只能二选一")
        self.assertEqual(second["code"], 1000, second)
        self.assertEqual(second["msg"], "勾选「默认抛内部抢单」时必须选择内部抢单")

        # 一行都没写：没有订单、没有对档案表的 UPDATE、偏好还是前置那个值
        self.assertEqual(Order.objects.count(), before_orders)
        updates = [
            query["sql"]
            for query in captured.captured_queries
            if table in query["sql"] and query["sql"].lstrip().upper().startswith("UPDATE")
        ]
        self.assertEqual(updates, [])
        profile = self._profile()
        self.assertTrue(profile.default_throw_to_hall)
        self.assertIsNone(profile.default_assignee_id)

    def test_hall_is_not_stored_when_the_throw_fails(self):
        """⑥ 抛单失败（`1023`）⇒ `"hall"` **不落库**（照 `"Y"` 只在指派成功之后落的先例）。"""
        # 先记着一个人：这一单想改成 hall，但抛单没成 ⇒ 那个人的偏好**原样保留**
        self._post(assignee=self.taker.pk, remember_assignee="Y")
        self.assertEqual(self._profile().default_assignee_id, self.taker.pk)

        with self._failing_throw():
            body = self._post(throw_to_hall=True, remember_assignee="hall").json()
        self.assertEqual(body["code"], 1023, body)
        profile = self._profile()
        self.assertFalse(profile.default_throw_to_hall)
        self.assertEqual(profile.default_assignee_id, self.taker.pk)
        self.assertIs(self._options()["defAssIsHall"], False)

    def test_an_unchanged_preference_does_not_touch_the_profile_row(self):
        """⑥′「同值不写库」：第二次发同一档 ⇒ 对档案表的 `UPDATE` 语句**恰好 0 条**。

        ⚠️ **修复轮 裁决 5 的 L2）：判据从「比 `updated_at`」换成「数 UPDATE 条数」。**
        原来那条把结论押在**时间戳精度**上（同一个测试事务里两次 `save` 的 `auto_now` 可能落在
        同一微秒 ⇒ 它红不红取决于环境，属「假绿/假红」两可）。现在直接数 SQL：
        `CaptureQueriesContext` 收下这一发的语句，只看**打在 `accounts_staffprofile` 表上的
        `UPDATE`**（⛔ 不用 `assertNumQueries` 数总数 —— 它会把 django-tenants 的
        `SET search_path` 一起算进去，先例见 `tests/test_jidanpu_upstream_offer_price.py`）。
        ⚠️ 并且自带**反向控制**（下面第二段）：把偏好真的改掉 ⇒ 同一判据必须数到**恰好 1 条**
        —— 没有这一步，「0 条」可能只是过滤条件写错了表名（恒真）。
        """
        self._post(throw_to_hall=True, remember_assignee="hall")
        self.assertTrue(self._profile().default_throw_to_hall)
        table = StaffProfile._meta.db_table

        def profile_updates(**payload):
            with CaptureQueriesContext(connection) as captured:
                body = self._post(**payload).json()
            self.assertEqual(body["code"], 0, body)
            return [
                query["sql"]
                for query in captured.captured_queries
                if table in query["sql"] and query["sql"].lstrip().upper().startswith("UPDATE")
            ]

        # ① 同值再发一次 ⇒ 一条 UPDATE 都没有（`update_fields` 只在真的变了才 save）
        self.assertEqual(profile_updates(throw_to_hall=True, remember_assignee="hall"), [])
        # ② 反向控制：真的改了（`"N"` 把 hall 清掉）⇒ 同一判据数到**恰好 1 条**
        changed = profile_updates(remember_assignee="N")
        self.assertEqual(len(changed), 1, changed)
        self.assertFalse(self._profile().default_throw_to_hall)

    def test_person_mode_without_a_taker_is_a_programming_error(self):
        """写入口的守卫：`mode='person'` 不给 `taker` / 未知档位 ⇒ `ValueError`（**不是**业务码）。

        ⚠️ 这是**编程错误**，刻意不翻译成业务结果（E-0010：未预期的照旧响亮）。
        """
        with self.assertRaises(ValueError):
            set_default_assign_preference(self.cs_user, mode="person")
        with self.assertRaises(ValueError):
            set_default_assign_preference(self.cs_user, mode="不存在的档位")


class AssigneeOptionsShapeTests(TenantTestCase):
    """`assignee-options` 的**恰好六个**顶层键（⛔ 不放宽成 `toContain`）。

    ⭐ **本线由五个变六个**：多一个 `defAssIsHall`（默认档位是不是「内部抢单」）。
    见 `docs/specs/2026-10-08-default-assign-hall-design.md`。
    """

    def setUp(self):
        self.taker = _make_taker("测试打手甲", first_name="测试昵称甲")
        self.cs = make_logged_in_client(username="测试客服甲", groups=("客服",), nodes=CS_NODES)

    def test_the_top_level_keys_are_exactly_the_six(self):
        data = self.cs.get(OPTIONS_URL).json()["data"]
        self.assertEqual(
            set(data),
            {
                "items",
                "truncated",
                "roles",
                "defAssRecManUid",
                "defAssRecMan",
                "defAssIsHall",
            },
        )
        self.assertIsInstance(data["defAssRecManUid"], int)
        self.assertIsInstance(data["defAssRecMan"], str)
        self.assertIsInstance(data["defAssIsHall"], bool)
        # 候选项的形状**一点没变**（仍恰好五个键）——新键只加在顶层
        self.assertGreater(len(data["items"]), 0)
        for row in data["items"]:
            self.assertEqual(set(row), {"id", "name", "role", "tag", "remark"})

    def test_an_operator_without_a_profile_gets_zero_instead_of_a_crash(self):
        """没有档案行（例如 `seed_tenant` 的 `admin` 形态）⇒ `0` / `''`，不是 500。"""
        # ⚠️ 这里要「没有档案」那一支（默认的全部游戏会建档案）⇒ 显式 `games=None`。
        admin = make_logged_in_client(
            username="测试无档案管理员",
            groups=("客服",),
            nodes=("order.add_edit", "order.assign_taker"),
            games=None,
        )
        self.assertFalse(StaffProfile.objects.filter(user__username="测试无档案管理员").exists())
        body = admin.get(OPTIONS_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["defAssRecManUid"], 0)
        self.assertEqual(body["data"]["defAssRecMan"], "")


class CrossTenantDefaultAssigneeTests(TenantTestCase):
    """跨租户判别力用例（`AGENTS.md` §4 要求的那条：新增查询必须带租户上下文）。

    ⚠️ 本类要证的不是「B 的 id 在 A 里取不到」——两个 schema 的自增序列各自从 1 开始，
    id 天然重叠。要证的是：**默认接手人只在自己那个 schema 里读得到**。
    判据打在 `0` / `''`（而不是拿 B 的 pk 去比）+ 「B 那个人的名字一个字都不出现在响应里」。
    """

    #: B 租户里的接手人（名字带租户后缀，便于在响应里指认）。
    B_TAKER = "tenant-b-only-taker"

    @classmethod
    def setUpClass(cls):
        # ⚠️ 第二个租户必须在 `setUpClass` 建（在一个用例内部建 schema 会与外层事务相冲）——
        #    与 `tests/test_account_api.py::AccountCrossTenantTests` 同一手法。
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name="quick_assign_b", name="快速指派租户 B", domain="quick-assign-b.test.local"
        )
        # `provision_tenant` 只在**新建** schema 时灌基线数据，所以这里显式再调一次
        # （不变量「B 也有 admin」是本类的前提，不能依赖 schema 是本次运行刚建的）。
        seed_tenant(cls.tenant_b)

    def test_tenant_bs_default_assignee_is_invisible_in_tenant_a(self):
        with tenant_context(self.tenant_b):
            b_taker = User.objects.create_user(username=self.B_TAKER, password="pw-123456")
            b_admin = User.objects.get(username="admin")
            b_profile, _ = StaffProfile.objects.get_or_create(user=b_admin)
            b_profile.default_assignee = b_taker
            b_profile.save(update_fields=["default_assignee", "updated_at"])
            # 正向前置：B 那边**确实**设上了（没有这一条，「A 读到 0」可能只是因为夹具没建出来）。
            self.assertEqual(
                StaffProfile.objects.get(user=b_admin).default_assignee_id, b_taker.pk
            )

        # A 的操作人：**没有**设过默认 ⇒ 必须读成 0 / ''，⛔ 不是 B 的那一份。
        cs = make_logged_in_client(username="测试客服甲", groups=("客服",), nodes=CS_NODES)
        body = cs.get(OPTIONS_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["defAssRecManUid"], 0)
        self.assertEqual(body["data"]["defAssRecMan"], "")
        # 反向前置：B 的那个名字在 A 的 schema 里本来就查不到（否则上面那句可能只是
        # 「名字恰好没被渲染」而不是「读的是自己那份」）。
        self.assertFalse(User.objects.filter(username=self.B_TAKER).exists())
        self.assertNotIn(self.B_TAKER, str(body))

    def test_tenant_bs_hall_default_is_invisible_in_tenant_a(self):
        """`defAssIsHall` 也是「操作人**自己的**偏好」⇒ B 记着的那一档在 A 里读不到。

        ⚠️ 判据**不是**「B 的主键在 A 里取不到」（两个 schema 的自增序列各自从 1 开始 ⇒
        拿主键指认恒真/恒假，`AGENTS.md` §5-G 那条），而是 A 的读面**必须是 `False`**——
        它若被 B 那一行串了，就会是 `True`。
        """
        with tenant_context(self.tenant_b):
            b_admin = User.objects.get(username="admin")
            b_profile, _ = StaffProfile.objects.get_or_create(user=b_admin)
            b_profile.default_throw_to_hall = True
            b_profile.default_assignee = None
            b_profile.save(update_fields=["default_throw_to_hall", "default_assignee", "updated_at"])
            # 正向前置：B 那边**确实**设上了（没有这一条，「A 读到 False」可能只是夹具没建出来）
            self.assertTrue(StaffProfile.objects.get(user=b_admin).default_throw_to_hall)

        # A 的操作人：**没有**设过 ⇒ 必须读成 False / 0 / ''（⛔ 不是 B 的那一份）
        cs = make_logged_in_client(
            username="测试客服甲", groups=("客服",),
            nodes=(*CS_NODES, "order.send_to_grab_hall"),
        )
        body = cs.get(OPTIONS_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertIs(body["data"]["defAssIsHall"], False)
        self.assertEqual(body["data"]["defAssRecManUid"], 0)
        self.assertEqual(body["data"]["defAssRecMan"], "")
        # 反向对照（证明上面那句 `False` 不是「这个键恒为 False」）：同一个租户里真设上就会变 True
        set_default_assign_preference(User.objects.get(username="测试客服甲"), mode="hall")
        self.assertIs(cs.get(OPTIONS_URL).json()["data"]["defAssIsHall"], True)
