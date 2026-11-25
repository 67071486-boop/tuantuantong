"""`prune_audit_tables`（审计表 30 天保留线，D-0370 批）的判据网。

钉住的契约（判据出处 = 命令模块 docstring「纪律」一节，形状照 `prune_sync_events` 网）：

- **默认干跑只报数**：不 `--apply` 一个字节都不删；`--dry-run` 与 `--apply` 同给时
  dry-run 赢；
- **按显式 WHERE 删**（§5-A 破坏性动作纪律）：候选 = 「超保留期」这一条，两表各按
  自己的时刻列（`bucket_start` / `created_at`），无状态、无豁免、⛔ 无时间大小兜底；
- **30 天边界**：恰好压线的行保留（WHERE 是严格 `<`），更老的删；
- `--days` 显式覆盖两表；`--days 0` 拒绝；未知租户 `CommandError`；
- 幂等：`--apply` 重跑删 0。
"""
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import LogType, OperationLog, RequestMetric

#: 冻结的「现在」：保留线是**严格 `<`**，压线行的存亡判据经不起真实时钟的亚秒漂移
#: （命令跑得比夹具晚 ⇒ 压线行会被判成超期）⇒ 命令的时钟一并钉死（见 _frozen_tz）。
FIXED_NOW = timezone.now()
OLD = FIXED_NOW - timedelta(days=40)
NEW = FIXED_NOW - timedelta(days=2)
#: 恰好压在 30 天线上（WHERE 是严格 `<` ⇒ 这一行必须活）。
BOUNDARY = FIXED_NOW - timedelta(days=30)


class _FrozenTZ:
    """命令模块里 `timezone` 的替身：`now()` 钉死、其余转发真模块。"""

    @staticmethod
    def now():
        return FIXED_NOW

    localtime = staticmethod(timezone.localtime)


def _run_prune(*args) -> str:
    from apps.business.management.commands import prune_audit_tables as cmd

    buf = StringIO()
    with mock.patch.object(cmd, "timezone", _FrozenTZ):
        call_command("prune_audit_tables", *args, stdout=buf)
    return buf.getvalue()


def _metric(kind, *, bucket_start):
    row = RequestMetric.objects.create(
        kind=kind, bucket_start=timezone.now(),
        route="/api/orders/", method="GET", status_code=200,
    )
    RequestMetric.objects.filter(pk=row.pk).update(bucket_start=bucket_start)
    return row


def _log(action, *, created_at):
    row = OperationLog.objects.create(
        log_type=LogType.SYSTEM, action=action, actor="合成", summary="合成留痕",
    )
    OperationLog.objects.filter(pk=row.pk).update(created_at=created_at)
    return row


class PruneAuditTablesTests(TenantTestCase):
    def setUp(self):
        # 先记两表的本底（命令会扫全租户表、同事务里框架/信号也可能写行），
        # 判据一律打在「本用例自己造的行的存亡 / 增量」上，⛔ 不数全表绝对值。
        self._base_metric = RequestMetric.objects.count()
        self._base_log = OperationLog.objects.count()
        # metric：老 2 行删、压线 1 行活、新 1 行活（唯一键含 kind/bucket ⇒ 各给独值）。
        self._old_metrics = [
            _metric("agg", bucket_start=OLD),
            _metric("slow", bucket_start=OLD),
        ]
        self._boundary_metric = _metric("agg", bucket_start=BOUNDARY)
        self._fresh_metric = _metric("agg", bucket_start=NEW)
        # log：老 2 行删、压线 1 行活、新 1 行活。
        self._old_logs = [
            _log("old.a", created_at=OLD),
            _log("old.b", created_at=OLD),
        ]
        self._boundary_log = _log("boundary.a", created_at=BOUNDARY)
        self._fresh_log = _log("fresh.a", created_at=NEW)

    def _alive_metrics(self):
        return RequestMetric.objects.count() - self._base_metric

    def _alive_logs(self):
        return OperationLog.objects.count() - self._base_log

    # ── 干跑（默认）─────────────────────────────────────────────────────
    def test_dry_run_is_the_default_and_deletes_nothing(self):
        out = _run_prune()
        self.assertEqual(self._alive_metrics(), 4, "metric 四行（两老/压线/新）全活")
        self.assertEqual(self._alive_logs(), 4)
        self.assertIn("dry-run", out)
        self.assertIn("命中 2 条", out)

    def test_explicit_dry_run_flag_wins_over_apply(self):
        out = _run_prune("--apply", "--dry-run")
        self.assertEqual(self._alive_metrics(), 4)
        self.assertEqual(self._alive_logs(), 4)

    # ── --apply ─────────────────────────────────────────────────────────
    def test_apply_deletes_exactly_the_expired_rows(self):
        _run_prune("--apply")
        # 老两行删、压线行与新行活（两表同判；⚠️ 判据打在**本用例自己造的行**的存亡上，
        # ⛔ 不数全表——同一事务里框架/信号可能写别的行）。
        self.assertEqual(self._alive_metrics(), 2)
        self.assertEqual(self._alive_logs(), 2)
        self.assertTrue(
            RequestMetric.objects.filter(pk=self._boundary_metric.pk).exists(),
            "压线行必须活（WHERE 是严格 <）",
        )
        self.assertTrue(RequestMetric.objects.filter(pk=self._fresh_metric.pk).exists())
        self.assertFalse(
            RequestMetric.objects.filter(pk__in=[m.pk for m in self._old_metrics]).exists(),
            "超保留期的行必须删",
        )
        self.assertTrue(OperationLog.objects.filter(pk=self._boundary_log.pk).exists())
        self.assertTrue(OperationLog.objects.filter(pk=self._fresh_log.pk).exists())
        self.assertFalse(
            OperationLog.objects.filter(pk__in=[row.pk for row in self._old_logs]).exists()
        )

    def test_apply_is_idempotent_rerun_deletes_zero(self):
        _run_prune("--apply")
        out = _run_prune("--apply")
        self.assertEqual(self._alive_metrics(), 2)
        self.assertEqual(self._alive_logs(), 2)
        self.assertIn("已删 metric 0 / log 0", out)

    # ── 保留期参数 ──────────────────────────────────────────────────────
    def test_days_parameter_moves_the_window(self):
        _run_prune("--apply", "--days", "60")
        # 60 天线 ⇒ 40 天的老行全在窗内，零删。
        self.assertEqual(self._alive_metrics(), 4)
        self.assertEqual(self._alive_logs(), 4)

    def test_days_below_one_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("prune_audit_tables", "--days", "0")

    def test_an_unknown_tenant_schema_raises_command_error(self):
        with self.assertRaises(CommandError):
            call_command("prune_audit_tables", "--tenant", "no-such-schema")
