"""P4FU-B：绩效页两颗写按钮 + 一颗导出（D-0072 / brief §1 A·B·C）。

覆盖三块：

1. **撤销结账**（`POST /api/business/settlements/revoke`）：正向回退、**同一轮**调整一起回退、
   后来「只结调整」轮次**不回退**、逐 id fail-closed、条件 `update()` rowcount 并发守门、
   留痕打**数据库行**（E-0009）。
2. **删除记录**（`POST /api/business/performances/delete`）：已结账不许删（三字段并集判据）、
   MAIN 行一律不可删（理由见 `performance_writes` 的模块 docstring：状态机**没有终态**）、
   有调整记录 ⇒ `ProtectedError` 翻 **1001** 而不是 500、被删事实写进订单留痕、
   **删完不留下 500 路径**（走真实端点做「后续动作」对照）。
3. **导出记录**（`GET /api/business/performances/export`）：与列表同源、附件是 `.xlsx`
   （`绩效明细` / `绩效汇总`）、超上限报错不截断。逐列逐格在 `tests/test_performance_export_xlsx.py`。

外加：三颗按钮各一组「无节点 ⇒ 403 / 有节点 ⇒ 200」与跨租户隔离回归。

⚠️ **期望值一律写字面值**（E-0024）：业务码写 `1000` / `1006` / `1001` / `1014`，
状态写 `"已结账"` / `"未结账"`——不引被测模块导出的常量（自指断言不会红）。
⚠️ **留痕断言一律打 `OperationLog` 的数据库行**（E-0009）：本项目没有 `LOGGING` 配置，
`logger` 断言全绿而生产一行都不写。本文件不写任何日志断言。
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.db.models.query import QuerySet
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
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
from apps.business.performance import main_performance_of
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, make_logged_in_client
from tests.xlsx_helpers import detail_rows, values_of, workbook_of

#: 四条路径取自 brief（**都没有**尾斜杠，与 `/settlements/settle` 同形）。
SETTLE_URL = "/api/business/settlements/settle"
REVOKE_URL = "/api/business/settlements/revoke"
DELETE_URL = "/api/business/performances/delete"
EXPORT_URL = "/api/business/performances/export"

#: 三个节点 code（写**字面值**，不引 catalog 常量）。
NODE_SETTLE = "finance.change_settlement_status"
NODE_DELETE = "finance.delete_performance"
NODE_EXPORT = "finance.export_performance"


class _PerfWriteFixture(TenantTestCase):
    """建单 / 建绩效的小夹具（各用例只表达**自己的差异**）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # 管理员登录：`has_node_now` / `has_node` 都对他短路（`is_superuser`）
        # ⇒ 业务逻辑用例不必先搭角色目录。
        self.finance = make_logged_in_client(username="p4fu-fin", is_superuser=True)
        self.taker_a = User.objects.create_user(username="p4fu-taker-a")
        self.taker_b = User.objects.create_user(username="p4fu-taker-b")
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=无游戏=拒）
        # ⇒ 走「删 MAIN 行后 /assign/ 补行」那类用例的打手要持本类的游戏。
        grant_games(self.taker_a, self.game)
        grant_games(self.taker_b, self.game)
        self._seq = 0

    def _order(self, *, status=OrderStatus.COMPLETED, hall_price="100.00"):
        self._seq += 1
        # 20 位订单号（`Order.order_no` 的列宽）。
        return Order.objects.create(
            game=self.game,
            order_no=f"2026092200000{self._seq:07d}",
            status=status,
            hall_price=Decimal(hall_price),
        )

    def _perf(self, order, *, user=None, emp_type=EmpType.MAIN, **overrides):
        fields = {
            "temp_amount": Decimal("20.00"),
            "bonus_amount": Decimal("20.00"),
            "deduct_mode": "",
            "deduct_value": Decimal("0.00"),
            # ⚠️ `performance_review_needs_user` 这条 CHECK 约束要求「没有归属人 ⇒ 没有审核状态」
            #    （NULL 而不是空串）——默认值必须跟着 `user` 走，否则建行直接撞约束。
            "review_status": (
                ReviewStatus.APPROVED if user is not None else None
            ),
            "settle_status": SettleStatus.UNSETTLED,
        }
        fields.update(overrides)
        return Performance.objects.create(
            order=order, user=user, emp_type=emp_type, **fields
        )

    def _adj(self, performance, amount):
        return PerformanceAdjustment.objects.create(
            performance=performance,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal(amount),
            reason="测试调整",
            operator="fin",
            settle_status=SettleStatus.UNSETTLED,
        )

    def _post(self, url, payload, *, client=None):
        return (client or self.finance).post(
            url, data=json.dumps(payload), content_type="application/json"
        )

    def _settle(self, ids, *, client=None, reason=""):
        response = self._post(
            SETTLE_URL, {"performance_ids": list(ids), "reason": reason}, client=client
        )
        self.assertEqual(response.json()["code"], 0, response.json())
        return response

    def _revoke(self, ids, *, reason="撤销原因", client=None):
        return self._post(
            REVOKE_URL, {"performance_ids": list(ids), "reason": reason}, client=client
        )

    def _delete(self, ids, *, client=None):
        return self._post(DELETE_URL, {"performance_ids": list(ids)}, client=client)


class RevokeTests(_PerfWriteFixture):
    """撤销结账：正向 / 边界 / fail-closed / 并发守门 / 入参。"""

    def test_revoke_returns_the_row_and_its_same_round_adjustment_to_unsettled(self):
        order = self._order()
        perf = self._perf(order, user=self.taker_a)
        adjustment = self._adj(perf, "-5.00")
        self._settle([perf.id])

        perf.refresh_from_db()
        adjustment.refresh_from_db()
        # 构造自证：结账那一刻写下的快照与「同一轮」的时间戳都在
        self.assertEqual(perf.settle_status, "已结账")
        self.assertEqual(Decimal(perf.settled_amount), Decimal("15.00"))
        self.assertEqual(adjustment.settle_status, "已结账")
        self.assertEqual(adjustment.settled_at, perf.settled_at)

        response = self._revoke([perf.id], reason="对账有误，撤销")
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["revoked_performances"], [perf.id])
        self.assertEqual(body["data"]["revoked_adjustments"], [adjustment.id])
        # ⛔ 反面对照：这一次真的撤了东西 ⇒ `skipped_performances` 必须是空的
        #    （把「凡是 rows 没命中的都算跳过」写成实现时，本行会红）。
        self.assertEqual(body["data"]["skipped_performances"], [])

        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertIsNone(perf.settled_at)
        self.assertEqual(perf.settled_by, "")
        self.assertIsNone(perf.settled_amount)
        self.assertEqual(adjustment.settle_status, "未结账")
        self.assertIsNone(adjustment.settled_at)

        # 留痕打在**数据库行**上（E-0009）：两个方向**对称**，各写两行都带这个
        # `performance_id`（绩效行一行 + 那条调整记录一行）。
        # ⭐ Task L：留痕已从旧 `AuditLog` 迁到 `OperationLog`
        #    （唯一写入出口 `record()`），动作 code 是 `performance.settle`。
        #    `OperationLog` **没有 `performance_id` 列** ⇒ 身份按 R12 落在
        #    `target_kind="user"` + 绩效归属人 id 上（`uniq_performance_order_user`
        #    保证「订单 × 人」唯一），与正向结账**完全同形**。
        lines = OperationLog.objects.filter(
            action="performance.settle", target_kind="user",
            target_id=perf.user_id, order_no=order.order_no,
        ).order_by("id")
        self.assertEqual(
            lines.count(), 4,
            list(lines.values("from_value", "to_value", "reason")),
        )
        revoke_lines = lines.filter(from_value="已结账", to_value="未结账")
        self.assertEqual(
            revoke_lines.count(), 2,
            "撤销留痕：绩效一行 + 被回退的调整记录一行（与正向结账对称）",
        )
        self.assertEqual(
            set(revoke_lines.values_list("reason", flat=True)), {"对账有误，撤销"}
        )
        revoke_line = revoke_lines.order_by("id").first()
        self.assertEqual(revoke_line.actor, "p4fu-fin")
        self.assertEqual(revoke_line.order_no, order.order_no)

    def test_revoke_writes_one_audit_line_per_revoked_adjustment(self):
        """与正向结账**对称**：每条被回退的调整记录也各写一行（评审 Minor，2026-09-22）。

        正向结账对「每条被结的调整记录」写一行（`settlement.py` 第 6 步），撤销原来只给
        绩效行写 ⇒ 「这条调整被回退过」逐条不可追溯。本用例断言**数据库行数**
        （E-0009：别断言 logger）；期望值写**字面整数**（E-0024：不引被测常量）。

        构造：1 条绩效 + 2 条同轮调整 ⇒ 正向 3 行（1+2）、反向也必须是 3 行（1+2）。
        """
        order = self._order()
        perf = self._perf(order, user=self.taker_a)
        first = self._adj(perf, "-2.00")
        second = self._adj(perf, "-3.00")
        self._settle([perf.id])

        settle_lines = OperationLog.objects.filter(
            action="performance.settle", target_kind="user",
            target_id=perf.user_id, order_no=order.order_no,
            from_value="未结账", to_value="已结账",
        )
        self.assertEqual(settle_lines.count(), 3, "前置：正向结账是逐条留痕的")

        body = self._revoke([perf.id], reason="逐条留痕").json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            sorted(body["data"]["revoked_adjustments"]), sorted([first.id, second.id])
        )

        revoke_lines = OperationLog.objects.filter(
            action="performance.settle", target_kind="user",
            target_id=perf.user_id, order_no=order.order_no,
            from_value="已结账", to_value="未结账",
        )
        self.assertEqual(
            revoke_lines.count(), 3,
            "撤销必须与正向结账对称：绩效 1 行 + 被回退的调整 2 行",
        )
        self.assertEqual(
            set(revoke_lines.values_list("reason", flat=True)), {"逐条留痕"}
        )

    def test_revoke_audit_row_names_the_owner_by_account_name(self):
        """⭐ 2026-10-17（订单日志人名线 spec §3-A2 / 判据 5）：撤销结账那两处 `record` 的人名
        是**账号名称**（昵称），⛔ 不是登录账号。

        ⚠️ 自建一个带昵称的归属人（⛔ 不改共享夹具）：两向断言（昵称在 + 账号不在），
        只断前者时「两个都写进去」也能绿。
        """
        owner = User.objects.create_user(
            username="p4fu-nick-taker", first_name="测试昵称打手丑"
        )
        order = self._order()
        perf = self._perf(order, user=owner)
        self._settle([perf.id])

        body = self._revoke([perf.id], reason="昵称撤销").json()
        self.assertEqual(body["code"], 0, body)

        rows = list(
            OperationLog.objects.filter(
                action="performance.settle", target_id=perf.user_id,
                order_no=order.order_no, from_value="已结账", to_value="未结账",
            )
        )
        self.assertEqual(len(rows), 1, [r.summary for r in rows])
        self.assertEqual(rows[0].target_name, "测试昵称打手丑")
        self.assertNotEqual(rows[0].target_name, "p4fu-nick-taker")
        self.assertIn("测试昵称打手丑", rows[0].summary)
        self.assertNotIn("p4fu-nick-taker", rows[0].summary)

    def test_revoke_leaves_a_later_adjustments_only_round_settled(self):
        """**有意的边界**：后来「只结调整」的那一轮（`settled_at` 更晚）不回退。"""
        order = self._order()
        perf = self._perf(order, user=self.taker_a)
        first_round = self._adj(perf, "-2.00")
        self._settle([perf.id])

        later_round = self._adj(perf, "-3.00")
        self._settle([perf.id])          # 绩效行已结账 ⇒ 这一轮只结调整记录

        perf.refresh_from_db()
        first_round.refresh_from_db()
        later_round.refresh_from_db()
        # 构造自证：两轮的 `settled_at` 真的不同（否则下面的边界断言无意义）
        self.assertEqual(first_round.settled_at, perf.settled_at)
        self.assertNotEqual(later_round.settled_at, perf.settled_at)

        body = self._revoke([perf.id], reason="只撤第一轮").json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["revoked_adjustments"], [first_round.id])
        self.assertNotIn(later_round.id, body["data"]["revoked_adjustments"])

        later_round.refresh_from_db()
        first_round.refresh_from_db()
        self.assertEqual(later_round.settle_status, "已结账")
        self.assertIsNotNone(later_round.settled_at)
        self.assertEqual(first_round.settle_status, "未结账")

    def test_a_row_already_unsettled_is_skipped_and_the_rest_still_revokes(self):
        """Q3=A（2026-10-03）：混一条**本来就是未结账**的行 ⇒ 只撤已结账那条，
        未结账那条进 `skipped_performances`（**不是**整批 `1006`）。

        ⚠️ **改前 → 改后**：原用例叫 `test_revoke_is_fail_closed_and_leaves_nothing_behind`，
        把「未结账的行混进来」与「不存在的 id」两件事**放在同一条用例里**、都期望 `1006`。
        业务方裁定 Q3=A（「勾选未结算或者已结算的时候，如果勾选的订单已经是
        要更改的状态 直接跳过即可 不用报错」）⇒ 两件事现在**必须分开测**：
        前者是成功路径（本用例），后者仍是整批失败（下面 `test_revoke_is_fail_closed_for_a_missing_id`）。
        判据一个字没放宽：被撤的行真的回退了、被跳过那行的库内状态与 `updated_at` 都不动、
        审计只给真被撤的那一条写。
        """
        order = self._order()
        settled = self._perf(order, user=self.taker_a)
        other_order = self._order()
        unsettled = self._perf(other_order, user=self.taker_b)
        self._settle([settled.id])
        audit_before = OperationLog.objects.filter(action="performance.settle").count()
        unsettled.refresh_from_db()
        untouched_at = unsettled.updated_at

        response = self._revoke([settled.id, unsettled.id], reason="混了一条未结账的")
        body = response.json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["revoked_performances"], [settled.id])
        self.assertEqual(body["data"]["skipped_performances"], [unsettled.id])

        settled.refresh_from_db()
        unsettled.refresh_from_db()
        self.assertEqual(settled.settle_status, "未结账")           # 真的撤了
        self.assertEqual(unsettled.settle_status, "未结账")         # 本来就是这个状态
        self.assertEqual(unsettled.updated_at, untouched_at)        # 一个字节都没动
        self.assertEqual(
            OperationLog.objects.filter(action="performance.settle").count(),
            audit_before + 1,
            "只该给真被撤的那一条写一行留痕（跳过的不写）",
        )

    def test_revoke_is_fail_closed_for_a_missing_id(self):
        """真不合格（id 不存在）⇒ 照旧整批 `1006` + 一行不写（Q3=A **不放宽**这一半）。

        ⚠️ 与上面那条分开是**承重**的：把两条混在一起（都期望 `1006`）时，
        实现只要把「凡是 rows 没命中的 id」一律拒绝就能同时满足两边，
        而「已是目标状态 ⇒ 跳过」这条新行为就**测不出来**。
        """
        order = self._order()
        settled = self._perf(order, user=self.taker_a)
        self._settle([settled.id])
        audit_before = OperationLog.objects.filter(action="performance.settle").count()

        missing = self._revoke([settled.id, 987654321], reason="不存在的 id").json()

        self.assertEqual(missing["code"], 1006, missing)
        self.assertIn("不存在或不可见", missing["msg"])
        self.assertIsNone(missing["data"], missing)
        settled.refresh_from_db()
        self.assertEqual(settled.settle_status, "已结账")           # 整批回滚
        self.assertEqual(
            OperationLog.objects.filter(action="performance.settle").count(),
            audit_before,
            "整批回滚的请求不该留下任何审计行",
        )

    def test_a_revoke_batch_of_unsettled_rows_is_skipped_whole(self):
        """§3 T2.2 判据 ④ 的第一半：**全批都是未结账** ⇒ `code=0`、全部进
        `skipped_performances`、库内一行未动、零新审计。"""
        first = self._perf(self._order(), user=self.taker_a)
        second = self._perf(self._order(), user=self.taker_b)
        audit_before = OperationLog.objects.filter(action="performance.settle").count()

        body = self._revoke([first.id, second.id], reason="都已经是未结账了").json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["revoked_performances"], [])
        self.assertEqual(body["data"]["revoked_adjustments"], [])
        self.assertEqual(
            sorted(body["data"]["skipped_performances"]), sorted([first.id, second.id])
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.settle_status, "未结账")
        self.assertEqual(second.settle_status, "未结账")
        self.assertEqual(
            OperationLog.objects.filter(action="performance.settle").count(),
            audit_before,
        )

    def test_revoke_rolls_back_when_the_conditional_update_reports_no_rows(self):
        """E-0022 同族的并发守门：条件 `update()` 报 0 行 ⇒ `1006` + 整批回滚 + 零新留痕。

        ⚠️ 用**确定性桩**复现（`QuerySet.update` 对 `Performance` 谎报 0 行），不依赖真并发；
        桩**自证接上**（`calls` 里必须出现过 `Performance`）。
        """
        order = self._order()
        perf = self._perf(order, user=self.taker_a)
        self._settle([perf.id])
        audit_before = OperationLog.objects.filter(action="performance.settle").count()

        calls = []
        real_update = QuerySet.update

        def fake_update(queryset, **kwargs):
            calls.append(queryset.model)
            if queryset.model is Performance:
                return 0
            return real_update(queryset, **kwargs)

        with mock.patch.object(QuerySet, "update", fake_update):
            body = self._revoke([perf.id], reason="并发守门").json()

        self.assertIn(Performance, calls, "桩没接上：没有任何一次 update 命中 Performance")
        self.assertEqual(body["code"], 1006, body)
        self.assertIn("已被改动", body["msg"])

        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")
        self.assertIsNotNone(perf.settled_at)
        self.assertEqual(
            OperationLog.objects.filter(action="performance.settle").count(), audit_before
        )

    def test_revoke_requires_a_non_empty_reason(self):
        """D-0072：撤销**必须填原因**（缺 / 空串 / 全空白 / 超长 都拒成 `1000`）。"""
        order = self._order()
        perf = self._perf(order, user=self.taker_a)
        self._settle([perf.id])

        cases = [
            ("缺 reason", {"performance_ids": [perf.id]}),
            ("空串 reason", {"performance_ids": [perf.id], "reason": ""}),
            ("全空白 reason", {"performance_ids": [perf.id], "reason": "   "}),
            ("超长 reason", {"performance_ids": [perf.id], "reason": "z" * 300}),
            ("空列表", {"performance_ids": [], "reason": "有原因但没勾行"}),
        ]
        for label, payload in cases:
            with self.subTest(case=label):
                response = self._post(REVOKE_URL, payload)
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertEqual(response.json()["code"], 1000, response.json())

        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")


class DeleteTests(_PerfWriteFixture):
    """删除记录：正向 / 已结账 / 三字段并集 / MAIN / PROTECT / fail-closed / 不留 500。"""

    def test_delete_removes_an_unsettled_sales_row_and_writes_a_db_audit_line(self):
        order = self._order()
        perf = self._perf(
            order, user=self.taker_b, emp_type=EmpType.SALES,
            bonus_amount=Decimal("8.00"),
        )
        response = self._delete([perf.id])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["deleted"], [perf.id])
        self.assertFalse(Performance.objects.filter(id=perf.id).exists())

        # ⚠️ 删行之后，这条审计行是**唯一**的历史线索（`target_id` 是**裸字符串主键**、
        #    不会随行级联删）。
        # ⭐ Task L：动作是对标自己的 `performance.delete`（逐字照抄那句），身份落在
        #    `target_kind="performance"` + `target_id` = **绩效主键**（对标句里有「对应绩效ID」）。
        line = OperationLog.objects.get(
            action="performance.delete", target_kind="performance",
            target_id=str(perf.id),
        )
        self.assertEqual(line.order_no, order.order_no)
        self.assertEqual(line.actor, "p4fu-fin")
        self.assertEqual(line.target_name, "p4fu-taker-b")
        # 预计绩效 / 生效绩效（`temp_amount` / `bonus_amount`）→ `from_value` / `to_value`；
        # 结账状态 → `reason`（纯值，标签由模板正文给，R10）。全是字面值（E-0024）。
        self.assertEqual(line.from_value, "20.00")
        self.assertEqual(line.to_value, "8.00")
        self.assertEqual(line.reason, "未结账")
        self.assertEqual(
            line.summary,
            "【p4fu-fin】删除绩效记录，该绩效用户：p4fu-taker-b，预计绩效金额：20.00元，"
            "生效绩效金额：8.00元，原结账状态：未结账，订单 {}，对应绩效ID：{}".format(
                order.order_no, perf.id
            ),
        )

    def test_delete_audit_row_names_the_owner_by_account_name(self):
        """⭐ 2026-10-17（订单日志人名线 spec §3-A2 / 判据 5）：删除绩效那条留痕的人名
        是**账号名称**（昵称），⛔ 不是登录账号。

        ⚠️ 删行之后这条审计行是**唯一**的历史线索 ⇒ 它显示成登录账号时，
        读日志的人根本不知道被删的是谁（本线要修的就是这个）。
        """
        owner = User.objects.create_user(
            username="p4fu-nick-sales", first_name="测试昵称打手寅"
        )
        order = self._order()
        perf = self._perf(
            order, user=owner, emp_type=EmpType.SALES, bonus_amount=Decimal("8.00")
        )
        body = self._delete([perf.id]).json()
        self.assertEqual(body["code"], 0, body)

        line = OperationLog.objects.get(
            action="performance.delete", target_kind="performance",
            target_id=str(perf.id),
        )
        self.assertEqual(line.target_name, "测试昵称打手寅")
        self.assertNotEqual(line.target_name, "p4fu-nick-sales")
        self.assertIn("测试昵称打手寅", line.summary)
        self.assertNotIn("p4fu-nick-sales", line.summary)
        self.assertEqual(
            line.summary,
            "【p4fu-fin】删除绩效记录，该绩效用户：测试昵称打手寅，预计绩效金额：20.00元，"
            "生效绩效金额：8.00元，原结账状态：未结账，订单 {}，对应绩效ID：{}".format(
                order.order_no, perf.id
            ),
        )

    def test_delete_refuses_a_settled_row_and_explains_per_id(self):
        order = self._order()
        settled = self._perf(
            order, user=self.taker_b, emp_type=EmpType.SALES,
            bonus_amount=Decimal("8.00"),
        )
        other_order = self._order()
        unsettled = self._perf(
            other_order, user=self.taker_a, emp_type=EmpType.SALES,
            bonus_amount=Decimal("7.00"),
        )
        self._settle([settled.id])

        body = self._delete([settled.id, unsettled.id]).json()
        self.assertEqual(body["code"], 1014, body)
        self.assertIn(str(settled.id), body["msg"])
        self.assertIn("已结账", body["msg"])
        # 整批拒：合格的那一条也不删
        self.assertTrue(Performance.objects.filter(id=settled.id).exists())
        self.assertTrue(Performance.objects.filter(id=unsettled.id).exists())

    def test_delete_treats_any_settlement_marker_as_settled(self):
        """已结账判据是 `settle_status` / `settled_at` / `settled_amount` 的**并集**。

        两个字段分别构造「只有一个标记」的行（存量行 / 状态被改回的脏行）——
        只看 `settle_status` 的实现会把它们当成未结账删掉，而那是对账凭据。
        """
        legacy_order = self._order()
        legacy = self._perf(legacy_order, user=self.taker_a, emp_type=EmpType.SALES)
        # 「已结账但没写 settled_at」：P4-FE-0 之前结的账就是这种形状。
        Performance.objects.filter(id=legacy.id).update(
            settle_status="已结账", settled_at=None, settled_amount=None
        )
        snapshot_order = self._order()
        snapshot_only = self._perf(
            snapshot_order, user=self.taker_b, emp_type=EmpType.SALES
        )
        # 「状态是未结账、却留着快照」的脏行。
        Performance.objects.filter(id=snapshot_only.id).update(
            settle_status="未结账", settled_at=None, settled_amount=Decimal("3.00")
        )

        for perf in (legacy, snapshot_only):
            with self.subTest(performance=perf.id):
                body = self._delete([perf.id]).json()
                self.assertEqual(body["code"], 1014, body)
                self.assertTrue(Performance.objects.filter(id=perf.id).exists())

    def test_delete_refuses_a_main_row_and_the_order_still_serves_later_actions(self):
        """MAIN 行一律不可删；**删完之后**那条订单的后续动作必须不 500（走真实端点）。

        对照组 ③ **改准**：此前它断言「绕过端点、真的把 MAIN 行删掉
        ⇒ 同一个真实端点 **500**」，用那条 500 证明「不许删 MAIN」是承重的。修复轮按业务方
        裁定（「**可以直接删掉无主绩效单**」）把 `/assign/` 与 `/submit/` 改成缺行**不抛**
        （前者补行、后者按既有 403 拒；完成钩子本来就是容错的）⇒ 那条 500 **不再存在**
        （`main_performance_of` 在产品码里已零调用方）⇒ 本对照改成钉**修复后的口径**：
        「绕过端点删掉 MAIN 行之后，`/assign/` 照常成功、并**补出一条新的**能用的行」。
        ⚠️ 「不许删 MAIN」的**其余**依据一个字没变（补行补不回上一个接单人的
        `taker_price` / `hours` / `bonus_amount` 记录 ⇒ 静默换身份），见
        `performance_writes` 模块 docstring 的那条更正块。
        """
        order = self._order(status=OrderStatus.UNASSIGNED)
        main = self._perf(order, user=None)
        body = self._delete([main.id]).json()
        self.assertEqual(body["code"], 1014, body)
        self.assertIn(str(main.id), body["msg"])
        self.assertIn("打单工资", body["msg"])
        self.assertTrue(Performance.objects.filter(id=main.id).exists())

        # ① 后续动作（真实端点）不 500，且真的走通了
        assign_url = f"/api/business/orders/{order.order_no}/assign/"
        ok = self._post(assign_url, {"user_id": self.taker_a.id})
        self.assertEqual(ok.status_code, 200, ok.content[:300])
        self.assertEqual(ok.json()["code"], 0, ok.json())
        order.refresh_from_db()
        self.assertEqual(order.status, "IN_PROGRESS")   # 落库的是枚举名（label 只在前端映射）
        self.assertEqual(main_performance_of(order).user_id, self.taker_a.id)

        # ② 状态无关：已完成的单，MAIN 行同样不可删
        completed_order = self._order(status=OrderStatus.COMPLETED)
        completed_main = self._perf(completed_order, user=self.taker_b)
        completed_body = self._delete([completed_main.id]).json()
        self.assertEqual(completed_body["code"], 1014, completed_body)
        self.assertTrue(
            Performance.objects.filter(id=completed_main.id).exists()
        )

        # ③ 对照：绕过端点真的删掉 MAIN 行 ⇒ `/assign/`
        #    **不再 500**，而是**补出一条新的** MAIN 行并照常完成指派。
        #    ⚠️ 判据要钉「**补的是另一条行**」：只断言「行存在」的话，「没删掉」也能绿。
        broken_order = self._order(status=OrderStatus.UNASSIGNED)
        broken_main = self._perf(broken_order, user=None)
        Performance.objects.filter(id=broken_main.id).delete()
        self.assertFalse(Performance.objects.filter(id=broken_main.id).exists())

        recovered = self._post(
            f"/api/business/orders/{broken_order.order_no}/assign/",
            {"user_id": self.taker_b.id},
        )
        self.assertEqual(recovered.status_code, 200, recovered.content[:300])
        self.assertEqual(recovered.json()["code"], 0, recovered.json())
        recovered_main = main_performance_of(broken_order)
        self.assertNotEqual(recovered_main.id, broken_main.id)   # ⛔ 补的是**另一条**行
        self.assertEqual(recovered_main.user_id, self.taker_b.id)
        broken_order.refresh_from_db()
        self.assertEqual(broken_order.status, "IN_PROGRESS")

    def test_a_hard_deleted_main_row_is_recreated_fresh_by_the_assign_backfill(self):
        """⭐ 2026-09-26 修复轮补（编排者要求）：**绕过端点硬删 MAIN 行**之后再指派给**另一个人**，
        `/assign/` 补出来的是一条**全新的行**，带的是**新接单人 B 的值**；A 那条行的 per-taker 列
        （`taker_price` / `hours` / `project` / `bonus_amount`）不会出现在新行上。

        ## 这条用例的**可证伪边界**（⛔ 如实登记，不假装「已经钉住了」）
        - ✅ **可证伪的那一半**：「补出来的行**不带**任何 per-taker 覆盖值」（`taker_price is None`）
          与「带的是 B 的 `hours` / `project`」。变异（只改生产、不动期望）：让补行顺手写一个覆盖价
          （`perf.taker_price = order.hall_price`）⇒ 本用例在 `taker_price is None` 上红。
          ⚠️ 这不是随便造的变异：本仓确实有「`/assign/` 的 `price` 写**订单级** `hall_price`、
          `/adjust/` 的 `taker_price` 写**每人一份**覆盖价」这对易混列。
        - ⛔ **不可证伪的那一半**：「A 的旧值**找不回来**」——`Model.delete()` 把唯一副本删掉了，
          这句话是**同义反复**（写不出任何能把它找回来的生产实现 ⇒ 也就写不出能红的变异）。
        - ⛔ **而且它不是「硬删」特有的代价**（下面 ⑤ 的对照是**实证**，不是读码断言）：
          **正常换人**（不删行、直接改派）走的 `reset_taker_performance` 会把**同一批列**
          一起回退 ⇒「A 的覆盖价 / 工时 / 生效绩效没了」在正常改派上同样成立
          （那条已有的判据在 `tests/test_order_assign_ext.py` 的换人/`R-2b` 一组用例里）。
        ⇒ 本用例的定位是**事实钉子 + 边界登记**：它给「硬删 vs 正常改派」留下一份可复核的
        **数据库读数**（`pk` / `created_at` / 各列的值），⛔ **不声称**「不许删 MAIN」这条守卫
        由此获得了一条可失败的判据；硬删**剩下**的真实差别是「**不可还原**、只剩审计线索」
        （见 `performance_writes` 模块 docstring 的更正块），那一条今天同样**没有**判据。
        """
        # ① 走真实端点：指派给 A，并给 A 一份**可辨识的** per-taker 记录。
        #    ⚠️ `taker_price` 只能走 `/adjust/`（每条绩效一份的覆盖价）；
        #    `/assign/` 的 `price` 写的是**订单级** `hall_price`，两列不是一回事。
        order = self._order(status=OrderStatus.UNASSIGNED)
        self._perf(order, user=None)                     # 「录单即建绩效」那条无主行
        assign_url = f"/api/business/orders/{order.order_no}/assign/"
        self.assertEqual(
            self._post(
                assign_url,
                {"user_id": self.taker_a.id, "hours": 12, "project": "A 的项目"},
            ).json()["code"],
            0,
        )
        row_a = main_performance_of(order)
        pk_a, created_a = row_a.pk, row_a.created_at
        self.assertEqual(
            self._post(
                f"/api/business/performances/{pk_a}/adjust/",
                {"taker_price": "88.00", "reason": "A 的覆盖价"},
            ).json()["code"],
            0,
        )
        # ⚠️ **R3 改准**：显式 `bonus_amount` 键在**非完成态**会被拒（`1014`）
        #    ⇒ 「A 的生效绩效」这一列改用**直接 ORM 写**来构造。本用例要的是
        #    「那一行带着**可辨识**的 per-taker 值」，⛔ 不是要测 `/adjust/` 的直写口
        #    （直写口的判据在 `tests/test_adjust_api.py` / `test_performance_adjust_amount_api.py`）。
        Performance.objects.filter(pk=pk_a).update(bonus_amount=Decimal("7.77"))
        row_a.refresh_from_db()
        # 哨兵：A 那条行真的带着**可辨识**的值（否则下面「这些值不在新行上」在空集上恒真）。
        self.assertEqual(row_a.user_id, self.taker_a.id)
        self.assertEqual(row_a.taker_price, Decimal("88.00"))
        self.assertEqual(row_a.hours, 12)
        self.assertEqual(row_a.project, "A 的项目")
        self.assertEqual(row_a.bonus_amount, Decimal("7.77"))

        # ② **绕过端点**硬删（模拟非法操作：真实端点 `POST /performances/delete` 会拒 MAIN 行）。
        Performance.objects.filter(pk=pk_a).delete()
        self.assertFalse(Performance.objects.filter(pk=pk_a).exists())

        # ③ 再指派给 B（带**B 的**工时 / 项目）。
        self.assertEqual(
            self._post(
                assign_url,
                {"user_id": self.taker_b.id, "hours": 24, "project": "B 的项目"},
            ).json()["code"],
            0,
        )

        # ④ 补出来的必须是**新行**（值断言：`pk` 与 `created_at` 都与被删那条不同）。
        row_b = main_performance_of(order)
        self.assertNotEqual(row_b.pk, pk_a)
        self.assertNotEqual(row_b.created_at, created_a)
        self.assertEqual(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).count(), 1
        )
        # 带的是 **B 的**值……
        self.assertEqual(row_b.user_id, self.taker_b.id)
        self.assertEqual(row_b.hours, 24)
        self.assertEqual(row_b.project, "B 的项目")
        self.assertIsNotNone(row_b.claimed_at)
        # ……而 A 的 per-taker 列一个都不在（`taker_price` 是那条**可证伪**的核心）。
        self.assertIsNone(row_b.taker_price)
        self.assertEqual(row_b.bonus_amount, Decimal("0.00"))

        # ⑤ 边界对照（**实证**「这不是硬删特有的代价」）：另起一张单，A → **直接改派** B
        #    （不删行）⇒ `reset_taker_performance` 把**同一批列**一起回退，行还是原来那条。
        control = self._order(status=OrderStatus.UNASSIGNED)
        self._perf(control, user=None)
        control_url = f"/api/business/orders/{control.order_no}/assign/"
        self.assertEqual(
            self._post(
                control_url,
                {"user_id": self.taker_a.id, "hours": 12, "project": "A 的项目"},
            ).json()["code"],
            0,
        )
        control_pk = main_performance_of(control).pk
        self.assertEqual(
            self._post(
                f"/api/business/performances/{control_pk}/adjust/",
                {"taker_price": "88.00", "reason": "A 的覆盖价"},
            ).json()["code"],
            0,
        )
        # ⚠️ 同上：非完成态不能用显式 `bonus_amount` 键 ⇒ 直接 ORM 写。
        Performance.objects.filter(pk=control_pk).update(bonus_amount=Decimal("7.77"))
        control_row = main_performance_of(control)
        # 哨兵：改派**之前**它确实带着 A 的那批值（否则下面「改派后没了」在空集上恒真）。
        self.assertEqual(control_row.taker_price, Decimal("88.00"))
        self.assertEqual(control_row.hours, 12)
        self.assertEqual(control_row.bonus_amount, Decimal("7.77"))
        self.assertEqual(
            self._post(
                control_url,
                {"user_id": self.taker_b.id, "hours": 24, "project": "B 的项目"},
            ).json()["code"],
            0,
        )
        control_row.refresh_from_db()
        self.assertEqual(control_row.pk, control_pk)        # ⛔ 没删过 ⇒ 还是同一行
        self.assertEqual(control_row.user_id, self.taker_b.id)
        self.assertEqual(control_row.hours, 24)
        self.assertEqual(control_row.project, "B 的项目")
        # 下面三条与 ④ 对**补出来的新行**的断言**逐条同形**：正常改派也照样清掉 A 的那批值。
        self.assertIsNone(control_row.taker_price)
        self.assertEqual(control_row.bonus_amount, Decimal("0.00"))
        self.assertEqual(control_row.project, "B 的项目")

    def test_delete_refuses_a_row_with_adjustments_with_the_conflict_code(self):
        order = self._order()
        perf = self._perf(order, user=self.taker_a, emp_type=EmpType.SALES)
        adjustment = self._adj(perf, "-1.00")

        response = self._delete([perf.id])
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 1001, body)          # `ProtectedError` ⇒ CONFLICT
        self.assertIn(str(perf.id), body["msg"])
        self.assertIn("调整记录", body["msg"])
        self.assertTrue(Performance.objects.filter(id=perf.id).exists())
        self.assertTrue(
            PerformanceAdjustment.objects.filter(id=adjustment.id).exists()
        )
        # 失败时不留「记了删除」的假留痕（审计与删除同事务）。
        self.assertFalse(
            OperationLog.objects.filter(
                action="performance.delete", target_id=str(perf.id)
            ).exists()
        )

    def test_delete_is_fail_closed_for_missing_ids(self):
        order = self._order()
        perf = self._perf(order, user=self.taker_a, emp_type=EmpType.SALES)
        body = self._delete([perf.id, 987654321]).json()
        self.assertEqual(body["code"], 1014, body)
        self.assertIn("不存在或不可见", body["msg"])
        self.assertTrue(Performance.objects.filter(id=perf.id).exists())


class ExportTests(_PerfWriteFixture):
    """导出记录：同源 / xlsx 响应头 / 上限 / 留痕。逐列逐格在 `test_performance_export_xlsx`。"""

    def test_export_uses_the_same_filter_source_as_the_list(self):
        """导出与列表**同源**：同一参数下两边命中的是同一批订单号。"""
        mine_order = self._order(status=OrderStatus.IN_PROGRESS)
        self._perf(mine_order, user=self.taker_a, emp_type=EmpType.SALES)
        other_order = self._order(status=OrderStatus.IN_PROGRESS)
        self._perf(other_order, user=self.taker_b, emp_type=EmpType.SALES)

        params = {"user": self.taker_a.id}
        response = self.finance.get(EXPORT_URL, params)
        self.assertEqual(response.status_code, 200, response.content[:200])
        # ⚠️ xlsx 是 zip 字节 ⇒ ⛔ 不能再 `decode("utf-8-sig")` + `assertIn`（会
        #    UnicodeDecodeError / 恒假）；按**单元格值**搜索。
        cells = values_of(workbook_of(response), "绩效明细")
        self.assertIn(mine_order.order_no, cells)           # 非空哨兵
        self.assertNotIn(other_order.order_no, cells)

        listed = self.finance.get("/api/business/performances/", params).json()
        self.assertEqual(listed["code"], 0, listed)
        self.assertEqual(
            [row["order_no"] for row in listed["data"]["items"]],
            [mine_order.order_no],
        )

    def test_export_streams_xlsx_with_attachment_headers(self):
        """成功时是 xlsx 文件流（MIME 精确 + 两个工作表 + 表头逐字）+ 双文件名响应头。"""
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._perf(order, user=self.taker_a, emp_type=EmpType.SALES)

        response = self.finance.get(EXPORT_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn(".xlsx", response["Content-Disposition"])
        self.assertIn("filename*=UTF-8''", response["Content-Disposition"])

        workbook = workbook_of(response)
        # ⭐ 导出**恰好 4 张**（本批新增两张副表）——字面列表，⛔ 不放宽成 `toContain`。
        self.assertEqual(
            workbook.sheetnames,
            [
                "绩效明细",
                "绩效汇总",
                "页一支付宝批量付款文件上传模板",
                "打款跳过名单",
            ],
        )
        rows = detail_rows(workbook)
        # 表头逐字（字面值，E-0024）；完整 29 列的顺序判据在
        # `tests/test_performance_export_xlsx.py`。
        self.assertEqual(rows[0][0], "用户姓名/名称")
        self.assertEqual(rows[0][9], "系统内置单号")
        self.assertEqual(rows[0][25], "清算状态")
        self.assertIn(order.order_no, values_of(workbook, "绩效明细"))

    def test_export_writes_one_audit_row_and_rejections_write_none(self):
        """⭐ Task L：导出**成功才留痕**（`performance.export`，spec §5.2 的挂账解除）。

        ⚠️ 判据打**数据库行**（E-0009），期望值一律写**字面值**（E-0024）。
        ⚠️ 超上限被拒的那次**不写**：它没有导出任何数据，记一条「导出了」就是假话。
        """
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._perf(order, user=self.taker_a, emp_type=EmpType.SALES)

        rows = OperationLog.objects.filter(action="performance.export")
        before = rows.count()

        response = self.finance.get(EXPORT_URL)
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(rows.count(), before + 1)

        row = rows.order_by("id").last()
        self.assertEqual(row.log_type, 20)
        self.assertEqual(row.actor, "p4fu-fin")
        self.assertEqual(row.summary, "【p4fu-fin】已将绩效和提成记录导出为 Excel")
        # 对标本动作那一行的四个「相关」列**全是空串** ⇒ 不硬塞 target_kind / target_id。
        self.assertEqual(row.target_kind, "")
        self.assertEqual(row.target_id, "")

        # 被拒的那次（`?user=abc` 过不了筛选序列化器）不写：同一条「没导出任何数据」的路径。
        rejected = self.finance.get(EXPORT_URL, {"user": "abc"})
        self.assertEqual(rejected.status_code, 200, rejected.content[:200])
        self.assertEqual(rejected.json()["code"], 1000, rejected.json())
        self.assertEqual(rows.count(), before + 1, "被拒的导出不该留痕")

    def test_export_over_the_row_cap_errors_instead_of_truncating(self):
        """边界：**恰好** 10000 行导得出来、**多一行**就报错（不是截断成前 N 行）。

        ⚠️ 真的造行（不是打桩改小上限）：打桩只能证明「有这个分支」，证明不了
        「真到 10000 行时会走它」。两个期望值都写**字面**（E-0024）——
        `10001` = 表头 + 10000 数据行；`10000` 出现在报错的 msg 里。

        ⚠️ **每行都要带归属人**（第四轮 A：导出与列表同源，排除
        `user IS NULL` 的行）——不带归属人的话这 10000 行一条都进不了导出，这条判据
        会静默变空。而带上归属人之后**不能再共用一张订单**：`Performance` 的
        `uniq_performance_order_user`（同订单同人唯一）会当场拒绝 ⇒ 这里改成
        **一万张订单各挂一条**（仍用 `SALES`，避开 `uniq_order_main_taker`）。
        """
        orders = Order.objects.bulk_create(
            [
                Order(
                    game=self.game,
                    # 20 位订单号（`Order.order_no` 的列宽），一万个互不相同；
                    # 前缀用 `20260923`（**避开**夹具 `_order()` 的 `2026092200000…` 段）。
                    order_no=f"20260923{i:012d}",
                    status=OrderStatus.IN_PROGRESS,
                    hall_price=Decimal("100.00"),
                )
                for i in range(10000)
            ]
        )
        Performance.objects.bulk_create(
            [
                Performance(
                    order_id=order.pk, emp_type=EmpType.SALES,
                    user=self.taker_a,
                    temp_amount=Decimal("0.00"),
                )
                for order in orders
            ]
        )

        # ① 恰好等于上限 ⇒ 真的把 xlsx 导出来（行数按工作表读回，不再数 CSV 行）
        ok = self.finance.get(EXPORT_URL)
        self.assertEqual(ok.status_code, 200, ok.content[:200])
        self.assertEqual(
            ok["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ok["Content-Type"],
        )
        self.assertEqual(
            len(detail_rows(workbook_of(ok))), 10001,
            "上限内的导出应当是「表头 + 10000 行」",
        )

        # ② 越过上限一行 ⇒ 报错，且**不给**半截文件（同样要带归属人，理由见上）
        extra = self._order(status=OrderStatus.IN_PROGRESS)
        Performance.objects.create(
            order=extra, emp_type=EmpType.SALES, user=self.taker_a,
            temp_amount=Decimal("0.00"),
        )
        response = self.finance.get(EXPORT_URL)
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("10000", body["msg"])
        self.assertIn("超过上限", body["msg"])
        # ⛔ 没有半截文件：不是 xlsx（回的是 JSON 信封），也搜不到任何表头文字。
        self.assertTrue(response["Content-Type"].startswith("application/json"))
        self.assertNotIn("用户姓名/名称", response.content.decode("utf-8", errors="ignore"))


class PermissionTests(_PerfWriteFixture):
    """三颗按钮各一组：无节点 ⇒ 403（且 msg 可辨）；有节点 ⇒ 走得通。"""

    _GROUP_SEQ = 0

    def _client(self, username, *codes):
        """真实登录 + **专属节点组**（`Role.name` 唯一 ⇒ 同组会共享节点）。

        ⭐ 按游戏权限收窄）：本组用例的账号是**请求方**，
        结账 / 撤销 / 删除都按请求方的游戏权限收窄取行 ⇒ 这里显式声明它持有
        本夹具的游戏（不声明时「有节点」那条腿会以 `1006`/`1014` 红）。
        """
        type(self)._GROUP_SEQ += 1
        group = f"节点N{type(self)._GROUP_SEQ}"
        client = make_logged_in_client(username=username, groups=(group,), nodes=codes)
        grant_games(User.objects.get(username=username), self.game)
        return client

    def test_revoke_needs_the_settlement_node(self):
        order = self._order()
        perf = self._perf(order, user=self.taker_a, emp_type=EmpType.SALES)
        allowed = self._client("perm-revoke-ok", NODE_SETTLE)
        denied = self._client("perm-revoke-no", NODE_DELETE)
        self._settle([perf.id], client=allowed)          # 正向结账也是**同一个**节点

        rejected = self._revoke([perf.id], client=denied)
        self.assertEqual(rejected.status_code, 403, rejected.content[:200])
        self.assertIn("撤销结账", rejected.json()["msg"])

        ok = self._revoke([perf.id], client=allowed)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], 0, ok.json())

    def test_delete_needs_the_delete_node(self):
        order = self._order()
        perf = self._perf(order, user=None, emp_type=EmpType.SALES)
        allowed = self._client("perm-delete-ok", NODE_DELETE)
        denied = self._client("perm-delete-no", NODE_EXPORT)

        rejected = self._delete([perf.id], client=denied)
        self.assertEqual(rejected.status_code, 403, rejected.content[:200])
        self.assertIn("删除绩效记录", rejected.json()["msg"])

        ok = self._delete([perf.id], client=allowed)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], 0, ok.json())

    def test_export_needs_the_export_node(self):
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._perf(order, user=None, emp_type=EmpType.SALES)
        allowed = self._client("perm-export-ok", NODE_EXPORT)
        denied = self._client("perm-export-no", NODE_DELETE)

        rejected = denied.get(EXPORT_URL)
        self.assertEqual(rejected.status_code, 403, rejected.content[:200])
        self.assertIn("导出绩效记录", rejected.json()["msg"])

        ok = allowed.get(EXPORT_URL)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(
            ok["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_anonymous_requests_are_401(self):
        """对外契约：401 = 没登录，403 = 登录了但没权限（前端按状态码分支）。"""
        from django.test import Client

        anonymous = Client(SERVER_NAME="tenant.test.com")
        for url in (REVOKE_URL, DELETE_URL, EXPORT_URL):
            with self.subTest(url=url):
                response = anonymous.post(url, data="{}", content_type="application/json")
                self.assertEqual(response.status_code, 401, response.content[:200])


class ExportDataScopeTests(_PerfWriteFixture):
    """⭐ 导出端点的**数据范围**（2026-09-23 最终修复波 F1）。

    **它守的是什么**：数据范围收口原先只写在 `PerformanceViewSet.list()` 里，而
    `PerformanceExportView` 复用同一个 `performance_queryset` **没有**收口 ⇒
    一个持 `finance.export_performance`（133）而**不持** `order.list`（14）的自定义职位
    能导出**全工作室**的绩效行。内置职位今天恰好都不满足这个组合（所以是潜伏的洞），
    但自定义职位可以——正是本线要关闭的那一类数据可见性缺陷。

    两条判据互为对照（缺一条就分不清「真的收窄了」与「本来就什么都有」/「本来就什么都没有」）：
    - 不持 14 ⇒ 只导得出自己的行（别人的行号**不在**明细正文里）；
    - 持 14 ⇒ 两行都导得出来（数据范围不能把管理侧也收窄）。

    ⚠️ 断言打**真实出口**：读 HTTP 响应的 **xlsx 字节**（openpyxl 读回**单元格值**），
    ⛔ 不对 mock / 内部函数断言——「导出的是另一批数据」这类 bug 只在真实正文里看得见。
    按工作表行数和单元格值判。xlsx 是 zip 字节，按文本解码会 UnicodeDecodeError。
    """

    _GROUP_SEQ = 0

    def _client(self, username, *codes):
        """真实登录 + **专属节点组**（`Role.name` 唯一且 `max_length=5` ⇒ 同组会共享节点）。"""
        type(self)._GROUP_SEQ += 1
        group = f"导出N{type(self)._GROUP_SEQ}"        # 4 个字符 + 序号，不超过 5
        client = make_logged_in_client(username=username, groups=(group,), nodes=codes)
        # 导出按这个人的游戏权限收窄。不授权的话两条用例都只剩表头。
        grant_games(User.objects.get(username=username), self.game)
        return client

    def _row_for(self, user):
        """给指定归属人建一条绩效行（不同归属人各用一张订单，避开同订单同人唯一约束）。"""
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._perf(order, user=user, emp_type=EmpType.SALES)
        return order.order_no

    def test_export_without_the_management_node_contains_only_my_rows(self):
        """持 133（导出）但不持 14（管理侧）⇒ 明细里**只有自己的行**。"""
        client = self._client("scope-export-own", NODE_EXPORT)
        me = User.objects.get(username="scope-export-own")
        mine_no = self._row_for(me)
        theirs_no = self._row_for(self.taker_b)

        response = client.get(EXPORT_URL)

        self.assertEqual(response.status_code, 200, response.content[:200])
        workbook = workbook_of(response)
        rows = detail_rows(workbook)
        self.assertEqual(len(rows), 2, "应为表头 + 恰好自己那一行")     # 非空哨兵
        cells = values_of(workbook, "绩效明细")
        self.assertIn(mine_no, cells)                                  # 字面值：自己的行在
        self.assertNotIn(theirs_no, cells)                             # 别人的行不在

    def test_export_with_the_management_node_contains_every_row(self):
        """回归：持 `order.list` 的调用方仍然导得出**全部**行（收口不能把管理侧也收窄）。"""
        client = self._client("scope-export-all", NODE_EXPORT, "order.list")
        me = User.objects.get(username="scope-export-all")
        mine_no = self._row_for(me)
        theirs_no = self._row_for(self.taker_b)

        response = client.get(EXPORT_URL)

        self.assertEqual(response.status_code, 200, response.content[:200])
        workbook = workbook_of(response)
        self.assertEqual(len(detail_rows(workbook)), 3, "应为表头 + 两行")   # 非空哨兵
        cells = values_of(workbook, "绩效明细")
        self.assertIn(mine_no, cells)
        self.assertIn(theirs_no, cells)


# ── 跨租户隔离（AGENTS.md §4：以租户 A 的凭据访问 B 的数据必须失败）──────

DOMAIN_A = "tenant.test.com"
DOMAIN_B = "p4fu-b-iso.test.local"
SCHEMA_B = "p4fu_b_iso"
NO_B = "20260922000000009901"
USER_B = "p4fu-iso-user-b"


class CrossTenantIsolationTests(_PerfWriteFixture):
    """三颗按钮各一条跨租户回归 + 一条凭据平面回归。

    ⚠️ **判据不用主键指认「另一个租户的那一行」**（两个 schema 的主键序列各自从起点开始）：
    撤销/删除的请求体只能带 id，所以这里用「把 B 的绩效主键推到 A 的最大值之上」当
    **构造保证**（`assertFalse` 显式自证它在 A 里不存在），再用**订单号**断言 B 的行没被动过。
    导出走响应体，直接用订单号判据。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="P4FU-B 隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)

        # A 侧：一条已结账的 SALES 行（跨租户请求不该动它，也不该动 B 的任何行）。
        # ⚠️ 订单必须是 `COMPLETED`：P4FU-A（D-0074）起结账判据 =「订单已完成 + 有归属人 + 未结账」，
        #    本夹具只需要「A 侧有一条已结账的行」，与订单状态无关 ⇒ 按新判据建成已完成。
        self.order_a = self._order(status=OrderStatus.COMPLETED)
        self.perf_a = self._perf(
            self.order_a, user=self.taker_a, emp_type=EmpType.SALES
        )
        self._settle([self.perf_a.id])
        max_a = (
            Performance.objects.order_by("-id").values_list("id", flat=True).first() or 0
        )

        try:
            with tenant_context(self.tenant_b):
                game_b = Game.objects.create(name="P4FU-B 隔离游戏 B")
                # ⚠️ 同 A 侧：结账判据（D-0074）要求订单已完成 ⇒ 这个夹具的订单建成 COMPLETED。
                order_b = Order.objects.create(
                    game=game_b,
                    order_no=NO_B,
                    status=OrderStatus.COMPLETED,
                    hall_price=Decimal("50.00"),
                )
                User.objects.create_user(username=USER_B)
                user_b = User.objects.get(username=USER_B)
                # 循环建行直到 B 的主键越过 A 的最大值 ⇒ `perf_b.id` 在 A 里不存在。
                # 先建的填充行没有归属人（`user=None` ⇒ `review_status` 必须是 NULL）。
                while True:
                    perf_b = Performance.objects.create(
                        order=order_b,
                        user=user_b,
                        emp_type=EmpType.SALES,
                        temp_amount=Decimal("20.00"),
                        bonus_amount=Decimal("20.00"),
                        review_status=ReviewStatus.APPROVED,
                    )
                    if perf_b.pk > max_a:
                        break
                    Performance.objects.filter(id=perf_b.id).update(
                        user=None, review_status=None
                    )
                self.perf_b = perf_b
                self.admin_b = make_logged_in_client(
                    domain=DOMAIN_B, username="p4fu-iso-admin-b", is_superuser=True
                )
                settled = self.admin_b.post(
                    SETTLE_URL,
                    data=json.dumps(
                        {"performance_ids": [perf_b.id], "reason": ""}
                    ),
                    content_type="application/json",
                )
                assert settled.json()["code"] == 0, settled.json()
        finally:
            connection.set_tenant(self.tenant)

        # 夹具自证：B 的绩效 id 在 A 的 schema 里确实不存在。
        self.assertFalse(
            Performance.objects.filter(id=self.perf_b.id).exists(),
            "构造失效：B 的绩效 id 在 A 里也存在，跨租户否定断言无意义",
        )

    def _in_b(self, function):
        try:
            with tenant_context(self.tenant_b):
                return function()
        finally:
            connection.set_tenant(self.tenant)

    def _b_row(self):
        return self._in_b(
            lambda: Performance.objects.select_related("order").get(id=self.perf_b.id)
        )

    def test_revoke_cannot_reach_the_other_tenants_row(self):
        body = self._revoke([self.perf_b.id], reason="跨租户尝试").json()
        self.assertEqual(body["code"], 1006, body)
        self.assertIn("不存在或不可见", body["msg"])

        row = self._b_row()
        self.assertEqual(row.order.order_no, NO_B)     # 判据用订单号，不是主键
        self.assertEqual(row.settle_status, "已结账")
        self.assertIsNotNone(row.settled_at)

        self.perf_a.refresh_from_db()
        self.assertEqual(self.perf_a.settle_status, "已结账")

    def test_delete_cannot_reach_the_other_tenants_row(self):
        body = self._delete([self.perf_b.id]).json()
        self.assertEqual(body["code"], 1014, body)
        self.assertIn("不存在或不可见", body["msg"])
        self.assertTrue(
            self._in_b(
                lambda: Performance.objects.filter(id=self.perf_b.id).exists()
            ),
            "B 的绩效行被跨租户请求删掉了",
        )

    def test_export_only_contains_the_current_tenants_rows(self):
        a_cells = values_of(workbook_of(self.finance.get(EXPORT_URL)), "绩效明细")
        self.assertIn(self.order_a.order_no, a_cells)        # 非空哨兵
        self.assertNotIn(NO_B, a_cells)

        try:
            response_b = self.admin_b.get(EXPORT_URL, SERVER_NAME=DOMAIN_B)
        finally:
            connection.set_tenant(self.tenant)
        b_cells = values_of(workbook_of(response_b), "绩效明细")
        self.assertIn(NO_B, b_cells)                         # 非空哨兵
        self.assertNotIn(self.order_a.order_no, b_cells)

    def test_a_session_on_the_other_tenants_host_is_rejected(self):
        """凭据平面：A 的会话拿到 B 的域名上 ⇒ 401（D-0009 的会话强绑身份平面）。"""
        try:
            response = self.finance.get(EXPORT_URL, SERVER_NAME=DOMAIN_B)
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(response.status_code, 401, response.content[:200])


# ── 数据权限卡点（`scope_queryset`）的**接线判据**──


def _recording_scope(calls):
    """`scope_queryset` 的替身：记录 `(user, queryset.model)` 后**照常转发**真实实现。

    ⚠️ 必须转发（不是直接 `return queryset` 了事）：否则「请求仍然成功」这件事就不再是
    真实路径的结论，桩会把被测行为换掉。
    ⚠️ 在 `mock.patch` **之前**调用本函数：此刻 import 到的是真实现（定义处被 patch 时
    再 import 会拿到替身，造成自递归）。
    """
    from apps.core.scoping import scope_queryset as real_scope

    def spy(user, queryset, scope_filter=None):
        calls.append((user, queryset.model))
        return real_scope(user, queryset, scope_filter)

    return spy


class ScopingWiringTests(_PerfWriteFixture):
    """三个新端点的**数据权限卡点接线**真的被用上（P3a 教训：只测函数 = 没人守接线）。

    ⚠️ **为什么需要这一组**（独立评审 Important，2026-09-22 实测）：`core/scoping.py`
    今天不传 `scope_filter` 就是**基线 pass-through**，而 `CrossTenantIsolationTests` 用
    「B 的主键推到 A 的最大值之上」构造 ⇒ 那个 id 在 A 里天然不存在 ⇒ 把卡点删掉/改成
    no-op，跨租户用例**照样全绿**。所以「卡点被调用、且拿到的是**当前请求的 user** 与
    **该端点的 queryset**」必须自己有一条判据：

    - 断言 `calls` **非空** —— 这同时是「桩接上了」的自证（E-0029：接不上时这条先红）；
    - 断言**调用次数**逐端点写死（撤销 2 / 删除 2 / 导出 1）—— ⚠️ 这条是复评
      补的承重判据：删除端点有两处卡点且**用同一张表**，只断「模型集合」时删掉任一处都不红；
    - 断言每条记录里的 user 就是本次请求的账号（比 `pk`，不拿 `None` 也算过）；
    - 断言 queryset 的**模型**是这个端点该碰的那张表（撤销是 `Performance` +
      `PerformanceAdjustment` 两类，删除/导出只有 `Performance`）。

    ⚠️ **桩打在「被查找的那个模块」**（E-0029）：`performance_writes` 与
    `api/performance_views` 各自在模块顶部 `from apps.core.scoping import scope_queryset`
    ⇒ 补丁目标必须是这两个模块；打在**定义处** `apps.core.scoping` 一条都收不到——
    那条对照见 `test_patching_the_definition_site_does_not_intercept`。
    """

    def _expected_user(self):
        return User.objects.get(username="p4fu-fin")

    def _assert_all_calls_use(self, calls, *, models, expected_calls):
        """两条判据缺一不可：**调用次数**（每条读取路径各自的卡点）与**每次的入参**。

        ⚠️ **为什么次数是承重的**（复评实测）：删除端点有**两处**卡点
        （读数一处 + `delete()` 一处），两处用的都是 `Performance` 同一张表
        ⇒ 只断「模型集合 == {Performance}」时，**删掉任意一处**都仍然成立
        （剩下的那处照样满足），于是「非可见行通过 fail-closed 检查、写出假留痕」这种
        真实缺陷可以整份用例全绿地溜过去。次数写**字面整数**（E-0024：不引被测常量）。
        """
        self.assertTrue(calls, "卡点一次都没被调用——接线断了（桩没接上也会走到这里）")
        self.assertEqual(
            len(calls), expected_calls,
            f"卡点调用次数不对（应为 {expected_calls} 次，见本类各用例的注释）",
        )
        expected = self._expected_user()
        for user, _model in calls:
            self.assertIsNotNone(user, "卡点收到的 user 是 None")
            self.assertEqual(user.pk, expected.pk)
        self.assertEqual({model for _user, model in calls}, set(models))

    def test_revoke_passes_the_request_user_and_its_querysets_to_the_scope_gate(self):
        order = self._order()
        perf = self._perf(order, user=self.taker_a)
        self._adj(perf, "-5.00")
        self._settle([perf.id])

        calls = []
        with mock.patch(
            "apps.business.performance_writes.scope_queryset", _recording_scope(calls)
        ):
            body = self._revoke([perf.id], reason="接线判据").json()

        self.assertEqual(body["code"], 0, body)
        # 为什么是 **2**：成功路径上有两处读取——绩效行（`performance_writes.py:156`）
        # 与「同一轮」的调整记录（`:184`）；第三处（`:128`）只在 fail-closed 的报错文案里。
        self._assert_all_calls_use(
            calls, models={Performance, PerformanceAdjustment}, expected_calls=2
        )

    def test_delete_passes_the_request_user_and_its_queryset_to_the_scope_gate(self):
        order = self._order()
        perf = self._perf(order, user=self.taker_b, emp_type=EmpType.SALES)

        calls = []
        with mock.patch(
            "apps.business.performance_writes.scope_queryset", _recording_scope(calls)
        ):
            body = self._delete([perf.id]).json()

        self.assertEqual(body["code"], 0, body)
        # 为什么是 **2**：删除成功路径上有**两处**卡点，用的是同一张表、必须分开数——
        # 读数那处（`performance_writes.py:274`，决定哪些行可见、能不能删）与
        # `delete()` 那处（`:342`，真正落删的谓词）。只断「模型集合」时删掉任一处都不会红。
        self._assert_all_calls_use(calls, models={Performance}, expected_calls=2)

    def test_export_passes_the_request_user_and_its_queryset_to_the_scope_gate(self):
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._perf(order, user=self.taker_a, emp_type=EmpType.SALES)

        calls = []
        with mock.patch(
            "apps.business.api.performance_views.scope_queryset",
            _recording_scope(calls),
        ):
            response = self.finance.get(EXPORT_URL)

        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            response["Content-Type"],
        )
        # 为什么是 **1**：导出的筛选段只有一处卡点（`performance_views.py:99`，
        # 列表与导出共用的那个函数）。
        self._assert_all_calls_use(calls, models={Performance}, expected_calls=1)

    def test_patching_the_definition_site_does_not_intercept(self):
        """**对照**（E-0029）：桩打在**定义处**收不到调用 ⇒ 上面三条打在查找处才是判据。

        ⚠️ 这就是「不接桩时它应当失败」的常驻版本：若哪天有人把三条用例的补丁目标改成
        `apps.core.scoping.scope_queryset`，它们会**先**红在这条对照的判据上（`calls` 非空
        的断言会失败），而不是给出一个看起来通过的假保证。
        """
        order = self._order()
        perf = self._perf(order, user=self.taker_b, emp_type=EmpType.SALES)

        calls = []
        with mock.patch("apps.core.scoping.scope_queryset", _recording_scope(calls)):
            body = self._delete([perf.id]).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            calls, [],
            "桩打在定义处居然被命中——本仓的 import 形状变了，要重核 E-0029 的口径",
        )


class RevokeSkipScopeGateTests(_PerfWriteFixture):
    """⭐ M-2：`performance_writes.already_unsettled_ids` 的**按游戏收窄**是承重的。

    **变异实测**：把那个函数里的
    `scope_filter=game_scope("order__game_id")` 删掉，`tests.test_settle_api` +
    `tests.test_p4fu_performance_writes` **88 条全绿**（`SURVIVED`）——「已经是未结账
    ⇒ 跳过、不报错」这道判据一旦丢了收窄，**别人游戏的未结账行**会被念成
    「本来就是未结账、跳过吧」而让整批成功：既是**一句假话**（那一行不在请求方的
    可见范围内），也是一次**越权被静默吞掉**（本该 `1006`「不存在或不可见」）。

    判据形态照 `tests/test_settlement_preview_api.py::PreviewScopeTests`：
    `games=[]` 是**显式 opt-out**（夹具默认给当前租户的**全部**游戏 ⇒ 区分不出范围），
    权限的唯一来源是 `grant_games(..., game_a)`。
    """

    def setUp(self):
        super().setUp()
        self.game_b = Game.objects.create(name="另一个游戏")

    def _a_only(self):
        """只勾 `game_a`（= 夹具自带的 `self.game`）的账号：持撤销节点、零游戏起步。"""
        client = make_logged_in_client(
            username="p4fu-a-only",
            groups=("撤销甲",),
            nodes=(NODE_SETTLE,),
            games=[],
        )
        grant_games(User.objects.get(username="p4fu-a-only"), self.game)
        return client

    def _order_b(self):
        self._seq += 1
        return Order.objects.create(
            game=self.game_b,
            order_no=f"2026092200001{self._seq:07d}",
            status=OrderStatus.COMPLETED,
            hall_price=Decimal("100.00"),
        )

    def test_another_games_unsettled_row_is_rejected_not_silently_skipped(self):
        """B 游戏的**未结账**行在 A 账号下 ⇒ 仍 `1006`，⛔ 不进「已跳过」。

        前置哨兵两条（否则「没跳过」与「端点坏了」不可区分）：
        ① 同一次会话里 A 游戏的**同样形态**那一行**确实**被跳过（`code=0` + `skipped` 含它）；
        ② B 游戏那一行**真的在库里**且确实是 `未结账`。
        ⚠️ 失败信封的 `data` 是 `None` —— 「`skipped_performances` 里没有它」在这里
        等价于「**根本没有成功信封**」。
        """
        client = self._a_only()
        mine = self._perf(
            self._order(), user=self.taker_a, settle_status=SettleStatus.UNSETTLED
        )
        theirs = self._perf(
            self._order_b(), user=self.taker_b, settle_status=SettleStatus.UNSETTLED
        )
        # 哨兵②：两行都在、且都是「未结账」。
        self.assertEqual(
            Performance.objects.filter(
                id__in=[mine.id, theirs.id], settle_status="未结账"
            ).count(),
            2,
        )

        # 哨兵①：A 游戏那一行走成功路径（否则下面那条 `1006` 可能只是端点在闹脾气）。
        ok = self._revoke([mine.id], client=client).json()
        self.assertEqual(ok["code"], 0, ok)
        self.assertEqual(ok["data"]["skipped_performances"], [mine.id])

        # 正题：B 游戏那一行 ⇒ 整批 `1006`、没有成功信封、那一行一个字节没动。
        theirs.refresh_from_db()
        untouched_at = theirs.updated_at
        body = self._revoke([theirs.id], client=client).json()
        self.assertEqual(body["code"], 1006, body)
        self.assertIn("不存在或不可见", body["msg"])
        self.assertIsNone(body["data"])
        theirs.refresh_from_db()
        self.assertEqual(theirs.settle_status, "未结账")
        self.assertEqual(theirs.updated_at, untouched_at)
