"""绩效域 / 结算域按游戏权限收窄（计划 `2026-09-23-scope-by-game-permission.md` 的 Task 3）。

**本文件钉的是「接线」**——规则本体（`game_scope`）的口径由 `tests/test_game_scope.py`
钉住；这里钉的是绩效域与结算域的查询**真的把那层过滤接上了**：

- 读路径：绩效列表 / 导出（`performance_views.performance_queryset`）、
  详情动作（`_base_queryset`）、调整记录链（`adjustment_views`）、
  结算列表 / 待归属 / 结账对象人员（`settlement_views`）；
- 写路径（结账 / 撤销结账 / 删除记录 / 人工修正）取行时的前置卡点
  （`settlement.py`、`performance_writes.py`）。

⚠️ **期望值一律写字面值**（E-0024）：业务码写 `1000` / `1002` / `1006` / `1014`、
状态写 `"未结账"` / `"已结账"`、订单号写 `"20260923000000000201"`——不 import
被测模块导出的常量（自指断言不会红）。
⚠️ **本文件的 `SettleStatus.…` 只剩 4 处，全在夹具里**（`_perf` / `_adjustment` /
`_settled_row` 的 `settle=` 入参）——那是**构造物**（"造一条已结账的行"），不是期望值；
**断言**里一律是字面值（收口评审 C-2 当场把 12 处断言从枚举改成字面值：6 处 `"未结账"`
+ 6 处 `"已结账"`。`TextChoices` 的取值本来就是 `"未结账"` / `"已结账"`，DB 回读就是这两个串
⇒ 断言写得出字面值，不需要枚举）。

⚠️ **否定断言用租户内唯一的业务值，不用主键指认「别人的那一行」**（`AGENTS.md` §5
硬规则：两个 schema 的主键序列各自从 1 开始）。本文件的判据是
**「行数 + 集合为空」**与**订单号 / 调整记录的 `reason` 字面量**，并一律配一条
**前置哨兵**（「那一行真的在库里」），否则「被收窄掉」与「压根没造出来」不可区分。

⚠️ **为什么 `_base_queryset`（详情动作）也收窄**（与 `order.restrict_to_self_created`
刻意不进 `get_queryset` 相反）：按游戏权限收窄的是**可见范围本身**——不在范围内的行
用户本就不该知道它存在 ⇒ 「对别人游戏的绩效 `/adjust` 拿 1002」是期望形态；
`restrict_to_self_created` 是对**可见范围内**的一行施加额外限制，用 404 会把
「被限制」伪装成「不存在」。理由与 `order_views.py` 的注释同源。

⚠️ **本文件比任务书的逐点表多覆盖 4 个调用点**（`settlement_views.py` 的三条读路径 +
`settlement._reject_message` 里那条 `PerformanceAdjustment` 查询），如实登记：

① 计划的 **DoD #1** 是「22 处卡点里 12 处读路径的 **10 处**接线、**2 处**
（`notification_views` / `my_orders_views`）刻意不接」。按模型把这 22 处分堆后，
读路径恰好 12 处：订单 2 + 退款列表 1 + 绩效 2 + 调整记录列表 1 +
**结算读 3**（结算列表 / 待归属 / 结账对象人员）+ 通知 1 + 我的订单 1 + 接单统计 1
⇒ **不接 `settlement_views.py` 那 3 处就凑不出 DoD 承诺的 10/12**，
而本仓没有别的任务认领那个文件（Task 2 = 订单、Task 4 = 统计、Task 5 = 前端）。
② `settlement._reject_message` 里那条 `PerformanceAdjustment` 查询是 2026-09-23
（钱②残留 + 评审 A-1）**后加**的，逐点表写在它之前 ⇒ 表里没有它。

两处都用表里**同模型已给的那条路径**，⛔ 没有自创第三条路径。
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    ReviewStatus,
    SettleStatus,
)
from tests.p5_auth_helpers import grant_games, make_logged_in_client
from tests.xlsx_helpers import DETAIL_SHEET, detail_rows, values_of, workbook_of

LIST_URL = "/api/business/performances/"
EXPORT_URL = "/api/business/performances/export"
ADJUST_URL = "/api/business/performances/{pk}/adjust/"
ADJUSTMENTS_URL = "/api/business/performances/{pk}/adjustments"
DELETE_URL = "/api/business/performances/delete"
SETTLE_URL = "/api/business/settlements/settle"
REVOKE_URL = "/api/business/settlements/revoke"
SETTLEMENTS_URL = "/api/business/settlements"
UNATTRIBUTED_URL = "/api/business/settlements/unattributed"
PEOPLE_URL = "/api/business/settlements/people"

#: 本文件实际走到的门（逐字面量；`groups` 只建 Group、**不放行**）。
#: 「按游戏收窄」和「不持 `order.list` ⇒ 只看自己的行」是两道并列的收窄。
#:    请求方必须持 `order.list`——否则「只看自己」会先把它自己名下没有的行全遮掉，
#:    本文件的判别力（「抽掉 `scope_filter` ⇒ B 游戏的行一起出现」）就退化成恒真/恒假。
#:    ⛔ 这是**夹具声明它要过的门**，不是放宽判据：B 游戏的行仍然被 `game_scope` 挡住。
NODES = (
    "my_performance.export",
    "finance.export_performance",
    "order.submit_payout_price",
    "finance.payroll",
    "finance.change_settlement_status",
    "finance.delete_performance",
    "order.list",
)

#: 租户内唯一的订单号（字面量，20 位）。A / B 分属两款游戏。
ORDER_A_NO = "20260923000000000201"
ORDER_B_NO = "20260923000000000202"
#: 删除用例专用的两张单（各自一条可删的 SALES 绩效）。
DELETE_A_NO = "20260923000000000203"
DELETE_B_NO = "20260923000000000204"

A_ONLY_USERNAME = "scope-perf-a"
ADMIN_USERNAME = "scope-perf-admin"
EMPTY_USERNAME = "scope-perf-empty"

#: 调整记录的 `reason` 是租户内唯一的业务值，用作正/负断言的字面锚点。
ADJ_A_REASON = "A 链的调整"
ADJ_B_REASON = "B 链的调整"


class PerformanceScopeTests(TenantTestCase):
    """两款游戏 + 各一条绩效/调整记录 + 三个账号（只勾 A / 租户管理员 / 有档案零游戏）。"""

    def setUp(self):
        self.game_a = Game.objects.create(name="原神")
        self.game_b = Game.objects.create(name="崩铁")
        self.taker_a = User.objects.create_user(username="scope-perf-taker-a")
        self.taker_b = User.objects.create_user(username="scope-perf-taker-b")

        self.order_a = self._order(ORDER_A_NO, self.game_a)
        self.order_b = self._order(ORDER_B_NO, self.game_b)
        self.perf_a = self._perf(self.order_a, self.taker_a)
        self.perf_b = self._perf(self.order_b, self.taker_b)
        self.adj_a = self._adjustment(self.perf_a, "5.00", ADJ_A_REASON)
        self.adj_b = self._adjustment(self.perf_b, "7.00", ADJ_B_REASON)

        # ① 只勾 A 游戏的账号（有档案 + 有游戏）。
        # ⚠️ `games=[]` 是本用例的**显式 opt-out**（夹具默认翻面）：本用例要的是
        #    **部分权限**形状。夹具只建空档案、不预先授权，权限的唯一来源是下面那句
        #    `grant_games(self.a_only_user, self.game_a)`（⚠️ 它是**并集**语义
        #    ——夹具若按新默认先给全部游戏，这里就并不出「只有 A」，本文件
        #    「B 的行不可见」那一整组断言会**静默变成恒真**）。
        self.a_only = make_logged_in_client(
            username=A_ONLY_USERNAME, groups=("客服",), nodes=NODES, games=[]
        )
        self.a_only_user = User.objects.get(username=A_ONLY_USERNAME)
        grant_games(self.a_only_user, self.game_a)

        # ② 租户管理员（`is_superuser` + 夹具顺手挂的「系统管理员」职位）：
        #    `scope_queryset` 对管理员短路 ⇒ 读路径不该被收窄。该账号**没有档案**
        #    （`games=None` ⇒ 夹具不建 `StaffProfile`），正是供给出来的租户 `admin` 的形状。
        self.admin = make_logged_in_client(
            username=ADMIN_USERNAME, is_superuser=True, games=None
        )
        self.admin_user = User.objects.get(username=ADMIN_USERNAME)

        # ③「有档案、零游戏」的非管理员（严格口径的反面）。
        #    `games=[]` = 建档案但**一款都不勾**（夹具的语义 3）。
        self.empty = make_logged_in_client(
            username=EMPTY_USERNAME, groups=("客服",), nodes=NODES, games=[]
        )
        self.empty_user = User.objects.get(username=EMPTY_USERNAME)
        # ⛔ 这一行**不动**（既有 `grant_games(...)` 调用一处不删）：它在本分支落地时是
        #    「只建档案、不勾任何游戏」那条夹具；现在夹具的 `games=[]` 已把档案建好，
        #    这里退化为**幂等重申**（`get_or_create` + 不传 games ⇒ 一行不改），
        #    留着是为了不改既有声明、也不让上面的 opt-out 变成"顺手删依赖"。
        grant_games(self.empty_user)  # 只建档案、不勾任何游戏

    # ── 小夹具 ────────────────────────────────────────────────────────
    def _order(self, order_no, game, *, status=OrderStatus.COMPLETED):
        return Order.objects.create(
            game=game,
            order_no=order_no,
            status=status,
            total_price=Decimal("100.00"),
            received_price=Decimal("100.00"),
            project_text="占位项目",
        )

    def _perf(
        self,
        order,
        user,
        *,
        emp_type=EmpType.MAIN,
        settle=SettleStatus.UNSETTLED,
        bonus="20.00",
        temp="20.00",
    ):
        return Performance.objects.create(
            order=order,
            user=user,
            emp_type=emp_type,
            temp_amount=Decimal(temp),
            bonus_amount=Decimal(bonus),
            commission_rate=Decimal("10.00"),
            deduct_mode="",
            deduct_value=Decimal("0.00"),
            review_status=ReviewStatus.APPROVED,
            settle_status=settle,
        )

    def _adjustment(self, performance, amount, reason, *, settle=SettleStatus.UNSETTLED):
        return PerformanceAdjustment.objects.create(
            performance=performance,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal(amount),
            reason=reason,
            operator="scope-fixture",
            settle_status=settle,
        )

    def _settled_row(self, game, *, order_no, user, reason):
        """一张「已结账」的绩效 + 一条「同一轮」已结账的调整记录（撤销结账的合格形状）。

        ⚠️ 两条的 `settled_at` **必须逐字节相同**：`revoke_settlements` 的「同一轮」判据
        是 `(performance_id, settled_at)` 配对（见该函数的实现）。
        """
        order = self._order(order_no, game)
        perf = self._perf(order, user, settle=SettleStatus.SETTLED)
        perf.settled_at = timezone.now()
        perf.save(update_fields=["settled_at"])
        adj = self._adjustment(perf, "5.00", reason, settle=SettleStatus.SETTLED)
        adj.settled_at = perf.settled_at
        adj.save(update_fields=["settled_at"])
        return perf, adj

    def _post(self, client, url, payload):
        return client.post(url, payload, content_type="application/json")

    # ── 构造物哨兵（「空游戏」这个形状真的被造出来了）────────────────────
    def test_the_empty_games_account_has_a_profile_with_no_games(self):
        """③ 的形状哨兵：**有档案**且 `games` 为空（不是「没造出档案」这种别的原因）。"""
        self.assertEqual(StaffProfile.objects.filter(user=self.empty_user).count(), 1)
        self.assertEqual(
            list(
                StaffProfile.objects.get(user=self.empty_user).games.values_list(
                    "pk", flat=True
                )
            ),
            [],
        )
        # 反面：只勾 A 的那个账号**没有**落进这个形状（否则本文件全部用例都在测同一件事）。
        self.assertEqual(
            list(
                StaffProfile.objects.get(user=self.a_only_user).games.values_list(
                    "pk", flat=True
                )
            ),
            [self.game_a.pk],
        )

    # ── ① 绩效列表（performance_queryset：list 与 export 共用）──────────
    def test_performance_list_keeps_only_my_games_rows(self):
        """只有 A 权限 ⇒ 列表里只有 A 游戏的绩效行；B 的不在结果里。

        判别力：抽掉 `performance_queryset` 的 `scope_filter` ⇒ B 的行一起出现，本用例红。
        """
        body = self.a_only.get(LIST_URL).json()
        self.assertEqual(body["code"], 0, body)
        # 前置哨兵：B 游戏那条绩效真的在库里（不是「没造出来」）。
        self.assertEqual(
            Performance.objects.filter(order__order_no=ORDER_B_NO).count(), 1
        )
        nos = {row["order_no"] for row in body["data"]["items"]}
        self.assertEqual(nos, {ORDER_A_NO})
        self.assertNotIn(ORDER_B_NO, nos)

    def test_performance_list_is_empty_for_a_profile_with_no_games(self):
        """零游戏 ⇒ `items` 为空且 `total` 为 0（严格口径，字面值）。"""
        body = self.empty.get(LIST_URL).json()
        self.assertEqual(body["code"], 0, body)
        # 前置哨兵：库里确实有两行（不是「本来就没数据」）。
        self.assertEqual(Performance.objects.count(), 2)
        self.assertEqual(body["data"]["items"], [])
        self.assertEqual(body["data"]["total"], 0)

    def test_performance_list_shows_every_game_to_a_tenant_admin(self):
        """管理员旁路在 `scope_queryset` 里 ⇒ 两张单的绩效行都看得到。"""
        body = self.admin.get(LIST_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            {row["order_no"] for row in body["data"]["items"]},
            {ORDER_A_NO, ORDER_B_NO},
        )

    # ── ② 绩效导出（同一个 performance_queryset；不接它 = 导出绕过收窄）──
    # 导出是 `.xlsx`，这三条必须按单元格值判。对 `response.content` 做文本解码会 `UnicodeDecodeError`。
    def test_export_contains_only_my_games_rows(self):
        """导出的明细里只有 A 的订单号；B 的不在（否则「列表看不到、导得出」）。"""
        values = values_of(workbook_of(self.a_only.get(EXPORT_URL)), DETAIL_SHEET)
        self.assertIn(ORDER_A_NO, values)
        self.assertNotIn(ORDER_B_NO, values)

    def test_export_is_only_a_header_for_a_profile_with_no_games(self):
        """零游戏 ⇒ 明细只有表头一行（空表格，不是全量导出）。"""
        rows = detail_rows(workbook_of(self.empty.get(EXPORT_URL)))
        self.assertEqual(len(rows), 1)

    def test_export_contains_every_game_for_a_tenant_admin(self):
        values = values_of(workbook_of(self.admin.get(EXPORT_URL)), DETAIL_SHEET)
        self.assertIn(ORDER_A_NO, values)
        self.assertIn(ORDER_B_NO, values)

    # ── ③ 详情动作（`_base_queryset`：/adjust 走 get_object 的同一条 queryset）──
    def test_adjust_of_another_games_performance_looks_nonexistent(self):
        """B 游戏的绩效对只勾 A 的人不可见 ⇒ `get_object()` 找不到 ⇒ 200 + `1002`。"""
        before = Performance.objects.get(pk=self.perf_b.pk).deduct_value
        body = self._post(
            self.a_only,
            ADJUST_URL.format(pk=self.perf_b.pk),
            {"deduct_mode": "1", "deduct_value": "5.00"},
        ).json()
        self.assertEqual(body["code"], 1002, body)
        self.assertIsNone(body["data"])
        # 前置哨兵 + 断言「真的没改」：B 那行仍在库里，扣点一个字没动。
        self.assertEqual(Performance.objects.filter(pk=self.perf_b.pk).count(), 1)
        refreshed = Performance.objects.get(pk=self.perf_b.pk)
        self.assertEqual(refreshed.deduct_value, before)
        self.assertEqual(refreshed.deduct_mode, "")

    def test_adjust_of_my_games_performance_works(self):
        """反例：自己的游戏 ⇒ `/adjust` **成功**（证明不是「一律拒绝」）。"""
        body = self._post(
            self.a_only,
            ADJUST_URL.format(pk=self.perf_a.pk),
            {"deduct_mode": "1", "deduct_value": "5.00"},
        ).json()
        self.assertEqual(body["code"], 0, body)
        refreshed = Performance.objects.get(pk=self.perf_a.pk)
        self.assertEqual(refreshed.deduct_mode, "1")
        self.assertEqual(refreshed.deduct_value, Decimal("5.00"))

    def test_adjust_is_unreachable_for_a_profile_with_no_games(self):
        """零游戏 ⇒ 连自己的（其实谁的都一样）绩效也 `1002`。"""
        body = self._post(
            self.empty,
            ADJUST_URL.format(pk=self.perf_a.pk),
            {"deduct_mode": "1", "deduct_value": "5.00"},
        ).json()
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(
            Performance.objects.get(pk=self.perf_a.pk).deduct_mode, ""
        )

    def test_adjust_of_another_games_performance_works_for_a_tenant_admin(self):
        body = self._post(
            self.admin,
            ADJUST_URL.format(pk=self.perf_b.pk),
            {"deduct_mode": "1", "deduct_value": "5.00"},
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            Performance.objects.get(pk=self.perf_b.pk).deduct_value, Decimal("5.00")
        )

    # ── ④ 调整记录链（GET：`adjustment_views.get_queryset`）──────────────
    def test_adjustment_list_hides_the_other_games_chain(self):
        """B 那条绩效的调整记录对只勾 A 的人**不可见**：`performance_id` 能猜、
        但那一条链返回空（用 `reason` 字面量判，不用主键）。"""
        hidden = self.a_only.get(ADJUSTMENTS_URL.format(pk=self.perf_b.pk)).json()
        self.assertEqual(hidden["code"], 0, hidden)
        self.assertEqual(hidden["data"]["items"], [])
        # 前置哨兵：B 那条调整记录真的在库里。
        self.assertEqual(
            PerformanceAdjustment.objects.filter(reason=ADJ_B_REASON).count(), 1
        )
        # 反面（正例）：自己那条链看得到。
        visible = self.a_only.get(ADJUSTMENTS_URL.format(pk=self.perf_a.pk)).json()
        self.assertEqual(
            [row["reason"] for row in visible["data"]["items"]], [ADJ_A_REASON]
        )

    def test_adjustment_list_is_empty_for_a_profile_with_no_games(self):
        body = self.empty.get(ADJUSTMENTS_URL.format(pk=self.perf_a.pk)).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(PerformanceAdjustment.objects.count(), 2)  # 前置哨兵
        self.assertEqual(body["data"]["items"], [])

    def test_adjustment_list_shows_every_game_to_a_tenant_admin(self):
        body = self.admin.get(ADJUSTMENTS_URL.format(pk=self.perf_b.pk)).json()
        self.assertEqual(
            [row["reason"] for row in body["data"]["items"]], [ADJ_B_REASON]
        )

    # ── ⑤ 人工修正前置（`adjustment_views.create` 取绩效那一次）──────────
    def test_manual_adjustment_on_another_games_performance_is_rejected(self):
        """对 B 游戏的绩效做人工修正 ⇒ `1000`，且**库里一行都没多**（E-0009）。"""
        before = PerformanceAdjustment.objects.count()
        self.assertEqual(before, 2)  # 前置哨兵
        body = self._post(
            self.a_only,
            ADJUSTMENTS_URL.format(pk=self.perf_b.pk),
            {"amount": "-3.00", "reason": "越权修正", "request_id": "scope-manual-b"},
        ).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIsNone(body["data"])
        self.assertEqual(PerformanceAdjustment.objects.count(), before)
        self.assertEqual(
            PerformanceAdjustment.objects.filter(reason="越权修正").count(), 0
        )

    def test_manual_adjustment_on_my_games_performance_succeeds(self):
        before = PerformanceAdjustment.objects.count()
        body = self._post(
            self.a_only,
            ADJUSTMENTS_URL.format(pk=self.perf_a.pk),
            {"amount": "-3.00", "reason": "自己的修正", "request_id": "scope-manual-a"},
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(PerformanceAdjustment.objects.count(), before + 1)
        self.assertEqual(
            PerformanceAdjustment.objects.filter(reason="自己的修正").count(), 1
        )

    def test_manual_adjustment_is_rejected_for_a_profile_with_no_games(self):
        before = PerformanceAdjustment.objects.count()
        body = self._post(
            self.empty,
            ADJUSTMENTS_URL.format(pk=self.perf_a.pk),
            {"amount": "-3.00", "reason": "零游戏修正", "request_id": "scope-manual-e"},
        ).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(PerformanceAdjustment.objects.count(), before)

    def test_manual_adjustment_on_another_games_performance_works_for_a_tenant_admin(self):
        body = self._post(
            self.admin,
            ADJUSTMENTS_URL.format(pk=self.perf_b.pk),
            {"amount": "-3.00", "reason": "管理员的修正", "request_id": "scope-manual-adm"},
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            PerformanceAdjustment.objects.filter(reason="管理员的修正").count(), 1
        )

    # ── ⑥ 结账（`settle_performances` 第 2 步两条腿 + `_reject_message`）──
    def test_settle_of_another_games_performance_is_rejected(self):
        """B 的绩效（未结账）⇒ `1006` + 「不存在或不可见」，且**两行都没被改动**。

        判别力（本任务点名的变异②）：抽掉 `settle_performances` 里**调整记录那条腿**的
        `scope_filter` ⇒ `adjustment_perf_ids` 会把 B 的绩效当成「合格」⇒ 整批不再被拒、
        B 的调整记录被静默结掉（response `code == 0`）⇒ 本用例红。
        """
        before = PerformanceAdjustment.objects.count()
        self.assertEqual(before, 2)  # 前置哨兵
        body = self._post(
            self.a_only, SETTLE_URL, {"performance_ids": [self.perf_b.pk]}
        ).json()
        self.assertEqual(body["code"], 1006, body)
        self.assertIn("不存在或不可见", body["msg"])
        self.assertEqual(
            Performance.objects.get(pk=self.perf_b.pk).settle_status,
            "未结账",
        )
        self.assertEqual(
            PerformanceAdjustment.objects.get(pk=self.adj_b.pk).settle_status,
            "未结账",
        )

    def test_settle_of_another_games_already_settled_row_is_rejected_not_skipped(self):
        """⭐ **跳过不许吞掉越权**（2026-10-03 清算绩效弹窗线）：B 的绩效行**本来就是已结账**
        （且名下没有未结账调整）时，它在 A 账号的请求里**仍必须是 `1006`**，
        ⛔ **不能**因为它「已是目标状态」就被跳过去、返回成功。

        判别力：抽掉 `settled_ids_without_pending_adjustments` 的 `scope_queryset` +
        `game_scope` ⇒ 这条越权行会被算成「已完全结清」⇒ `code == 0` 且它出现在
        `skipped_performances` 里 ⇒ 本用例红（而这正是「跳过」这条新路径最容易犯的错：
        把「已是目标状态」判成「不看范围」）。
        """
        perf = self._perf(
            self._order("20260923000000000207", self.game_b),
            self.taker_b,
            settle=SettleStatus.SETTLED,
        )
        body = self._post(
            self.a_only, SETTLE_URL, {"performance_ids": [perf.pk]}
        ).json()
        self.assertEqual(body["code"], 1006, body)
        self.assertIn("不存在或不可见", body["msg"])
        self.assertEqual(
            Performance.objects.get(pk=perf.pk).settle_status, "已结账"
        )

    def test_revoke_of_another_games_unsettled_row_is_rejected_not_skipped(self):
        """镜像判据（撤销侧）：B 的绩效行**本来就是未结账**时，A 账号的撤销请求
        仍必须是 `1006`（⛔ 不能判成「已是目标状态」而跳过）。

        判别力：抽掉 `already_unsettled_ids` 的 `scope_queryset` + `game_scope` ⇒
        `code == 0` 且那行出现在 `skipped_performances` 里 ⇒ 本用例红。
        """
        perf = self.perf_b          # 夹具里就是「未结账 + 一条未结账调整」
        self.assertEqual(perf.settle_status, "未结账")     # 前置哨兵
        body = self._post(
            self.a_only,
            REVOKE_URL,
            {"performance_ids": [perf.pk], "reason": "越权撤销未结账行"},
        ).json()
        self.assertEqual(body["code"], 1006, body)
        self.assertIn("不存在或不可见", body["msg"])
        self.assertEqual(
            Performance.objects.get(pk=perf.pk).settle_status, "未结账"
        )

    def test_settle_of_my_games_performance_works(self):
        """反例：自己的游戏 ⇒ 结账**成功**，且库里两行都真的变了。"""
        body = self._post(
            self.a_only, SETTLE_URL, {"performance_ids": [self.perf_a.pk]}
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["settled_performances"], [self.perf_a.pk])
        self.assertEqual(body["data"]["settled_adjustments"], [self.adj_a.pk])
        self.assertEqual(
            Performance.objects.get(pk=self.perf_a.pk).settle_status,
            "已结账",
        )
        self.assertEqual(
            PerformanceAdjustment.objects.get(pk=self.adj_a.pk).settle_status,
            "已结账",
        )

    def test_settle_is_rejected_for_a_profile_with_no_games(self):
        body = self._post(
            self.empty, SETTLE_URL, {"performance_ids": [self.perf_a.pk]}
        ).json()
        self.assertEqual(body["code"], 1006, body)
        self.assertEqual(
            Performance.objects.get(pk=self.perf_a.pk).settle_status,
            "未结账",
        )

    def test_settle_of_another_games_performance_works_for_a_tenant_admin(self):
        body = self._post(
            self.admin, SETTLE_URL, {"performance_ids": [self.perf_b.pk]}
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            Performance.objects.get(pk=self.perf_b.pk).settle_status,
            "已结账",
        )

    # ── ⑦ 撤销结账（`_revoke_reject_message` + 两条腿）──────────────────
    def test_revoke_of_another_games_settled_performance_is_rejected(self):
        """B 的已结账绩效 ⇒ `1006` + 「不存在或不可见」；两行都仍是「已结账」。"""
        perf, adj = self._settled_row(
            self.game_b,
            order_no="20260923000000000205",
            user=self.taker_b,
            reason="B 已结账链",
        )
        body = self._post(
            self.a_only,
            REVOKE_URL,
            {"performance_ids": [perf.pk], "reason": "越权撤销"},
        ).json()
        self.assertEqual(body["code"], 1006, body)
        self.assertIn("不存在或不可见", body["msg"])
        self.assertEqual(
            Performance.objects.get(pk=perf.pk).settle_status, "已结账"
        )
        self.assertEqual(
            PerformanceAdjustment.objects.get(pk=adj.pk).settle_status,
            "已结账",
        )

    def test_revoke_of_my_games_settled_performance_works(self):
        perf, adj = self._settled_row(
            self.game_a,
            order_no="20260923000000000206",
            user=self.taker_a,
            reason="A 已结账链",
        )
        body = self._post(
            self.a_only,
            REVOKE_URL,
            {"performance_ids": [perf.pk], "reason": "自己的撤销"},
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["revoked_performances"], [perf.pk])
        self.assertEqual(body["data"]["revoked_adjustments"], [adj.pk])
        self.assertEqual(
            Performance.objects.get(pk=perf.pk).settle_status, "未结账"
        )
        self.assertEqual(
            PerformanceAdjustment.objects.get(pk=adj.pk).settle_status,
            "未结账",
        )

    def test_revoke_is_rejected_for_a_profile_with_no_games(self):
        perf, _adj = self._settled_row(
            self.game_a,
            order_no="20260923000000000207",
            user=self.taker_a,
            reason="零游戏链",
        )
        body = self._post(
            self.empty,
            REVOKE_URL,
            {"performance_ids": [perf.pk], "reason": "零游戏撤销"},
        ).json()
        self.assertEqual(body["code"], 1006, body)
        self.assertEqual(
            Performance.objects.get(pk=perf.pk).settle_status, "已结账"
        )

    def test_revoke_of_another_games_performance_works_for_a_tenant_admin(self):
        perf, _adj = self._settled_row(
            self.game_b,
            order_no="20260923000000000208",
            user=self.taker_b,
            reason="管理员撤销链",
        )
        body = self._post(
            self.admin,
            REVOKE_URL,
            {"performance_ids": [perf.pk], "reason": "管理员撤销"},
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            Performance.objects.get(pk=perf.pk).settle_status, "未结账"
        )

    # ── ⑧ 删除记录（`delete_performances` 取行 + 删除两条 queryset）──────
    def test_delete_of_another_games_performance_is_rejected(self):
        """B 的可删绩效 ⇒ `1014` + 「不存在或不可见」，且**那一行还在库里**。"""
        order = self._order(DELETE_B_NO, self.game_b)
        perf = self._perf(order, self.taker_b, emp_type=EmpType.SALES)
        body = self._post(
            self.a_only, DELETE_URL, {"performance_ids": [perf.pk]}
        ).json()
        self.assertEqual(body["code"], 1014, body)
        self.assertIn("不存在或不可见", body["msg"])
        self.assertEqual(Performance.objects.filter(pk=perf.pk).count(), 1)

    def test_delete_of_my_games_performance_works(self):
        order = self._order(DELETE_A_NO, self.game_a)
        perf = self._perf(order, self.taker_a, emp_type=EmpType.SALES)
        body = self._post(
            self.a_only, DELETE_URL, {"performance_ids": [perf.pk]}
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["deleted"], [perf.pk])
        self.assertEqual(Performance.objects.filter(pk=perf.pk).count(), 0)

    def test_delete_is_rejected_for_a_profile_with_no_games(self):
        order = self._order(DELETE_A_NO, self.game_a)
        perf = self._perf(order, self.taker_a, emp_type=EmpType.SALES)
        body = self._post(self.empty, DELETE_URL, {"performance_ids": [perf.pk]}).json()
        self.assertEqual(body["code"], 1014, body)
        self.assertEqual(Performance.objects.filter(pk=perf.pk).count(), 1)

    def test_delete_of_another_games_performance_works_for_a_tenant_admin(self):
        order = self._order(DELETE_B_NO, self.game_b)
        perf = self._perf(order, self.taker_b, emp_type=EmpType.SALES)
        body = self._post(
            self.admin, DELETE_URL, {"performance_ids": [perf.pk]}
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(Performance.objects.filter(pk=perf.pk).count(), 0)

    # ── ⑨ 结算域的只读接口（结算列表 / 待归属 / 结账对象人员）────────────
    def test_settlement_list_keeps_only_my_games_rows(self):
        body = self.a_only.get(SETTLEMENTS_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(Order.objects.filter(status=OrderStatus.COMPLETED).count(), 2)
        self.assertEqual(
            {row["order_no"] for row in body["data"]["items"]}, {ORDER_A_NO}
        )

    def test_settlement_list_is_empty_for_a_profile_with_no_games(self):
        body = self.empty.get(SETTLEMENTS_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["items"], [])

    def test_settlement_list_shows_every_game_to_a_tenant_admin(self):
        body = self.admin.get(SETTLEMENTS_URL).json()
        self.assertEqual(
            {row["order_no"] for row in body["data"]["items"]},
            {ORDER_A_NO, ORDER_B_NO},
        )

    def test_unattributed_hides_another_games_orders(self):
        """待归属清单是**按订单的游戏**收窄（`Order` ⇒ `game_id`）。"""
        body = self.a_only.get(UNATTRIBUTED_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(Order.objects.filter(status=OrderStatus.COMPLETED).count(), 2)
        self.assertEqual(
            {row["order_no"] for row in body["data"]["items"]}, {ORDER_A_NO}
        )

    def test_unattributed_is_empty_for_a_profile_with_no_games(self):
        body = self.empty.get(UNATTRIBUTED_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["items"], [])

    def test_settlement_people_hides_another_games_people(self):
        """结账对象人员 = 结算数据里出现过的人，按**看数据的人**的游戏权限收窄。"""
        body = self.a_only.get(PEOPLE_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            [row["username"] for row in body["data"]["items"]],
            ["scope-perf-taker-a"],
        )

    def test_settlement_people_is_empty_for_a_profile_with_no_games(self):
        body = self.empty.get(PEOPLE_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["items"], [])
