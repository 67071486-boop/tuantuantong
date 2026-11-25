"""旧 `AuditLog` → `OperationLog` 的数据迁移。

造旧行必须走本文件里的 `_legacy_log_*`。`apps/business/audit.py` 已删除，
那些函数体搬到这里，`reason` 的全角 `｜` 拼接保持原样。
手写 `AuditLog.objects.create(...)` 验不到「总价变更」和「绩效字段变更」
在 `reason` 上的差别，判别规则可以改坏而测试仍绿。

## ⭐ M3a：`actor` 超长行由 `0006` 接手（本文件的两条互补用例）

`0004` 跑在 `0005` **之前**，那时 `OperationLog.actor` 只有 **64** 宽，而旧表是 **150**
⇒ 原实现（原样搬运）在带 65–150 字 `actor` 的库上**第一次 `migrate` 会 `DataError` 中断**
（M2 实测）。修法 = `0004` 只搬放得下的行、`0006`（`0005` 之后）搬其余的行，两条判据互补。
本文件用两条用例钉住：① 整条链跑完，长 `actor` 行**不截断**且**恰好一行**；
② `0004` **单独**跑时**不搬**长行（把它真的放到 64 宽的表上跑，见用例内的 `ALTER TABLE`）。

## 为什么是「重记迁移记录 + MigrationExecutor 正向」而不是 migrate 反向

被迁移的是**回填**：迁移写出来的行与迁移之后应用自己写的行在表里长得一模一样，
没有能自我证明正确的反向。为了跑测试而写一个 `reverse_code`，等于把一个会删真实
数据的动作固化进迁移。所以测试用 `MigrationRecorder.record_unapplied` 只把
**记账**退回，再用 `MigrationExecutor` 正向重放；`TenantTestCase` 的用例级原子
回滚会把记账复原。

⚠️ **退回记账必须连 `0004` 之后的同 app 迁移一起退**（M2，2026-09-22 实测）：
`MigrationExecutor.migration_plan()` 见到**目标**（app 的叶子）已在 `applied` 里就走
**反向**分支、不再回头补它未应用的依赖 ⇒ 只退 `0004` 而叶子是 `0005` 时，
audit 的迁移计划**为空**、`0004` 根本不会重跑（症状是本文件三条用例「回填出来 0 行」，
看着像迁移坏了）。所以判据收在 `rewind_backfill_record()` 一处：**新增 audit 迁移时
必须把它加进 `MIGRATIONS_AFTER_BACKFILL`**，否则症状同上复现。

## 对位方式：按 `(order_no, reason)`

旧行与新行都保留 `order_no` / `reason`（迁移不改写这两列），且用例里每对取值唯一 ⇒
这是天然的、不依赖主键或写入顺序的一一对应键（`log_status_change` 不写 `reason`，
所以两条状态行用不同 `order_no` 区分）。
"""
import importlib
import inspect
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.audit import actions
from apps.audit.models import OperationLog
from apps.business.models import AuditLog, OrderStatus


# ---------------------------------------------------------------------------
# 旧写入函数的**历史夹具**（从被删除的 `apps/business/audit.py` 逐字搬来）
#
# 它们**只用于造旧行**，不参与生产。搬进测试的理由见模块 docstring 的更正块：
# 该模块零生产调用方，而本测试需要「由真实写入语句产生」的旧行形状。
# 搬动时**只改了函数名**，`create(...)` 的字段、`reason` 的全角 `｜` 拼接、
# `text[:255]` 的截断都逐字保留——改坏其中任何一处，下面的迁移断言就该红。
# ---------------------------------------------------------------------------
def _legacy_log_status_change(*, order_no, from_status, to_status, actor, roles=None):
    # roles 暂不落库：P2 无认证，roles 恒为 None（spec D8）。P5 接上角色后再加列。
    AuditLog.objects.create(
        action=AuditLog.Action.STATUS_CHANGE,
        order_no=order_no,
        actor=actor or "",
        from_value=from_status or "",
        to_value=to_status or "",
    )


def _legacy_log_price_change(*, order_no, old_price, new_price, reason, actor):
    AuditLog.objects.create(
        action=AuditLog.Action.PRICE_CHANGE,
        order_no=order_no,
        actor=actor or "",
        reason=reason or "",
        from_value="" if old_price is None else str(old_price),
        to_value=str(new_price),
    )


def _legacy_log_performance_adjust(*, order_no, changes, actor, reason=""):
    """把一次 `/adjust` 的**每个被改字段各写一行**（一行一个被改字段）。"""
    for label, old, new in changes:
        text = f"{label}｜{reason}" if reason else label
        AuditLog.objects.create(
            action=AuditLog.Action.PRICE_CHANGE,
            order_no=order_no,
            actor=actor or "",
            reason=text[:255],
            from_value="" if old is None else str(old),
            to_value="" if new is None else str(new),
        )


def _legacy_log_performance_review(*, order_no, old_status, new_status, actor, reason=""):
    AuditLog.objects.create(
        action=AuditLog.Action.REVIEW,
        order_no=order_no,
        actor=actor or "",
        reason=(reason or "")[:255],
        from_value=old_status or "",
        to_value=new_status or "",
    )


def _legacy_log_settle(*, order_no, performance_id, actor, from_value, to_value, reason=""):
    """⭐ M3a：逐字搬自被删除的 `apps/business/audit.py`（只改函数名）。"""
    AuditLog.objects.create(
        action=AuditLog.Action.SETTLE,
        order_no=order_no,
        performance_id=performance_id,
        actor=actor or "",
        reason=(reason or "")[:255],
        from_value="" if from_value is None else str(from_value),
        to_value="" if to_value is None else str(to_value),
    )


def _legacy_log_refund(*, order_no, actor, from_value, to_value, reason):
    """⭐ M3a：逐字搬自 `apps/business/audit.py`（只改函数名）。"""
    AuditLog.objects.create(
        action=AuditLog.Action.REFUND,
        order_no=order_no,
        actor=actor or "",
        reason=(reason or "")[:255],
        from_value="" if from_value is None else str(from_value),
        to_value="" if to_value is None else str(to_value),
    )


def _legacy_log_manual_adjustment(
    *, order_no, performance_id, actor, from_value, to_value, reason
):
    """⭐ M3a：逐字搬自 `apps/business/audit.py`（只改函数名）。

    ⚠️ 与 `_legacy_log_performance_adjust` **不是同一件事**（Ruling N）：那个写
    `PRICE_CHANGE`、改既有字段，这个写 `ADJUST`、新增一条调整记录——夹具刻意保留
    两份，别合并。
    """
    AuditLog.objects.create(
        action=AuditLog.Action.ADJUST,
        order_no=order_no,
        performance_id=performance_id,
        actor=actor or "",
        reason=(reason or "")[:255],
        from_value="" if from_value is None else str(from_value),
        to_value="" if to_value is None else str(to_value),
    )

# 迁移文件名（本任务的产物）。RED 状态下它还不存在——见 `_run_migrations_forward`。
BACKFILL_MIGRATION = "0004_backfill_from_auditlog"

#: `0004` 之后**同 app** 的迁移。退回记账时连它们一起退，`0004` 才会真的重跑
#: （理由见模块 docstring 的「为什么是重记迁移记录」一节；M2 实测）。
#: ⚠️ `0006_backfill_long_actors`（M3a）**也是**回填链的一部分：它搬 `0004` 放不下的
#: 那些行，漏登记它会让「长操作人」用例看到 0 行而误判成迁移坏了。
#: ⚠️ **只登记「回填链自己」的迁移**：链之后新增的迁移（例如审计日志线的
#: `0007_operationlog_actor_name`，纯加列）**不属于**这条链，⛔ 不要加进来——
#: 重放它会 `ProgrammingError: column … already exists`（那条列在测试库建好后一直在，
#: 而 `AddField` 没有幂等 DDL）。
MIGRATIONS_AFTER_BACKFILL = (
    "0005_alter_operationlog_actor_target_name",
    "0006_backfill_long_actors",
)

#: 正向重放时钉住的**目标节点** = 回填链的最后一环，**不是** `loader.graph.leaf_nodes()`。
#: 即便目标钉在链尾，**链尾之后的建表迁移也不能进 `MIGRATIONS_AFTER_BACKFILL`**：
#: 退回只 `record_unapplied`（只退记账）⇒ 前向重放会重跑名单里每一条，
#: `CreateModel` 重跑必报 relation already exists。
#: 名单里只放可重放的迁移（AlterField / 幂等 RunPython）。
#: ⚠️ 为什么（审计日志线实测）：`MigrationExecutor.migration_plan()` 对
#: **已应用**的目标走反向分支（同 app 没有更晚的迁移时计划为空）⇒ 本 app 的叶子变成
#: `0007_operationlog_actor_name` 之后，继续拿 `leaf_nodes()` 当目标会让
#: 「退回记账 + 正向重放」**静默什么都不做**（症状 = 回填出来 0 行，看着像迁移坏了）。
#: 钉在链尾：既让 `0004`→`0006` 真的重跑，又不必回退链之后的迁移。
#: 回填链将来延长时，加进 `MIGRATIONS_AFTER_BACKFILL` 即可，本常量自动跟着走。
FORWARD_MIGRATION_TARGET = ("audit", MIGRATIONS_AFTER_BACKFILL[-1])


def rewind_backfill_record():
    """把 `0004`（及其后的同 app 迁移）的**记账**退回（不做数据反向；理由见模块 docstring）。

    这是本文件**唯一**的退回入口：两处调用（回填用例与脏数据用例）共用同一口径，
    不会出现「一处记得退 0005、另一处忘了」的分叉。
    """
    recorder = MigrationRecorder(connection)
    for name in (BACKFILL_MIGRATION, *MIGRATIONS_AFTER_BACKFILL):
        recorder.record_unapplied("audit", name)


ORDER_NO = "20260921000000000001"
# 第二条订单：`log_status_change` 不写 `reason`，两条状态行只能靠 order_no 区分；
# 它承载「未知状态码原样保留」这条用例。
ORDER_NO_UNKNOWN_STATUS = "20260921000000000002"
FROZEN = datetime(2026, 9, 21, 10, 30, 0, tzinfo=dt_timezone.utc)


class AuditLogBackfillTests(TenantTestCase):
    """按**写入者**造行（spec §10.7），逐行断言新表的取值。"""

    def _write_legacy_rows(self):
        """用真实写入函数造 12 条旧行。

        时间用递增的冻结值（`auto_now_add` 会调 `timezone.now()`，见
        `DateTimeField.pre_save`），这样每行的 `created_at` 互不相同——
        「新行保留了各自的原值」才是一条有判别力的断言（而不是「都等于某一个常量」）。
        """
        times = iter(FROZEN + timedelta(minutes=i) for i in range(20))
        with mock.patch("django.utils.timezone.now", side_effect=lambda: next(times)):
            # ① 状态流转（STATUS_CHANGE，存的是英文状态码）
            _legacy_log_status_change(
                order_no=ORDER_NO,
                from_status="IN_PROGRESS",
                to_status="PENDING_REVIEW",
                actor="客服甲",
            )
            # ② 改订单总价（PRICE_CHANGE；reason 是调用方自由文本）
            _legacy_log_price_change(
                order_no=ORDER_NO,
                old_price=Decimal("100.00"),
                new_price=Decimal("120.00"),
                reason="客户加价",
                actor="客服乙",
            )
            # ②b **同一条改价路径**，但自由文本以绩效标签的短前缀开头。
            #     判别若写成短前缀 `接单价` 就会把这行误判成绩效调整——
            #     它其实是订单总价（真实绩效标签是 `接单价（绩效覆盖）`）。
            _legacy_log_price_change(
                order_no=ORDER_NO,
                old_price=Decimal("120.00"),
                new_price=Decimal("130.00"),
                reason="接单价太贵了",
                actor="客服乙",
            )
            # ②c 同上，但自由文本以**另一个**绩效标签短前缀开头
                        #     （形态：`扣点值算错了要改总价`）。
            _legacy_log_price_change(
                order_no=ORDER_NO,
                old_price=Decimal("130.00"),
                new_price=Decimal("140.00"),
                reason="扣点值算错了要改总价",
                actor="客服乙",
            )
            # ③ 绩效调整（PRICE_CHANGE；reason = 标签 + `｜` + 用户原因）
            _legacy_log_performance_adjust(
                order_no=ORDER_NO,
                changes=[
                    ("接单价（绩效覆盖）", Decimal("88.00"), Decimal("99.00")),
                    ("扣点模式", "按比例", "固定额"),
                ],
                actor="审核人丙",
                reason="协商",
            )
            # ④ 绩效调整且**没有**用户原因 ⇒ reason 恰好等于标签本身（没有 `｜`）
            _legacy_log_performance_adjust(
                order_no=ORDER_NO,
                changes=[("扣点值", Decimal("5.00"), Decimal("8.00"))],
                actor="审核人丙",
            )
            # ⑤ 绩效审核（REVIEW）
            _legacy_log_performance_review(
                order_no=ORDER_NO,
                old_status="待审核",
                new_status="已通过",
                actor="审核人丁",
                reason="金额无误",
            )
            # ⑥ 状态流转，但 from 是**未知状态码** ⇒ 必须原样保留、不许抛
            _legacy_log_status_change(
                order_no=ORDER_NO_UNKNOWN_STATUS,
                from_status="LEGACY_UNKNOWN",
                to_status="PENDING_REVIEW",
                actor="客服己",
            )
            # ⑦ 结账（SETTLE；M3a 新增归类 ⇒ `performance.settle` + `log_type=20`）
            _legacy_log_settle(
                order_no=ORDER_NO,
                performance_id=77,
                actor="财务庚",
                from_value="未结账",
                to_value="已结账",
                reason="9 月结账",
            )
            # ⑧ 退款（REFUND；订单级 ⇒ `order.refund` + `log_type=10`）
            _legacy_log_refund(
                order_no=ORDER_NO,
                actor="客服辛",
                from_value="0.00",
                to_value="50.00",
                reason="客户投诉进度慢",
            )
            # ⑨ 人工修正（ADJUST ⇒ `performance.adjust_manual` + `log_type=20`）
            _legacy_log_manual_adjustment(
                order_no=ORDER_NO,
                performance_id=91,
                actor="财务壬",
                from_value="-8.00",
                to_value="0.00",
                reason="手工修正坏账",
            )

    def _rewind_backfill_record(self):
        """只把 `0004`（及其后同 app 迁移，见 `MIGRATIONS_AFTER_BACKFILL`）的**记账**退回。"""
        rewind_backfill_record()

    def _run_migrations_forward(self):
        """正向重放到回填链的链尾（`FORWARD_MIGRATION_TARGET`）。

        ⚠️ 目标是**链尾**而不是 `leaf_nodes()`（理由见 `FORWARD_MIGRATION_TARGET` 的注释：
        app 叶子已应用时 `migration_plan()` 走反向分支 ⇒ 计划为空、重放静默无效）。
        ⚠️ 判据也不是写死 `("audit", BACKFILL_MIGRATION)`：RED 状态下 0004 还不存在，
        只迁到它会让后面的链（0005/0006）不跑 ⇒ 期望以「新表里没有行」失败
        （这正是本任务预期的第一个红），而不是 `NodeNotFoundError` 这种
        「测试自己写错了」的形状。
        """
        executor = MigrationExecutor(connection)
        executor.migrate([FORWARD_MIGRATION_TARGET])

    def test_legacy_rows_are_backfilled_by_writer(self):
        self._write_legacy_rows()

        # 前置非空断言：旧行确实造出来了（否则后面的断言可能是空洞通过）。
        legacy = list(AuditLog.objects.order_by("id"))
        self.assertEqual(len(legacy), 12)
        by_key = {(row.order_no, row.reason): row for row in legacy}
        self.assertEqual(len(by_key), 12, "旧行的 (order_no, reason) 应当互不相同，才能一一对位")

        self._rewind_backfill_record()
        self._run_migrations_forward()

        rows = list(OperationLog.objects.all())
        self.assertEqual(len(rows), 12, "回填应当逐行搬 12 条旧行")
        new_by_key = {(row.order_no, row.reason): row for row in rows}
        self.assertEqual(
            set(new_by_key), set(by_key), "新行应当逐条对上旧行（order_no/reason 同行保留）"
        )

        # 每条 = (order_no, 旧行 reason, 新 action, log_type, field_name,
        #          新 from_value, 新 to_value, 新 summary)
        # 期望值一律**字面值**（E-0024：不要断言 `ACTION_TEMPLATES[...]` 这类自指常量）。
        cases = [
            (
                ORDER_NO,
                "",
                "order.transition",
                10,
                "status",
                # R15：旧行存英文状态码，文案要中文（spec §5.1「与对标一致」）
                "代练中",
                "待验收",
                f"将订单 {ORDER_NO} 的状态从代练中改为待验收",
            ),
            (
                ORDER_NO,
                "客户加价",
                "order.change_taker_price",
                10,
                "total_price",
                "100.00",
                "120.00",
                "修改了订单接单价格（原订单价格：100.00元，修改后价格：120.00元）",
            ),
            (
                # 自由文本，**不是**绩效标签 ⇒ 仍是改总价
                ORDER_NO,
                "接单价太贵了",
                "order.change_taker_price",
                10,
                "total_price",
                "120.00",
                "130.00",
                "修改了订单接单价格（原订单价格：120.00元，修改后价格：130.00元）",
            ),
            (
                # 以 `扣点值` 开头的**用户自由文本**，仍是改总价
                ORDER_NO,
                "扣点值算错了要改总价",
                "order.change_taker_price",
                10,
                "total_price",
                "130.00",
                "140.00",
                "修改了订单接单价格（原订单价格：130.00元，修改后价格：140.00元）",
            ),
            (
                ORDER_NO,
                "接单价（绩效覆盖）｜协商",
                "performance.adjust",
                20,
                "taker_price",
                "88.00",
                "99.00",
                # 旧表没有「绩效用户」这一列 ⇒ target_name 为空（不拿 actor 冒充目标，不猜）
                "修改了的接单价覆盖值（原88.00，新99.00）",
            ),
            (
                ORDER_NO,
                "扣点模式｜协商",
                "performance.adjust",
                20,
                "deduct_mode",
                "按比例",
                "固定额",
                "修改了的扣点模式（原按比例，新固定额）",
            ),
            (
                ORDER_NO,
                "扣点值",  # 没有 `｜`：标签本身就是整段 reason
                "performance.adjust",
                20,
                "deduct_value",
                "5.00",
                "8.00",
                "修改了的扣点值（原5.00，新8.00）",
            ),
            (
                ORDER_NO,
                "金额无误",
                "performance.review",
                20,
                "review_status",
                # ⚠️ 审核状态的取值**本来就是中文**（`ReviewStatus` 的 value），不翻译
                "待审核",
                "已通过",
                f"审核人丁审核了订单 {ORDER_NO} 的绩效：已通过，备注：金额无误",
            ),
            (
                ORDER_NO_UNKNOWN_STATUS,
                "",
                "order.transition",
                10,
                "status",
                # 未知状态码**原样保留**（不猜、不抛）
                "LEGACY_UNKNOWN",
                "待验收",
                f"将订单 {ORDER_NO_UNKNOWN_STATUS} 的状态从LEGACY_UNKNOWN改为待验收",
            ),
            # ⭐ M3a：P4 的三个旧 action 不再落 `70 其它` + 空 summary。
            # ⚠️ `target_name` 为空是**登记过的能力边界**（旧表没有绩效归属人这一列，
            #    不拿 `actor` 冒充目标）⇒ `performance.settle` 的句子读作「修改了的…」。
            (
                ORDER_NO,
                "9 月结账",
                "performance.settle",
                20,
                "settle_status",
                "未结账",
                "已结账",
                "修改了的绩效清算状态为：已结账(仅更改结算状态)",
            ),
            (
                ORDER_NO,
                "客户投诉进度慢",
                "order.refund",
                10,
                "",
                "0.00",
                "50.00",
                f"将订单 {ORDER_NO} 的累计退款额从0.00改为50.00，备注：客户投诉进度慢",
            ),
            (
                ORDER_NO,
                "手工修正坏账",
                "performance.adjust_manual",
                20,
                "",
                "-8.00",
                "0.00",
                "新增了的绩效调整记录（变更前：-8.00，变更后：0.00），备注：手工修正坏账",
            ),
        ]

        for order_no, reason, action, log_type, field_name, from_value, to_value, summary in cases:
            old = by_key[(order_no, reason)]
            new = new_by_key[(order_no, reason)]
            with self.subTest(order_no=order_no, reason=reason):
                self.assertEqual(new.action, action)
                self.assertEqual(new.log_type, log_type)
                self.assertEqual(new.field_name, field_name)
                self.assertEqual(new.summary, summary)
                self.assertEqual(new.from_value, from_value)
                self.assertEqual(new.to_value, to_value)
                self.assertEqual(new.order_no, order_no)
                self.assertEqual(new.target_kind, "order")
                self.assertEqual(new.target_id, order_no)
                self.assertEqual(new.actor, old.actor)
                self.assertIsNone(new.actor_id, "旧表只存名字，反查不可能 ⇒ 必须留空")
                self.assertEqual(new.reason, old.reason, "reason 同行保留")
                self.assertEqual(
                    new.created_at, old.created_at, "created_at 必须保留原值（不是迁移运行时间）"
                )

        # 判据「新值 = 原值」不能只靠上面那条 `assertEqual`：若回填把 `created_at`
        # 写成了迁移运行时间，各行会得到**同一个**时间。这条断言让「十二行各不相同」
        # 也成为一个事实（旧行是递增冻结的）。
        self.assertEqual(len({row.created_at for row in rows}), 12)
        self.assertNotEqual({row.created_at for row in rows}, {FROZEN})

        # 非空洞证明：本用例真的**跑到了**这个迁移。
        self.assertIn(
            ("audit", BACKFILL_MIGRATION),
            MigrationRecorder(connection).applied_migrations(),
        )


class UnknownLegacyActionTests(TenantTestCase):
    """旧表列是 `varchar(20)` + `choices`（DB 层没有 CHECK）⇒ 脏值必须**不倒灌崩**。

    spec §9：渲染不出的旧行 `summary` 留空（**不猜**）。这里钉住「留行不留文本」：
    行照搬、`log_type` 落到 `70 其它`、`action` 保持旧 code、`summary` 空。
    ⚠️ 这条不能用四个写入函数造（它们只写得出三种 action），故直接建行——
    本用例要验的不是判别规则，是脏数据的容错。
    """

    def _backfill_from_dirty_row(self, action):
        AuditLog.objects.create(
            action=action,
            order_no=ORDER_NO,
            actor="客服戊",
            reason="历史脏值",
            from_value="A",
            to_value="B",
        )
        rewind_backfill_record()
        executor = MigrationExecutor(connection)
        executor.migrate([FORWARD_MIGRATION_TARGET])
        return OperationLog.objects.get()

    def test_unknown_action_keeps_row_with_empty_summary(self):
        row = self._backfill_from_dirty_row("SOMETHING_ELSE")
        self.assertEqual(row.action, "SOMETHING_ELSE")
        self.assertEqual(row.log_type, 70)
        self.assertEqual(row.summary, "")
        self.assertEqual(row.reason, "历史脏值")
        self.assertIsNone(row.actor_id)

    def test_dirty_action_equal_to_a_new_code_still_gets_empty_summary(self):
        """脏值**恰好等于**一个新 action code 时也不能渲染出文案。

        否则会出现「`log_type=70`（其它）+ 文案却是新动作模板」这种自相矛盾的行
        （评审 round 1 的 minor）。判据：`summary` 恒为空。
        """
        row = self._backfill_from_dirty_row("order.transition")
        self.assertEqual(row.action, "order.transition")
        self.assertEqual(row.log_type, 70)
        self.assertEqual(row.summary, "")


class LongActorBackfillTests(TenantTestCase):
    """⭐ M3a：`actor` 长于 `0004` 当时列宽（64）的旧行**不截断**地过完整条链。

    背景（M2 实测，spec §16.2 第 11 条）：`0004` 跑在 `0005` 之前，原实现把
    `AuditLog.actor`（150）原样写进 64 宽的列 ⇒ 带 65–150 字操作人的库**第一次
    `migrate` 会 `DataError` 中断**，而 `0005` 排在后面救不了。
    修法 = `0004` 只搬放得下的行、`0006`（`0005` 之后）搬其余的行。

    ## 两条用例为什么都要

    本文件的历史机制只退**记账**、不退 **schema**（见模块 docstring）⇒ 重放 `0004` 时
    物理列其实已经是 150，光靠它**重现不了** `DataError`（旧代码也能「通过」）。
    所以：

    1. `test_the_whole_chain_keeps_the_full_actor`：整条链（`0004`→`0005`→`0006`）跑完
       长行**不截断**且**恰好一行**（钉住「不丢字 + 不复制」）；
    2. `test_0004_alone_skips_the_long_row`：把物理列**真的改回 64** 再重放 `0004`
       ——旧代码在这一步会 `DataError`（这就是它在全新库上的症状），修后 `0004`
       一行都不写；随后 `0005` 加宽、`0006` 补齐。
    """

    #: 70 字：> 64（`0004` 放不下），<= 150（`0005` 之后放得下）。
    LONG_ACTOR = "长" * 70

    #: ⭐ M3b 边界夹具：**恰好 64**（== `BACKFILL_ACTOR_WIDTH`，`0004` 的 `<=` 这一侧）。
    BOUNDARY_ACTOR = "边" * 64
    #: ⭐ M3b 边界夹具：**65**（== 宽度 + 1，只能由 `0006` 的 `>` 这一侧搬）。
    OVER_BOUNDARY_ACTOR = "越" * 65

    def setUp(self):
        super().setUp()
        # 前置：本用例的库在这一刻没有回填行，才能用「行数」当判据。
        self.assertEqual(OperationLog.objects.count(), 0)

    def _rewind_and_migrate_forward(self):
        rewind_backfill_record()
        executor = MigrationExecutor(connection)
        executor.migrate([FORWARD_MIGRATION_TARGET])

    def _narrow_actor_column_to_backfill_width(self):
        """把物理列改回 `0004` 当时看到的宽度（64），重现「全新库」的真实形状。

        ⚠️ 只在**没有长行**时执行（本用例先断言表是空的）——否则 PostgreSQL 的
        `ALTER COLUMN ... TYPE varchar(64)` 自己就会因既有长值失败，那正是要防的事。
        """
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE audit_operationlog ALTER COLUMN actor TYPE varchar(64)"
            )

    def test_the_whole_chain_keeps_the_full_actor(self):
        _legacy_log_refund(
            order_no=ORDER_NO,
            actor=self.LONG_ACTOR,
            from_value="0.00",
            to_value="50.00",
            reason="长操作人退款",
        )

        self._rewind_and_migrate_forward()

        rows = list(OperationLog.objects.all())
        self.assertEqual(len(rows), 1, "长操作人那行应当**恰好**被搬一次（不丢也不复制）")
        row = rows[0]
        self.assertEqual(row.actor, self.LONG_ACTOR)
        self.assertEqual(len(row.actor), 70, "actor 被截断/丢字了")
        # 归类不受拆分影响（0006 复用 0004 的同一份 `_build`）
        self.assertEqual(row.action, "order.refund")
        self.assertEqual(row.log_type, 10)
        self.assertEqual(row.summary, f"将订单 {ORDER_NO} 的累计退款额从0.00改为50.00，备注：长操作人退款")
        # 非空洞证明：两条回填迁移都跑过了。
        applied = MigrationRecorder(connection).applied_migrations()
        self.assertIn(("audit", BACKFILL_MIGRATION), applied)
        self.assertIn(("audit", "0006_backfill_long_actors"), applied)

    def test_0004_alone_skips_the_long_row(self):
        """`0004` **单独**跑时不许碰长行——旧代码在这里会 `DataError`（M2 实测的形状）。"""
        _legacy_log_refund(
            order_no=ORDER_NO,
            actor=self.LONG_ACTOR,
            from_value="0.00",
            to_value="50.00",
            reason="长操作人退款",
        )
        # ⚠️ 顺序：先退记账（让执行器认为 `0004` 未跑），再把物理列改回 64。
        #    反过来的话执行器看到的仍是「`0005` 已应用」，`migrate` 到 `0004` 的计划为空。
        rewind_backfill_record()
        self._narrow_actor_column_to_backfill_width()

        # 只应用到 `0004`：物理列此刻是 64 —— 与全新库跑到 `0004` 时一模一样。
        executor = MigrationExecutor(connection)
        executor.migrate([("audit", BACKFILL_MIGRATION)])

        self.assertEqual(
            OperationLog.objects.count(), 0,
            "0004 把放不下的 actor 写进了 64 宽的列（旧代码的 DataError 形状）",
        )

        # 补完剩下的链：0005 加宽 + 0006 补齐 ⇒ 行仍然完整。
        executor = MigrationExecutor(connection)
        executor.migrate([FORWARD_MIGRATION_TARGET])
        row = OperationLog.objects.get()
        self.assertEqual(row.actor, self.LONG_ACTOR)

    def test_the_width_boundary_is_pinned_at_64_and_65(self):
        """⭐ M3b：64 / 65 两条旧行把两条迁移的**分界**钉在宽度常量上。

        `0004` 与 `0006` 各有一处长度判据（`<=` / `>`）。若有人把它们漂移成同一方向
        （都 `<=` 或都 `>`）或抄错数字，症状是「长行丢字」或「短行被搬两次」。
        做法同 `test_0004_alone_skips_the_long_row`：把物理列**真的改回 64**，先只跑 `0004`
        ——恰好只搬 64 字那条（65 字那条放不下）；再补完整条链——两条都在、各自完整。
        """
        _legacy_log_refund(
            order_no=ORDER_NO, actor=self.BOUNDARY_ACTOR,
            from_value="0.00", to_value="1.00", reason="边界64",
        )
        _legacy_log_refund(
            order_no=ORDER_NO_UNKNOWN_STATUS, actor=self.OVER_BOUNDARY_ACTOR,
            from_value="0.00", to_value="2.00", reason="边界65",
        )
        rewind_backfill_record()
        self._narrow_actor_column_to_backfill_width()

        executor = MigrationExecutor(connection)
        executor.migrate([("audit", BACKFILL_MIGRATION)])
        only_0004 = list(OperationLog.objects.order_by("id"))
        self.assertEqual(
            len(only_0004), 1,
            "0004 应当只搬得下 64 字那条（`<=` 这一侧），实际：%r"
            % ([r.actor for r in only_0004],),
        )
        self.assertEqual(only_0004[0].actor, self.BOUNDARY_ACTOR)
        self.assertEqual(len(only_0004[0].actor), 64)

        executor = MigrationExecutor(connection)
        executor.migrate([FORWARD_MIGRATION_TARGET])
        rows = {row.actor: row for row in OperationLog.objects.all()}
        self.assertEqual(set(rows), {self.BOUNDARY_ACTOR, self.OVER_BOUNDARY_ACTOR})
        self.assertEqual(len(rows[self.OVER_BOUNDARY_ACTOR].actor), 65, "65 字那条被截断/丢字")


class BackfillActorWidthDriftTests(SimpleTestCase):
    """⭐ M3b（评审加固）：`0004` 与 `0006` 的两条长度判据**共用同一个常量**，不许漂移。

    两条回填迁移按 `len(actor) <= / > BACKFILL_ACTOR_WIDTH` 分家（互补且覆盖全表）。
    宽度常量只在 `0004` 定义一次，`0006` 从它取（`importlib`，因为模块名以数字开头）；
    若有人在任一条里把常量抄成字面数字、或改一处忘另一处，就会出现「漏行」或「复制行」——
    而这两种症状只会在**全新的库**上暴露。所以这里把「只定义一次 + 两处都引用符号」钉住，
    再加上 `LongActorBackfillTests.test_the_width_boundary_is_pinned_at_64_and_65` 的
    64/65 行级边界夹具，两头合起来覆盖「符号没漂 + 行为在边界正确」。
    """

    def _module(self, name):
        return importlib.import_module(f"apps.audit.migrations.{name}")

    def test_the_width_constant_is_defined_once_and_imported(self):
        four = self._module("0004_backfill_from_auditlog")
        six = self._module("0006_backfill_long_actors")
        # 字面 64（`0001_initial` 建 `actor` 列时的宽度）——手写，不引模型当前列宽
        # （现在已是 150，引用它会把「冻结宽度」这件事测没了；E-0024）。
        self.assertEqual(four.BACKFILL_ACTOR_WIDTH, 64)
        # `0006` 的模块常量指向 `0004`：宽度与 `_build` 都从那里取，不复制一份。
        self.assertEqual(
            six._BACKFILL_MODULE, "apps.audit.migrations.0004_backfill_from_auditlog"
        )
        self.assertIs(six.import_module(six._BACKFILL_MODULE), four)

    def test_both_backfills_compare_against_the_shared_symbol(self):
        four = self._module("0004_backfill_from_auditlog")
        six = self._module("0006_backfill_long_actors")
        four_source = inspect.getsource(four.backfill)
        six_source = inspect.getsource(six.backfill_long_actors)
        # 0004：`<=` 一侧，直接比常量符号。
        self.assertIn("BACKFILL_ACTOR_WIDTH", four_source)
        # 0006：`>` 一侧，先把同一个符号绑到局部名 `width`（而不是另抄一个数字）。
        self.assertIn("backfill.BACKFILL_ACTOR_WIDTH", six_source)
        self.assertIn("<= width", six_source)


class InlineCopiesDriftTests(SimpleTestCase):
    """回填文案固定为迁移发布时的快照。字段和状态标签仍与当前定义对照。"""

    def _migration_module(self):
        return importlib.import_module(
            "apps.audit.migrations.0004_backfill_from_auditlog"
        )

    def test_inlined_templates_match_the_published_snapshot(self):
        module = self._migration_module()
        self.assertEqual(module._TEMPLATES, {
            "order.transition": "将订单 {order_no} 的状态从{from_value}改为{to_value}（原因：{reason}）",
            "order.change_taker_price": (
                "修改了订单接单价格（原订单价格：{from_value}元，修改后价格：{to_value}元）"
            ),
            "order.refund": "将订单 {order_no} 的累计退款额从{from_value}改为{to_value}，备注：{reason}",
            "performance.adjust": "修改了{target_name}的{field_label}（原{from_value}，新{to_value}）",
            "performance.adjust_manual": (
                "新增了{target_name}的绩效调整记录（变更前：{from_value}，变更后：{to_value}），备注：{reason}"
            ),
            "performance.review": "{actor_name}审核了订单 {order_no} 的绩效：{to_value}，备注：{reason}",
            "performance.settle": "修改了{target_name}的绩效清算状态为：{to_value}(仅更改结算状态)",
        })

    def test_inlined_field_labels_match_actions_registry(self):
        module = self._migration_module()
        self.assertEqual(set(module._FIELD_LABELS), {
            "taker_price", "deduct_mode", "deduct_value",
        })
        for field_name, label in module._FIELD_LABELS.items():
            self.assertEqual(label, actions.FIELD_LABELS[field_name], field_name)

    def test_inlined_status_labels_match_order_status_choices(self):
        module = self._migration_module()
        self.assertEqual(
            module._ORDER_STATUS_LABELS,
            {value: label for value, label in OrderStatus.choices},
        )
