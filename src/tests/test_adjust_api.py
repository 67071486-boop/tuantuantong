"""B1-5：`/adjust` 改绩效的接单价覆盖值 / 扣点（spec §5 / §6.3 / §8.1 / §8.3；P4 的 B3、B4）。

规则（每条都有会红的测试）：
- **谁**：持类级节点 `order.submit_payout_price` 的人（**与职位无关**；Task 4 起 `{CS}`
  职位门已删，见 spec §6 第 12 行）；**只有载荷里带 `taker_price` 时**才额外要「能改接单价」
  （spec §4.3 的动作表：「改绩效金额 / 扣点」按节点放行，**「改接单价」单独一行**）。
  `/adjust` 是两层：类级节点门
  `order.submit_payout_price`（`PERFORMANCE_PERMISSIONS`）+ body 层
  `can_edit_taker_price`（`order.restrict_edit_taker_price` 的**反面**，flag fail-open）。
  ⚠️ **flag 被触发时不是 403**（spec §4.2 / §5.2）：只忽略 `taker_price` 这一个键、
  同一载荷的其余字段照常应用，响应仍是 `code 0`（对标 1165「只能编辑其它的内容」）。
  legacy 的 `business.override_taker_price` **不再参与判据**（D-0028：实现保留但冻结）；
- **已结账**不得改 ⇒ `PERFORMANCE_STATE_INVALID`(**1014**)，**不是** P4 的 `1006`（B4）；
- **没有接单人的绩效不得改**（P3b 计划 Q5 的裁定）⇒ 同样 1014、msg 可辨；
- 改**基数**（接单价）⇒ 按 §6.1 的同一口径**重算派生金额**（`temp_amount`；订单**当前状态**
  是 `COMPLETED` 时连 `bonus_amount` 一起 —— F1 / 裁定 A 起判据是订单状态，**不是**
  `review_status`，见下面 `test_a_submitted_row_on_a_non_completed_order_keeps_its_bonus`）；
- 每个被改字段各写一行**留痕**（含前后值 + 操作人 + 原因），**同事务**；
- ⚠️ **D-0015**：金额可以为负/零——`taker_price = 0` 与负数都是**合法**的（不设 `min_value`）；
- ⚠️ **B3**：改扣点**不动** `bonus_amount`（扣点由 P4 在应付净额里单独减）；
- ⭐ **R3（编排方裁定）**：显式 `bonus_amount` 键**只在订单当前状态 == `COMPLETED`**
  时放行；非完成态 + `bonus_amount != 0` ⇒ 拒（`1014` + 逐字文案），显式 `0` 放行（归零）。

⚠️ **本任务顺带修一处不变量冲突**：`reset_taker_performance`（清除接单人）原本只回退**三元组**，
**不清 `taker_price`** ⇒ 「指派 → `/adjust` 设覆盖价 → `/clear-taker` → 抛单」之后，
那张单会**带着上一个人的覆盖价**回到大厅——与 spec §6.3.1 记的裁决
「池内 `taker_price` 恒为空」**直接矛盾**。本文件最后两条用例把修复钉住。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
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
from tests.p5_auth_helpers import grant_games, grant_nodes, make_logged_in_client

#: 本文件走到的节点门。`/performances/*` 的类级门是
#: `PerformanceViewSet.PERFORMANCE_PERMISSIONS`（`/adjust` → `order.submit_payout_price`）；
#: 订单端点在 `OrderViewSet.ORDER_PERMISSIONS`。`groups` 只建 Group、不放行。
ADJUST_ORDER_NODES = (
    "order.set_offer_price",          # `/hall-price/`（改发单价）
    "order.send_to_grab_hall",        # `/throw-to-hall/`（2026-10-26 起**独占闸**）
    "order.submit_payout_price",
    "order.clear_taker",              # `/clear-taker/`
    "grab_hall.take_order",           # 大厅
    "order.list",
    "order.view_detail",
)

PERCENT = CommissionMode.ORDER_PERCENT


class AdjustApiTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game, order_no="20260920000000000001",
            status=OrderStatus.IN_PROGRESS, hall_price=Decimal("88.00"),
        )
        # 客服——真实登录夹具每次建号，所以「接单人」用 `create_user` 即可。
        # ⚠️ legacy 的 `business.override_taker_price` 不再挂。
        #    `/adjust` 的 body 层判的是 `can_edit_taker_price`（flag 的反面），
        #    挂它只会让"判据到底看谁"变得含糊（要证明旧 codename 无效，
        #    用 `test_the_restrict_flag_denies_the_override` 的显式 flag 更直接）。
        self.taker = User.objects.create_user(username="taker1")
        StaffProfile.objects.create(
            user=self.taker, commission_mode=PERCENT, commission_value=Decimal("20.00")
        )
        self.perf = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=self.taker,
            temp_amount=Decimal("88.00"),
        )
        self.client = make_logged_in_client(
            username="cs1", groups=("客服",), nodes=ADJUST_ORDER_NODES
        )
        self.cs = User.objects.get(username="cs1")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 客服在「清除接单人 / 抛单到大厅」两条用例里要读改订单（读侧过
        # `game_scope`、写侧过 `assert_game_allowed`），缺档案 ⇒ 读侧 1002、
        # 清不掉覆盖价 —— 那两条用例的期望值一个字没改。
        grant_games(self.cs, self.game)
        self.url = f"/api/business/performances/{self.perf.id}/adjust/"

    def _post(self, payload=None):
        return self.client.post(self.url, payload or {}, content_type="application/json")

    # ── 正常路径：改接单价 ──────────────────────────────────────
    def test_cs_with_perm_changes_the_override_and_audits_it(self):
        body = self._post({"taker_price": "66.00", "reason": "客户议价"}).json()
        self.assertEqual(body["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.taker_price, Decimal("66.00"))
        # 派生金额按同一口径重算：66.00 × 20 / 100 = 13.20（钉字面值）
        self.assertEqual(self.perf.temp_amount, Decimal("13.20"))
        row = OperationLog.objects.get(
            order_no=self.order.order_no, action="performance.adjust"
        )
        self.assertEqual(row.actor, "cs1")
        self.assertEqual(row.log_type, 20)
        # 字段的中文名由模板 + `FIELD_LABELS` 派生（R10：reason 只放**纯值**）
        self.assertIn("接单价覆盖值", row.summary)
        self.assertEqual(row.reason, "客户议价")
        self.assertEqual(row.field_name, "taker_price")
        self.assertEqual(row.target_name, "taker1")
        self.assertEqual(row.from_value, "")          # 改前没有覆盖值（为空）
        self.assertEqual(row.to_value, "66.00")

    def test_the_order_row_is_locked_before_the_performance_row(self):
        """⭐ I4（终审修复轮）：`/adjust/` 统一锁序 —— 先锁 Order、再锁 Performance。

        `/adjust/` 与 `/adjust-amount/` 是同一形状（`apply_amount_to_effective_base` 会写
        `Order.hall_price`），所以 I4 一并统一（⛔ 只改别的三处、漏下这里会留着同一条 AB-BA）。
        判据是**这次调用发出的 SQL 顺序**；判别力：删掉 `lock_order_row(perf.order)` ⇒ 红。
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from tests.lock_order_helpers import assert_order_locked_before_performance

        with CaptureQueriesContext(connection) as captured:
            body = self._post({"taker_price": "66.00"}).json()
        self.assertEqual(body["code"], 0, body)
        assert_order_locked_before_performance(self, captured)
        # 哨兵：确实改了（不是在一笔失败的调用上断锁序）。
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.taker_price, Decimal("66.00"))

    def test_clearing_the_override_falls_back_to_the_order_price(self):
        self.perf.taker_price = Decimal("300.00")
        self.perf.save(update_fields=["taker_price"])
        body = self._post({"taker_price": None}).json()
        self.assertEqual(body["code"], 0)
        self.perf.refresh_from_db()
        self.assertIsNone(self.perf.taker_price)
        self.assertEqual(self.perf.temp_amount, Decimal("17.60"))     # 88.00 × 20%

    def test_a_submitted_row_on_a_non_completed_order_keeps_its_bonus(self):
        """⭐ **F1 / 裁定 A 的语义反转**（2026-09-25）：判据是**订单当前状态**，不是 `review_status`。

        本单是 `IN_PROGRESS` ⇒ 改基数只动 `temp_amount`；`review_status` 非空**不再**是
        「同写生效绩效」的理由（旧实现正是看它 ⇒ 会把 `bonus_amount` 从 17.60 写成 20.00）。
        ⛔ 这不是放宽：完成态那一档的「连带写」由
        `test_a_completed_row_without_a_review_status_gets_its_bonus_recomputed` 正面钉住。
        """
        self.perf.review_status = ReviewStatus.PENDING
        self.perf.bonus_amount = Decimal("17.60")
        self.perf.save(update_fields=["review_status", "bonus_amount"])
        self.assertEqual(self._post({"taker_price": "100.00"}).json()["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.temp_amount, Decimal("20.00"))
        # 反向：非 COMPLETED ⇒ `bonus_amount` 一个字节都不动（旧实现在这里会写成 20.00）
        self.assertEqual(self.perf.bonus_amount, Decimal("17.60"))

    def test_a_completed_row_without_a_review_status_gets_its_bonus_recomputed(self):
        """⭐ 正向（F1 的缺口本身）：`COMPLETED` 且 `review_status is None` ⇒ 生效绩效也跟基数走。

        旧判据看 `review_status` ⇒ 这一档下 `temp_amount` 变了、`bonus_amount` 停在旧值
        （两套事实并存）——本用例在旧实现上变红。
        """
        self.order.status = OrderStatus.COMPLETED
        self.order.save(update_fields=["status"])
        self.perf.bonus_amount = Decimal("17.60")     # 陈旧值：重算后应变成 20.00
        self.perf.save(update_fields=["bonus_amount"])
        self.assertIsNone(self.perf.review_status)    # 前置哨兵：本档要的正是「没有 review_status」

        self.assertEqual(self._post({"taker_price": "100.00"}).json()["code"], 0)

        self.perf.refresh_from_db()
        self.assertEqual(self.perf.temp_amount, Decimal("20.00"))     # 100.00 × 20%
        self.assertEqual(self.perf.bonus_amount, Decimal("20.00"))

    def test_not_completed_performance_keeps_its_bonus_at_zero(self):
        """非完成态 ⇒ 生效绩效不动（判据是**订单当前状态**，与 `review_status` 无关）。"""
        self.assertEqual(self._post({"taker_price": "100.00"}).json()["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.temp_amount, Decimal("20.00"))
        self.assertEqual(self.perf.bonus_amount, Decimal("0.00"))     # 非 COMPLETED ⇒ 不动

    # ── ⭐ R3：显式 `bonus_amount` 键（非完成态 + 非 0 ⇒ 拒；`0` 放行）────
    #: R3 文案逐字（⛔ 不 import 生产常量：自指断言在被断言的常量被改坏时不红，E-0024）。
    R3_MSG = "生效绩效只在订单变成「已完成」时按预计绩效生成，不能在这里直接改"

    def test_an_explicit_non_zero_bonus_on_a_non_completed_order_is_refused(self):
        """⭐ R3（spec §4.2.3 / §7.3 #27）：非完成态 + 显式 `bonus_amount="-1.00"` ⇒ 拒。

        负数在 D-0015 之下是合法**金额**，但它不是「归零」⇒ 按 R3 的字面口径同样拒。
        判别力：漏掉 `explicit_bonus` 那一段判据 ⇒ 请求成功、`bonus` 被写成 `-1.00` ⇒ 本条红。
        """
        self.perf.bonus_amount = Decimal("17.60")
        self.perf.review_status = ReviewStatus.PENDING
        self.perf.save(update_fields=["bonus_amount", "review_status"])

        body = self._post({"bonus_amount": "-1.00"}).json()

        self.assertEqual(body["code"], ErrorCode.PERFORMANCE_STATE_INVALID, body)
        self.assertEqual(int(body["code"]), 1014)
        self.assertEqual(body["msg"], self.R3_MSG)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.bonus_amount, Decimal("17.60"))
        self.assertEqual(self.perf.temp_amount, Decimal("88.00"))
        self.assertFalse(
            OperationLog.objects.filter(order_no=self.order.order_no).exists()
        )

    def test_an_explicit_zero_bonus_on_a_non_completed_order_is_allowed(self):
        """⭐ R3 的 `0` 边界（E-0021：判据用**值比**，⛔ 不用真值）：显式 `"0.00"` ⇒ **放行**（归零）。

        判别力：把判据写成 `if data["bonus_amount"]:` ⇒ 本条**仍绿**（0 是假值、被当成「没传」）
        ⇒ 必须与上一条（`-1.00` 拒）**同时**存在才杀得掉「一律放行」与「一律拒绝」两种坏实现。
        """
        self.perf.bonus_amount = Decimal("17.60")
        self.perf.save(update_fields=["bonus_amount"])

        body = self._post({"bonus_amount": "0.00", "reason": "清空"}).json()

        self.assertEqual(body["code"], 0, body)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.bonus_amount, Decimal("0.00"))     # 归零
        self.assertEqual(self.perf.temp_amount, Decimal("88.00"))     # 预计值不动
        row = OperationLog.objects.get(
            order_no=self.order.order_no, action="performance.adjust"
        )
        self.assertEqual(row.field_name, "bonus_amount")
        self.assertEqual(row.from_value, "17.60")
        self.assertEqual(row.to_value, "0.00")

    def test_a_completed_order_still_accepts_an_explicit_bonus(self):
        """反向对照（防「一刀切全拒」恒真）：`COMPLETED` + 未结账 + 显式 `bonus_amount` ⇒ 放行。"""
        self.order.status = OrderStatus.COMPLETED
        self.order.save(update_fields=["status"])

        body = self._post({"bonus_amount": "5.00"}).json()

        self.assertEqual(body["code"], 0, body)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.bonus_amount, Decimal("5.00"))

    # ── 权限（两个方向）────────────────────────────────────────
    def test_the_restrict_flag_denies_the_override(self):
        """body 层 = `can_edit_taker_price`。

        flag 是**限制型**且 fail-open（没挂它 = 允许），所以"限制"必须显式挂上
        `order.restrict_edit_taker_price`——与 `/throw-to-hall`、`/assign` 的
        `order.submit_payout_price` 各回答一问（那一问是"能不能改价"，flag 这一问是
        "本租户是否禁改价"）。

        ⚠️ **它同时是 flag**：失败表现**不是 403**（spec §4.2 / §5.2）——请求仍然 200 /
        `code 0`，只是 `taker_price` 这一个键被忽略（响应 `msg` 注明），也没有留痕。
        """
        grant_nodes(self.cs, "order.restrict_edit_taker_price")
        response = self._post({"taker_price": "66.00"})
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertEqual(response.json()["msg"], "接单价已按权限忽略")
        self.perf.refresh_from_db()
        self.assertIsNone(self.perf.taker_price)                      # 没被改
        self.assertFalse(OperationLog.objects.filter(order_no=self.order.order_no).exists())

    def test_the_restrict_flag_does_not_block_a_deduction_change(self):
        """spec §4.3：改**扣点**只要持 `order.submit_payout_price` 的人——限制 flag 只挡接单价这一项。"""
        grant_nodes(self.cs, "order.restrict_edit_taker_price")
        body = self._post({"deduct_mode": "1", "deduct_value": "10.00"}).json()
        self.assertEqual(body["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.deduct_mode, "1")
        self.assertEqual(self.perf.deduct_value, Decimal("10.00"))

    def test_a_node_holder_can_adjust_regardless_of_job_title(self):
        """Task 4：`/adjust` 只剩**节点**判据 ⇒ 持节点的代练员也能改（职位不再参与判据）。

        ⚠️ **本条替换了旧的 `test_taker_cannot_adjust`**：旧用例断言「代练员不是客服 ⇒ 403」
        （`{CS}` 职位门与节点门是 `and`）。单轴化（spec §6 第 12 行）删掉职位门后，
        目标形态就是「**持节点即可、与职位无关**」：同一个动作不再有两套判据，
        自定义职位与内置职位走同一条路。这是 spec 登记的**放宽**方向
        （副作用：客服主管 / 任何被授予该节点的职位都能改金额）。
        ⚠️ **拒绝侧**（不持节点 ⇒ 403）由节点门本身保证，`tests/test_rbac_endpoint_enforcement.py`
        与 `tests/test_performance_api.py` 的节点门用例覆盖；这里只把放宽按实测钉一次。
        """
        taker_client = make_logged_in_client(
            username="taker_only", groups=("代练员",),
            nodes=("order.submit_payout_price",),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。`/adjust` 经
        # `get_object()`（被 `game_scope` 收窄的 queryset）取这张绩效行 ⇒ 不补本夹具的
        # 游戏就会先撞上读侧的 **1002**（记录不存在），而本用例要断的是「持节点 ⇒ 改得动」
        # （业务码 0）。期望值一个字没改。
        grant_games(User.objects.get(username="taker_only"), self.game)
        response = taker_client.post(
            self.url, {"taker_price": "1.00"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 0, response.json())
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.taker_price, Decimal("1.00"))

    def test_d0219_order_list_alone_does_not_grant_adjust(self):
        """⭐ D-0219（2026-10-10）：`order.list` 只开「**进得了列表**」，不开 `/adjust`。

        列表门改成任一即进（1163 ∪ `order.list`）后，「持 `order.list` 但不持 1162」的账号
        进得了绩效列表页——但改打手绩效（行内改接单价）仍只认 1162
        （`order.submit_payout_price`），403 就是它的拒绝形状。业务方要的「能改」由
        生产授予满足（客服 / 客服主管本来就持 1162），本条钉住的是**门没有跟着放大**。

        ⚠️ 节点是**字面量**（⛔ 不从 `PERFORMANCE_PERMISSIONS` 派生）。
        ⚠️ 组名用本文件**独一份**的「D0219」：节点挂在**与组同名的 Role** 上
        （`p5_auth_helpers._grant_nodes`）而 Role.name 唯一——用「客服」会继承 setUp
        给它挂的 `ADJUST_ORDER_NODES`（含 1162），403 就成了假红（`test_performance_api`
        文件头登记过的同族坑）。
        ⚠️ 「持 1162 ⇒ 改得动」那一侧由上面
        `test_a_node_holder_can_adjust_regardless_of_job_title` 覆盖，这里只钉拒绝侧。
        """
        order_list_only = make_logged_in_client(
            username="cs_only_order_list", groups=("D0219",), nodes=("order.list",),
        )
        # 夹具补齐（不是放宽判据）：`/adjust` 经 `get_object()`（`game_scope` 收窄）取这张
        # 绩效行，缺档案会先撞 1002，而本用例要断的是 403。
        grant_games(User.objects.get(username="cs_only_order_list"), self.game)
        response = order_list_only.post(
            self.url, {"taker_price": "1.00"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 403, response.content[:200])
        # 没被改 + 零留痕（拒绝是真的，不是「提示了但照改」）
        self.perf.refresh_from_db()
        self.assertIsNone(self.perf.taker_price)
        self.assertFalse(OperationLog.objects.filter(order_no=self.order.order_no).exists())

    def test_anonymous_is_rejected(self):
        response = Client(SERVER_NAME="tenant.test.com").post(
            self.url, {"taker_price": "1.00"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)

    # ── 状态闸门 ────────────────────────────────────────────────
    def test_settled_performance_is_rejected_with_1014_not_1006(self):
        """**B4**：用 P3 的 `PERFORMANCE_STATE_INVALID`(1014)，**不要**用 P4 的 `1006`。"""
        self.perf.settle_status = SettleStatus.SETTLED
        self.perf.save(update_fields=["settle_status"])
        body = self._post({"taker_price": "66.00"}).json()
        self.assertEqual(body["code"], ErrorCode.PERFORMANCE_STATE_INVALID)
        self.assertEqual(int(body["code"]), 1014)
        self.assertNotEqual(int(body["code"]), 1006)
        self.assertIn("结账", body["msg"])
        self.perf.refresh_from_db()
        self.assertIsNone(self.perf.taker_price)
        self.assertFalse(OperationLog.objects.filter(order_no=self.order.order_no).exists())

    def test_the_other_two_settled_columns_alone_are_also_refused(self):
        """**R-10b / 裁定 A.2**：判据必须是**三字段并集**（`Performance.is_settled`），
        不是只看 `settle_status` 一列。

        下面两档都让 `settle_status` 保持「未结账」，只在 `settled_at`（存量行里
        P4-FE-0 之前结的账）或 `settled_amount` 上留值 ⇒ 只看状态列的实现在这里会
        放行并**真的改价**，本用例因此变红。
        """
        for field, value in (
            ("settled_at", timezone.now()),
            ("settled_amount", Decimal("8.80")),
        ):
            with self.subTest(settled_field=field):
                Performance.objects.filter(pk=self.perf.pk).update(
                    settle_status=SettleStatus.UNSETTLED,
                    settled_at=None,
                    settled_amount=None,
                )
                Performance.objects.filter(pk=self.perf.pk).update(**{field: value})

                body = self._post({"taker_price": "66.00"}).json()

                self.assertEqual(int(body["code"]), 1014, body)
                self.assertIn("结账", body["msg"])
                self.perf.refresh_from_db()
                self.assertIsNone(self.perf.taker_price)
                self.assertFalse(
                    OperationLog.objects.filter(order_no=self.order.order_no).exists()
                )

    def test_performance_without_a_taker_cannot_be_adjusted(self):
        """**Q5 的裁定**：没有接单人的绩效不得改金额（池内的 `taker_price` 恒为空）。"""
        self.perf.user = None
        self.perf.save(update_fields=["user"])
        body = self._post({"taker_price": "66.00"}).json()
        self.assertEqual(body["code"], ErrorCode.PERFORMANCE_STATE_INVALID)
        self.assertIn("接单人", body["msg"])
        self.assertIn("还没有接单人", body["msg"])                    # 与「已结账」那条 msg 可区分
        self.perf.refresh_from_db()
        self.assertIsNone(self.perf.taker_price)

    # ── 「禁止结算」：扣点拦截─────────────────────────
    def _freeze_taker(self, *, value=True):
        """把接单人设成「禁止结算」/ 解冻（`StaffProfile.settle_forbidden`）。"""
        profile = StaffProfile.objects.get(user=self.taker)
        profile.settle_forbidden = value
        profile.save(update_fields=["settle_forbidden"])
        return profile

    def test_a_frozen_taker_cannot_be_given_a_deduction(self):
        """Task 9：冻结的人 + 请求带扣点 ⇒ 拒绝，且 `deduct_mode` / `deduct_value` **都没变**。

        业务码取本端点既有的状态闸门码 `1014`（`PERFORMANCE_STATE_INVALID`，
        与「已结账 / 没有接单人」同码），**不新造一套**（改动 3 的明文要求）。
        """
        self._freeze_taker()

        body = self._post({"deduct_mode": "1", "deduct_value": "10.00"}).json()

        self.assertEqual(body["code"], ErrorCode.PERFORMANCE_STATE_INVALID)
        self.assertEqual(int(body["code"]), 1014)
        self.assertIn("禁止结算", body["msg"])
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.deduct_mode, "")
        self.assertEqual(self.perf.deduct_value, Decimal("0.00"))
        self.assertFalse(OperationLog.objects.filter(order_no=self.order.order_no).exists())

    def test_a_lone_deduct_value_from_a_frozen_taker_is_also_rejected(self):
        """判据是「本次带了 `deduct_mode` **或** `deduct_value`」——只带后者同样拦。"""
        self._freeze_taker()

        body = self._post({"deduct_value": "10.00"}).json()

        self.assertEqual(int(body["code"]), 1014)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.deduct_value, Decimal("0.00"))

    def test_a_frozen_taker_can_still_be_adjusted_without_a_deduction(self):
        """**不带扣点键**的请求照旧放行（本任务只拦「设扣点」）。两个子例的判别力**不同**：

        - `taker_price` 那一支**真的走到**冻结判据（`touches` 非空 ⇒ 不会在空载荷早退；
          再经过「已结账 / 没有接单人」两条 ⇒ 到达
          `if {"deduct_mode", "deduct_value"} & set(data)`）。把冻结判据写成**无条件**，
          这一支就会被拒 ⇒ 变红。
        - `reason` 那一支**到不了**冻结判据：`reason` 不在 `touches` 里、且字段有 `default=""`
          ⇒ 空载荷早退在更前面就返回了。它只能钉「既有的空载荷
          无副作用」行为，**不是**本条判据的判别力来源——如实写在这里，免得被当成冻结判据的证据。
        """
        self._freeze_taker()

        with self.subTest(payload="只改接单价（真的走到冻结判据）"):
            body = self._post({"taker_price": "66.00", "reason": "客户议价"}).json()
            self.assertEqual(body["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.taker_price, Decimal("66.00"))
        with self.subTest(payload="只写原因（走的是既有空载荷早退，不是冻结判据）"):
            audits_before = OperationLog.objects.count()
            body = self._post({"reason": "仅备注"}).json()
            self.assertEqual(body["code"], 0)
            self.assertEqual(OperationLog.objects.count(), audits_before)   # 无副作用
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.deduct_mode, "")          # 扣点一行都没写

    def test_unfreezing_lets_the_deduction_be_set(self):
        """解冻后再设扣点 ⇒ 放行。**前置哨兵**：冻结时那一次必须已经被拒，
        否则「解冻后绿」可能由「这条判据根本没生效」凑出来。"""
        profile = self._freeze_taker()
        frozen = self._post({"deduct_mode": "1", "deduct_value": "10.00"}).json()
        self.assertEqual(int(frozen["code"]), 1014)

        profile.settle_forbidden = False
        profile.save(update_fields=["settle_forbidden"])
        body = self._post({"deduct_mode": "1", "deduct_value": "10.00"}).json()

        self.assertEqual(body["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.deduct_mode, "1")
        self.assertEqual(self.perf.deduct_value, Decimal("10.00"))

    # ── 扣点与边界 ──────────────────────────────────────────────
    def test_deduction_change_does_not_touch_the_bonus(self):
        """**B3**：扣点是独立一项，`bonus_amount` 不含它（折进去 P4 会双重扣点）。"""
        self.perf.review_status = ReviewStatus.PENDING
        self.perf.bonus_amount = Decimal("17.60")
        self.perf.save(update_fields=["review_status", "bonus_amount"])
        self.assertEqual(self._post({"deduct_mode": "1", "deduct_value": "10.00"}).json()["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.bonus_amount, Decimal("17.60"))    # 没被减掉 10 元
        self.assertEqual(self.perf.deduct_value, Decimal("10.00"))

    def test_deduction_bounds_are_enforced_at_write_time(self):
        """两条对标物原文的上界（spec §6.1）：比例 ≤100%、固定额 ≤ 预计绩效总计。"""
        over_percent = self._post({"deduct_mode": "0", "deduct_value": "100.01"}).json()
        self.assertEqual(over_percent["code"], ErrorCode.VALIDATION)
        self.assertIn("100", over_percent["msg"])
        over_fixed = self._post({"deduct_mode": "1", "deduct_value": "88.01"}).json()
        self.assertEqual(over_fixed["code"], ErrorCode.VALIDATION)
        self.assertIn("预计绩效", over_fixed["msg"])
        # 正好等于上界：两端都合法
        self.assertEqual(self._post({"deduct_mode": "1", "deduct_value": "88.00"}).json()["code"], 0)
        self.assertEqual(self._post({"deduct_mode": "0", "deduct_value": "100.00"}).json()["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.deduct_mode, "0")

    def test_zero_and_negative_prices_are_allowed(self):
        """⚠️ **D-0015**：`0` 与负数都是合法金额——谁加了 `min_value`，本用例变红。"""
        self.assertEqual(self._post({"taker_price": "0.00"}).json()["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.taker_price, Decimal("0.00"))
        self.assertEqual(self.perf.temp_amount, Decimal("0.00"))       # 0 × 20% = 0
        self.assertEqual(self._post({"taker_price": "-10.00"}).json()["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.taker_price, Decimal("-10.00"))
        self.assertEqual(self.perf.temp_amount, Decimal("-2.00"))      # -10 × 20% = -2

    def test_malformed_and_out_of_choice_values_are_business_codes(self):
        """畸形值必须是 200 + 业务码，不是 HTML 500（E-0010）。"""
        for payload in (
            {"taker_price": "abc"},
            {"deduct_mode": "9"},
            {"deduct_value": "abc"},
            {"reason": "x" * 300},          # 超长 reason 不能进 varchar(255) 后 500
        ):
            with self.subTest(payload=payload):
                body = self._post(payload).json()
                self.assertEqual(body["code"], ErrorCode.VALIDATION)

    def test_empty_payload_has_no_side_effects(self):
        """空载荷**什么都不改也不留痕**（与账号线的「编辑空载荷无副作用」同一口径）。"""
        body = self._post({}).json()
        self.assertEqual(body["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.temp_amount, Decimal("88.00"))
        self.assertFalse(OperationLog.objects.filter(order_no=self.order.order_no).exists())

    def test_uncomputable_commission_mode_is_a_business_code(self):
        profile = StaffProfile.objects.get(user=self.taker)
        profile.commission_mode = CommissionMode.FIXED
        profile.save(update_fields=["commission_mode"])
        body = self._post({"taker_price": "66.00"}).json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION)
        self.perf.refresh_from_db()
        self.assertIsNone(self.perf.taker_price)          # 一行都没写
        self.assertFalse(OperationLog.objects.filter(order_no=self.order.order_no).exists())

    # ── 事务原子性 ──────────────────────────────────────────────
    def test_rolls_back_the_amount_when_the_audit_write_fails(self):
        seen = {}

        def _boom(**kwargs):
            seen["taker_price"] = Performance.objects.get(pk=self.perf.pk).taker_price
            raise RuntimeError("boom")

        with mock.patch(
            "apps.business.api.performance_views.record", side_effect=_boom
        ):
            with self.assertRaises(RuntimeError):
                self._post({"taker_price": "66.00"})
        self.assertEqual(seen["taker_price"], Decimal("66.00"))       # 哨兵：金额真的写进去了
        self.perf.refresh_from_db()
        self.assertIsNone(self.perf.taker_price)                      # 但回滚了
        self.assertEqual(self.perf.temp_amount, Decimal("88.00"))
        self.assertFalse(OperationLog.objects.filter(order_no=self.order.order_no).exists())

    # ── 不变量：池内 `taker_price` 恒为空（本任务修的冲突）────────
    def test_clearing_the_taker_drops_the_override(self):
        """⚠️ `reset_taker_performance` 必须**连 `taker_price` 一起回退**。

        否则「上一个人谈好的覆盖价」会留在绩效行上，下一个接单人（或大厅）会继承它。
        判别力：把 `taker_price` 从回退字段里去掉 ⇒ 本用例变红。
        """
        self.assertEqual(self._post({"taker_price": "300.00"}).json()["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.taker_price, Decimal("300.00"))     # 前置哨兵
        self.client.post(
            f"/api/business/orders/{self.order.order_no}/clear-taker/", {},
            content_type="application/json",
        )
        self.perf.refresh_from_db()
        self.assertIsNone(self.perf.taker_price)
        self.assertIsNone(self.perf.user_id)

    def test_hall_shows_the_default_price_again_after_the_taker_is_cleared(self):
        """端到端：覆盖价**经接口**不可达池内 ⇒ 大厅的生效价恒等于默认价（spec §6.3.1 的裁决）。"""
        self.assertEqual(self._post({"taker_price": "300.00"}).json()["code"], 0)
        self.client.post(
            f"/api/business/orders/{self.order.order_no}/clear-taker/", {},
            content_type="application/json",
        )
        self.client.post(
            f"/api/business/orders/{self.order.order_no}/throw-to-hall/", {},
            content_type="application/json",
        )
        taker_client = make_logged_in_client(
            username="hall_viewer", groups=("代练员",),
            nodes=("grab_hall.take_order",),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # hall_viewer 要读大厅（`/hall/` 过 `game_scope`），缺档案 ⇒ 空结果、
        # 本用例拿不到那一行；该用例的期望值一个字没改。
        grant_games(User.objects.get(username="hall_viewer"), self.game)
        rows = taker_client.get("/api/business/hall/").json()["data"]["items"]
        row = next(r for r in rows if r["order_no"] == self.order.order_no)
        self.assertEqual(Decimal(row["hall_price"]), Decimal("88.00"))  # 不是 300.00
        # 筛选同源：按 ≤100 能筛到它、按 ≥200 筛不到
        self.assertIn(
            self.order.order_no,
            [r["order_no"] for r in taker_client.get("/api/business/hall/", {"price_max": "100.00"}).json()["data"]["items"]],
        )
        self.assertNotIn(
            self.order.order_no,
            [r["order_no"] for r in taker_client.get("/api/business/hall/", {"price_min": "200.00"}).json()["data"]["items"]],
        )

    def test_performance_cannot_be_edited_by_a_generic_patch(self):
        """**结构**保证：绩效的金额只能经 `/submit` 与 `/adjust` 改，通用 `PATCH` 一律 405。

        判别力：给 `PerformanceViewSet` 加上通用编辑能力（继承 `ModelViewSet`、或声明
        `partial_update`）⇒ 路由被注册、请求变成 200（或 403）⇒ 本用例变红。
        它守的是「有人在 ViewSet 上顺手开一个通用编辑口」。

        ⚠️ **实测是 404 不是 405**：本 ViewSet 没有 `retrieve`/`update`，router 干脆不注册
        `performances/{pk}/` 这条 detail 路由（只有 `.../adjust/`、`.../review/`）。
        两个码都表示「结构上不可达」，所以断言取二者之一 + **值没变**——
        真正要钉的是**效果**（改不了），而不是 Django 用哪个码表达「没有这条路」。
        """
        response = self.client.patch(
            f"/api/business/performances/{self.perf.id}/",
            {"taker_price": "1.00"}, content_type="application/json",
        )
        self.assertIn(response.status_code, (404, 405))
        self.perf.refresh_from_db()
        self.assertIsNone(self.perf.taker_price)
