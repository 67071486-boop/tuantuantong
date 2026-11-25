"""P4 三个动作（结账 / 退款 / 人工修正）的**新表**行级断言 + 旧表的**历史形状**守卫。

    ## 本文件在 M3a改了什么

    原文断言的是 `apps/business/audit.py` 的三个 legacy 出口（`log_settle` / `log_refund` /
    `log_manual_adjustment`）写进**旧** `AuditLog` 表。M3a 把这三个出口退役（见 spec §16.1
    「旧出口退役」）：7 个调用处迁到唯一写入出口 `apps/audit/log.py::record(...)`，`audit.py`
    删除。所以本文件的「出口行级断言」**搬到新表**（`OperationLog`），并且**钉住 P4 载荷的
    承重语义**（spec §3.6 / §5.9）：

    - **结账 / 人工修正带绩效身份**：旧 `log_settle` / `log_manual_adjustment` 带
    `performance_id` 正是为了「一条订单可以同时有打手行与客服行，只用 `order_no` 分不出
    结的是哪条」。`OperationLog` **没有** `performance_id` 列 ⇒ 身份按 R11 落到
    `target_kind="user"` + `target_id`/`target_name` = 绩效归属人
    （`uniq_performance_order_user` 保证「订单 × 人」唯一）。
    - **退款是订单级**：`target_kind="order"`、`target_id` = 订单号；`from/to` 是
    `Order.refund_fee` 的旧 / 新累计额。
    - **`reason` 不许丢**（Ruling S：财务备注是事后对账的依据）。
    - **`performance.adjust_manual` 与 `performance.adjust` 是两个 code**（Ruling N），
    模板也一词不同（「新增了…绩效调整记录」vs「修改了…的字段」）。

    ## 保留在旧表上的三组断言（**不是**遗留，是迁移输入的守卫）

    `AuditActionChoicesTests` / `AuditPerformanceIdFieldTests` / `AuditPerformanceIdColumnTests`
    断言的是**旧表的形状**——它是冻结迁移 `0004_backfill_from_auditlog` 的**输入契约**，
    删掉它们等于让「迁移读的是哪张表、有哪些 action 取值」失去守卫。旧表按 spec §4.3
    「保留到迁移验证通过后再删」，故这三组**原样保留**。

    ## 两条纪律

    ⚠️ **为什么断言数据库行**：本项目没有 `LOGGING` 配置，`logger.info()` 是空操作；
    而 Django 自带的日志断言会把目标 logger 的级别临时抬到 INFO ⇒ 用日志实现的「留痕」测试
    **全绿、生产一行都没写**（E-0009）。⛔ 本文件**不写任何日志断言**。

    ⚠️ **期望值写字符串字面值**，不引被测模块导出的常量：`assertEqual(row.action, X)` 在 `X`
    被改坏时**不会红**（E-0024）。
"""
from django.contrib.auth.models import User
from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.audit.log import record
from apps.audit.models import OperationLog
from apps.business.models import AuditLog


class OperationLogExitRowTests(TenantTestCase):
    """三个 P4 动作各写一次 → 断言**新表那一行**的具体字段值（§9 #17 的出口部分）。"""

    def setUp(self):
        self.user = User.objects.create_user(username="perf-owner-1")

    def test_performance_settle_row_carries_the_performance_owner(self):
        """结账：动作/分类/前后值/备注 + **绩效身份**（R11 的 user 三元组）。"""
        record(
            log_type=20,
            action="performance.settle",
            actor="finance1",
            actor_id=7,
            target_kind="user",
            target_id=self.user.pk,
            target_name="perf-owner-1",
            order_no="20260920000000000041",
            from_value="未结账",
            to_value="已结账",
            reason="9 月结账",
        )
        row = OperationLog.objects.get(order_no="20260920000000000041")
        self.assertEqual(row.action, "performance.settle")
        self.assertEqual(row.log_type, 20)
        self.assertEqual(row.target_kind, "user")
        self.assertEqual(row.target_id, str(self.user.pk))
        self.assertEqual(row.target_name, "perf-owner-1")
        self.assertEqual(row.actor, "finance1")
        self.assertEqual(row.actor_id, 7)
        self.assertEqual(row.from_value, "未结账")
        self.assertEqual(row.to_value, "已结账")
        self.assertEqual(row.reason, "9 月结账")
        # 照抄对标原句（RECON §1.4，7,343 行），逐字。
        self.assertEqual(
            row.summary, "【finance1】已将perf-owner-1的绩效结账状态标记为已结账（仅标记状态）（订单 20260920000000000041）"
        )

    def test_order_refund_row_is_order_level(self):
        """退款：**订单级**（`target_kind="order"`），前后值是累计退款额。"""
        record(
            log_type=10,
            action="order.refund",
            actor="cs1",
            # ⭐ 本行只测「订单级」形状、没有认证态用户
            # ⇒ 显式声明机器来源（`record()` 对「没有操作人」fail-closed）。
            system=True,
            target_kind="order",
            target_id="20260920000000000042",
            order_no="20260920000000000042",
            from_value="0.00",
            to_value="50.00",
            reason="客户投诉进度慢",
        )
        row = OperationLog.objects.get(order_no="20260920000000000042")
        self.assertEqual(row.action, "order.refund")
        self.assertEqual(row.log_type, 10)
        self.assertEqual(row.target_kind, "order")
        self.assertEqual(row.target_id, "20260920000000000042")
        self.assertEqual(row.target_name, "")
        self.assertEqual(row.actor, "cs1")
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.from_value, "0.00")
        self.assertEqual(row.to_value, "50.00")
        self.assertEqual(row.reason, "客户投诉进度慢")
        # ⭐ 订单日志人名线 spec §3-A1：机器行（`actor_id is None`）现在
        # **机器行也加** `【来源】` 前缀（来源 = `actor` 列）。
        self.assertEqual(
            row.summary,
            "【cs1】将订单 20260920000000000042 的累计退款额从 0.00 元更新为 50.00 元"
            "（原因：客户投诉进度慢）",
        )

    def test_performance_adjust_manual_row_is_distinct_from_performance_adjust(self):
        """人工修正：动作是 `performance.adjust_manual`（**不是** `performance.adjust`，Ruling N）。

    文案也一词不同：`新增了…绩效调整记录` ≠ `修改了…的字段`。
"""
        record(
            log_type=20,
            action="performance.adjust_manual",
            actor="finance2",
            actor_id=9,
            target_kind="user",
            target_id=self.user.pk,
            target_name="perf-owner-1",
            order_no="20260920000000000043",
            from_value="",
            to_value="-8.00",
            reason="手工修正坏账",
        )
        row = OperationLog.objects.get(order_no="20260920000000000043")
        self.assertEqual(row.action, "performance.adjust_manual")
        self.assertEqual(row.log_type, 20)
        self.assertEqual(row.target_kind, "user")
        self.assertEqual(row.target_id, str(self.user.pk))
        self.assertEqual(row.target_name, "perf-owner-1")
        self.assertEqual(row.from_value, "")
        self.assertEqual(row.to_value, "-8.00")
        self.assertEqual(row.reason, "手工修正坏账")
        self.assertEqual(
            row.summary,
            "【finance2】新增了perf-owner-1的绩效调整记录（变更前：，变更后：-8.00），备注：手工修正坏账（订单 20260920000000000043）",
        )

    def test_manual_adjustment_reason_longer_than_the_column_is_truncated(self):
        """请求来源的值不能因为超长而 500（E-0010）——`reason` 列宽 255，超出按列宽截断。

    ⚠️ 这是 legacy `log_manual_adjustment` 的「`reason[:255]`」语义在新出口上的**等价**：
    截断由 `record()` 的 `_clip` 统一做（列宽取自模型元数据），调用方**不再**自己切。
"""
        record(
            log_type=20,
            action="performance.adjust_manual",
            actor="fin",
            # ⭐ 只测 `reason` 截断、没有认证态用户 ⇒ 显式声明机器来源。
            system=True,
            order_no="20260920000000000046",
            from_value="0.00",
            to_value="-1.00",
            reason="退" * 300,
        )
        row = OperationLog.objects.get(order_no="20260920000000000046")
        self.assertEqual(len(row.reason), 255)
        self.assertEqual(row.reason, "退" * 255)

    def test_the_legacy_table_gets_no_new_rows(self):
        """三个新动作全部写**新**表；旧表在这条出口上**零新增行**。"""
        before = AuditLog.objects.count()
        record(
            log_type=20,
            action="performance.settle",
            actor="finance1",
            # ⭐ 下面三条都只测「旧表零新增」、没有认证态用户
            # ⇒ 各显式声明机器来源（`record()` 对「没有操作人」fail-closed）。
            system=True,
            order_no="20260920000000000047",
            from_value="未结账",
            to_value="已结账",
        )
        record(
            log_type=10,
            action="order.refund",
            actor="cs1",
            system=True,
            order_no="20260920000000000048",
            from_value="0.00",
            to_value="10.00",
        )
        record(
            log_type=20,
            action="performance.adjust_manual",
            actor="finance2",
            system=True,
            order_no="20260920000000000049",
            to_value="5.00",
        )
        self.assertEqual(AuditLog.objects.count(), before)


class AuditActionChoicesTests(TenantTestCase):
    """旧表 `Action` 恰为 6 个取值，且前三个（既有对外契约）没被动。

    ⚠️ 这是**冻结迁移的输入契约**（`0004` 按旧 action 归类）：`SETTLE`/`REFUND`/`ADJUST`
    三个值必须继续存在，否则带这类历史行的库回填时会落到「认不出」的分支。
    期望值全是字面值。
"""

    def test_action_choices_are_exactly_the_six_literal_pairs(self):
        self.assertEqual(
            set(AuditLog.Action.values),
            {"STATUS_CHANGE", "PRICE_CHANGE", "REVIEW", "SETTLE", "REFUND", "ADJUST"},
        )
        self.assertEqual(
            list(AuditLog.Action.choices),
            [
                ("STATUS_CHANGE", "状态变更"),
                ("PRICE_CHANGE", "改价"),
                ("REVIEW", "审核"),
                ("SETTLE", "结账"),
                ("REFUND", "退款"),
                ("ADJUST", "绩效调整"),
            ],
        )

    def test_the_existing_three_action_values_are_untouched(self):
        self.assertEqual(
            list(AuditLog.Action.choices)[:3],
            [("STATUS_CHANGE", "状态变更"), ("PRICE_CHANGE", "改价"), ("REVIEW", "审核")],
        )


class AuditPerformanceIdFieldTests(TenantTestCase):
    """`performance_id` 必须是**裸整数列，不建 FK**（spec §3.6 的显式设计决定）。"""

    def test_performance_id_is_a_plain_integer_not_a_foreign_key(self):
        field = AuditLog._meta.get_field("performance_id")
        self.assertIsNone(
            field.remote_field,
            "performance_id 挂了 FK：留痕会被引用行阻塞（PROTECT）或因引用行消失而断链（SET_NULL）——"
            "spec §3.6 明确要求只存裸 id（同 order_no 存字符串的理由）。",
        )
        self.assertEqual(field.get_internal_type(), "IntegerField")


class AuditPerformanceIdColumnTests(TenantTestCase):
    """⭐ 判据打在**数据库列**上（`information_schema`），不是打在模型上：

    查模型时列还在也会「通过」——模型属性是声明式的，删列后的模型与迁移后的库
    在「字段存在」这件事上给出同一个答案，判别力为零（P4-1 的同款教训）。
"""

    def test_performance_id_column_exists_in_the_tenant_schema(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT data_type, is_nullable
                   FROM information_schema.columns
                   WHERE table_schema = %s
                     AND table_name = 'business_auditlog'
                     AND column_name = 'performance_id'""",
                [connection.schema_name],
            )
            rows = cursor.fetchall()
        self.assertEqual(rows, [("integer", "YES")])
