"""清理 `RequestMetric` 里超过保留期的行（spec `docs/plans/2026-10-04-request-metrics.md` §6）。`agg` 行按「小时 × 接口 × 状态码 × 人」有界，而 `slow` 行**随慢请求条数增长**（spec §8 代价 2）⇒ 业务方定的是自动留 90 天（spec §1），没有这道清理 `slow` 行只增不减。
判据（`AGENTS.md` §5-A 破坏性动作硬规矩）：先用**与删除完全同一条 `WHERE`** 跑一次 `SELECT` 打出命中行数；命中 **0** 行**就地结束**——⛔ 不换成更宽的条件、⛔ 不拿时间 / 大小 / 前缀当兜底（踩过 `E-0181`：一句 `interval '1 day'` 删光全部登录会话）；删除本身**分批**（默认每批 1000 行），⛔ 不用一条 `DELETE` 扫全表。保留期的判据列 = `bucket_start`（数据自己的时间：`agg` 是整点、`slow` 是那次请求的时刻，有单列索引 ⇒ 范围扫描走索引）——⛔ 不用 `created_at`：UPSERT 命中已有 `agg` 行时它不变，按它算保留期会把「一直在被累加的行」误判成新行、永远清不掉。
必须自己遍历租户（与 `purge_pending_screenshots` 同族）：`apps.audit` 在 `TENANT_APPS` 里 ⇒ `audit_requestmetric` 只在**租户 schema** 里，而 django-tenants 的连接默认停在 `public`（`django_tenants/postgresql_backend/base.py`）⇒ 只查当前连接的命令会直接 `ProgrammingError: relation "audit_requestmetric" does not exist`、清理根本不运行。⚠️ 本命令不吞异常：任何失败都让它响亮地非 0 退出（`deploy/activate.sh` 在 `set -e` 之下当场中止发版、`current` 软链不动）。⚠️ 注释与常量不写「路径加行号」引用：`src/tests/test_doc_line_references.py` 把 `src/apps/**` 注释里的那种形状算进冻结语料（写一条就要重取那张网）。
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.audit.models import RequestMetric

#: 保留天数（spec §1 业务方口径「自动留 90 天」）。`--days` 可覆盖，下界 1。
DEFAULT_RETENTION_DAYS = 90

#: 单批删除的行数上界（「分批删」的批大小）。`--batch-size` 可覆盖，下界 1。
DEFAULT_BATCH_SIZE = 1000


class Command(BaseCommand):
    help = "清理超过保留期的请求性能指标（默认 90 天；先枚举命中行数、再分批删）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=DEFAULT_RETENTION_DAYS,
            help=f"保留天数（默认 {DEFAULT_RETENTION_DAYS}；下界 1）",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只跑那条枚举 SELECT、打印命中行数，一行都不删",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=f"每批删除的行数（默认 {DEFAULT_BATCH_SIZE}；下界 1）",
        )
        parser.add_argument(
            "--schema",
            default=None,
            help="只清理这一个租户 schema（默认：全部非 public 租户，逐个进 tenant_context）",
        )

    def handle(self, *args, **options):
        from django_tenants.utils import get_public_schema_name, tenant_context

        from apps.platform.models import Tenant

        days = options["days"]
        if days < 1:
            # ⛔ `--days 0` 会把刚落库的行也划进「过期」（cutoff = 现在）⇒ 运维脚枪，直接拒。
            raise CommandError("--days 必须 ≥ 1（0 会把刚写下的行也当过期）")
        batch_size = options["batch_size"]
        if batch_size < 1:
            # 批大小 0 会让「取一批 pk」永远取空 / 死循环 ⇒ 直接拒。
            raise CommandError("--batch-size 必须 ≥ 1")

        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        only = options["schema"]
        if only is not None:
            tenants = tenants.filter(schema_name=only)
            if not tenants.exists():
                raise CommandError(f"没有这个 schema：{only}")

        matched_total = 0
        deleted_total = 0
        for tenant in tenants.order_by("schema_name"):
            # ⚠️ 必须逐个进 tenant_context：租户表只在各自的 schema 里（见模块 docstring）。
            with tenant_context(tenant):
                matched, deleted = self._purge_schema(
                    tenant.schema_name, days, batch_size, options["dry_run"]
                )
            matched_total += matched
            deleted_total += deleted

        if options["dry_run"]:
            self.stdout.write(f"合计：命中 {matched_total} 行（dry-run，未删任何行）")
        else:
            self.stdout.write(f"合计：删除 {deleted_total} 行")

    def _purge_schema(self, schema_name, days, batch_size, dry_run):
        """清理一个 schema；返回 `(命中行数, 删除行数)`。"""
        cutoff = timezone.now() - timedelta(days=days)
        # ⚠️ 这一个 queryset 就是**枚举与删除共用的同一条 WHERE**（§5-A 的判据载体）：
        #    下面的分批删除每一批都由它派生，⛔ 不换条件、不兜底。
        stale = RequestMetric.objects.filter(bucket_start__lt=cutoff)
        matched = stale.count()
        self.stdout.write(
            f"[{schema_name}] 枚举：bucket_start < {cutoff.isoformat()} 命中 {matched} 行"
        )
        if matched == 0:
            # ⛔ 命中 0 行就地结束：不换更宽的条件再试（`AGENTS.md` §5-A / `E-0181`）。
            self.stdout.write(f"[{schema_name}] 命中 0 行 ⇒ 不删除、不放宽条件")
            return matched, 0
        if dry_run:
            self.stdout.write(f"[{schema_name}] [dry-run] 命中 {matched} 行，未删除任何行")
            return matched, 0

        deleted = 0
        while True:
            pks = list(stale.order_by("pk").values_list("pk", flat=True)[:batch_size])
            if not pks:
                break
            rows, _ = RequestMetric.objects.filter(pk__in=pks).delete()
            if rows == 0:
                # 防御：刚枚举到的 pk 不可能一行都删不掉；真发生说明有别的写者/删者插进来
                # ⇒ 停下来，⛔ 不让循环原地打转。
                break
            deleted += rows
        self.stdout.write(f"[{schema_name}] 删除 {deleted} 行（保留期 {days} 天）")
        return matched, deleted
