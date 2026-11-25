"""`prune_audit_tables`：请求性能指标 + 操作日志两张审计表的**保留期清理**（D-0370 批）。 python manage.py prune_audit_tables                  # **dry-run**（默认）：只报数，一个字节都不删 python manage.py prune_audit_tables --apply          # 真删（仍逐租户、仍只删命中的行、**分批**删） python manage.py prune_audit_tables --tenant <schema> [--apply] python manage.py prune_audit_tables --days 14 --apply      # 显式保留期（两表同用；排障口径） ## 表与保留期（D-0370 批，2026-11-03） - `audit_requestmetric`（`RequestMetric`）：`bucket_start < 截止` 即删——纯性能指标，
页面时间范围筛选最多看几天，30 天远超口径（生产最大租户实测 26.8 万行 / 87MB，只增不减）； - `audit_operationlog`（`OperationLog`）：`created_at < 截止` 即删——操作留痕页与订单时间线「操作留痕」节的读面默认窗是 30 天量级（与台账 `JidanpuSyncEvent` 的 D-0349 保留期同一拍板口径）； - **默认 30 天**（两表同用）；`--days N` 显式给出时两表同用 N（排障口径）； - 两张表都没有「证据行豁免」——保留线是业务方拍板（30 天）。⚠️ **读方如实登记** （⛔ 不说「唯一消费方是审计页」）：操作留痕页 / 订单时间线操作节 / 订单详情日志（`api/order_views.py`）、统计（`app_stats.py` 的接单分布）、回填命令（`backfill_hall_publisher` 从人工指派日志取最新一条）也读 `OperationLog` ——超期行删掉后，这些读面对**超期单**的取数自然变薄（回填命令对超期单少一条
候选来源），属拍板接受的代价。⛔ 没有任何「时间 / 大小 / 前缀兜底」的第二把刷子：候选 WHERE 只有「超保留期」这一条。 ## 纪律（AGENTS.md §5-A 破坏性动作三条；形状照 `prune_sync_events`） 1. **默认 dry-run 只报数**：不 `--apply` 就什么都不删；`--dry-run` 与 `--apply` 同给时以 dry-run 为准（零写入优先，矛盾时不猜）； 2. **删除前同 WHERE 打命中数**：count 与 delete 用**同一个 queryset**（`_metric_candidates` / `_log_candidates`），⛔ 不许「报数一套条件、删除另一套」； 3. **分批删除**（照 D-0349）：每批 `BATCH_SIZE` 条、循环到删完，批与批各自提交—— ⛔ 不许一把巨量 DELETE（长事务 + 长锁）。每一批的 DELETE 都带**同一把候选 WHERE** 再 `pk__in` 收口。 ## 运行形态 - **逐租户循环**（两张表都在 `TENANT_APPS` ⇒ 租户 schema 各一份，public 没有这两张表）： `Tenant` 排除 public → 逐个 `tenant_context`；`--tenant` 是排障闸门； - 逐租户独立异常边界：一个租户失败不挡别的租户；有未预期失败 ⇒ 退出码 1； - 调度：`tuantuantong-audit-prune.timer` 每天 03:45±5min 触发一次 `--apply` （与台账清理 03:30 / 每日报份 04:40 错峰），四处接线由 `src/tests/test_deploy_sync_units.py::AuditPruneTimerUnitTests` 守。"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max, Min
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.audit.models import OperationLog, RequestMetric
from apps.platform.models import Tenant

#: 两张审计表的默认保留期（天）。30 天 = 业务方拍板（D-0370 批，与台账 D-0349 同口径）。
#: timer 不带 `--days` 跑的就是它。要改保留期先改拍板，⛔ 不改这里。
DEFAULT_RETENTION_DAYS = 30

#: `--days` 的下限：0 = 「即删」没有任何审计可看，与「保留期」语义矛盾。保守地板。
MIN_RETENTION_DAYS = 1

#: 分批删除的批大小（照 prune_sync_events 的 D-0349 判据：不长事务 / 不长锁）。
BATCH_SIZE = 5000


def _fmt_time(value):
    """候选集时间窗的展示值（运维读的；空集 / 空值 → "—"）。"""
    if value is None:
        return "—"
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M")


class Command(BaseCommand):
    help = (
        "清理两张审计表（RequestMetric / OperationLog）：默认 dry-run 只报数，"
        "--apply 才删；超过保留期（缺省两表各 30 天，--days 显式覆盖）即删、"
        "分批删除；无任何证据行豁免（两表是纯审计面，无业务判据回读旧行）"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="真删（⛔ 缺省是 dry-run：只报命中数，一个字节都不删）",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="强制只报数（缺省本就是 dry-run；与 --apply 同给时以 dry-run 为准——零写入优先）",
        )
        parser.add_argument(
            "--days", type=int, default=None,
            help="保留期（天）：显式给出时两张表同用（排障口径）；"
                 f"缺省 {DEFAULT_RETENTION_DAYS} 天",
        )
        parser.add_argument(
            "--tenant", default=None,
            help="只跑一个租户 schema（排障）；不在库里 ⇒ 响亮报错",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days is not None and days < MIN_RETENTION_DAYS:
            raise CommandError(f"--days 至少 {MIN_RETENTION_DAYS}（收到 {days}）")
        schema = (options.get("tenant") or "").strip() or None
        apply_changes = bool(options.get("apply")) and not bool(options.get("dry_run"))
        now = timezone.now()
        retention_days = days if days is not None else DEFAULT_RETENTION_DAYS
        cutoff = now - timedelta(days=retention_days)

        unexpected = False
        deleted_metric_total = deleted_log_total = 0
        for tenant in self._iter_tenants(schema):
            try:
                with tenant_context(tenant):
                    metric_candidates = self._metric_candidates(cutoff)
                    log_candidates = self._log_candidates(cutoff)
                    metric_count = metric_candidates.count()
                    log_count = log_candidates.count()
                    metric_window = self._oldest_newest(metric_candidates)
                    log_window = self._oldest_newest(log_candidates)
                    if apply_changes:
                        # ⚠️ count 与 delete 用**同一个 queryset**（同一把 WHERE），
                        #    ⛔ 不许报数一套、删除另一套（§5-A 破坏性动作纪律）。
                        deleted_metric = self._delete_in_batches(metric_candidates)
                        deleted_log = self._delete_in_batches(log_candidates)
                    else:
                        deleted_metric = deleted_log = 0
                    deleted_metric_total += deleted_metric
                    deleted_log_total += deleted_log
                self.stdout.write(
                    f"{tenant.schema_name}: requestmetric 命中 {metric_count} 条"
                    f"（最老 {_fmt_time(metric_window[0])} · 最新 {_fmt_time(metric_window[1])}）"
                    f" · operationlog 命中 {log_count} 条"
                    f"（最老 {_fmt_time(log_window[0])} · 最新 {_fmt_time(log_window[1])}）"
                    + (
                        f" ⇒ 已删 metric {deleted_metric} / log {deleted_log}"
                        if apply_changes else "（dry-run，未删除）"
                    )
                )
            except Exception as exc:  # noqa: BLE001 —— 逐租户异常边界（照 prune_sync_events）
                unexpected = True
                # ⛔ 只写异常类型名：str(exc) 可能带载荷片段（spec §11-S1 同族）。
                self.stderr.write(
                    f"{tenant.schema_name}: 本租户审计表清理失败（{type(exc).__name__}），"
                    "下一个租户继续"
                )
        mode = "已删除" if apply_changes else "dry-run 未删除"
        self.stdout.write(
            f"合计（{mode}）：requestmetric {deleted_metric_total} 条 · "
            f"operationlog {deleted_log_total} 条 · 保留期 {retention_days} 天 · "
            f"分批 {BATCH_SIZE} 条/批"
        )
        if unexpected:
            raise CommandError("有租户的审计表清理出现未预期失败，详见上方", returncode=1)

    # ── 跨 schema 枚举（照 prune_sync_events 的既有模式）────────────────────
    def _iter_tenants(self, schema):
        """本命令这一趟要跑的租户（只读平台面的 `Tenant`）。 ⚠️ `--tenant` 是**排障闸门**（⛔ 不提供 `--schema` 别名：两个开关会变成
        「一个没有文档的别名」，照 prune_sync_events 的同一条裁定）。timer 每台机跑的
        就是本机 `Tenant` 表里的租户（与备份「own tenant DB」同一口径）。"""
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if schema:
            tenants = tenants.filter(schema_name=schema)
            if not tenants.exists():
                raise CommandError(f"没有这个租户 schema：{schema}")
        return list(tenants.order_by("schema_name"))

    # ── 候选集（count 与 delete 共用；⛔ 别在调用方再写一份 WHERE）─────────────
    @staticmethod
    def _metric_candidates(cutoff):
        """`RequestMetric` 的可删候选：`bucket_start` 超保留期（无状态、无豁免）。"""
        return RequestMetric.objects.filter(bucket_start__lt=cutoff)

    @staticmethod
    def _log_candidates(cutoff):
        """`OperationLog` 的可删候选：`created_at` 超保留期（无状态、无豁免）。"""
        return OperationLog.objects.filter(created_at__lt=cutoff)

    @staticmethod
    def _oldest_newest(queryset):
        """候选集的（最老, 最新）时刻（报告用；空集两个都是 None）。"""
        if queryset.model is RequestMetric:
            window = queryset.aggregate(oldest=Min("bucket_start"), newest=Max("bucket_start"))
        else:
            window = queryset.aggregate(oldest=Min("created_at"), newest=Max("created_at"))
        return window["oldest"], window["newest"]

    @staticmethod
    def _delete_in_batches(queryset):
        """**分批**删除候选集（每批 `BATCH_SIZE` 条、循环到删完），返回删除总数。
        每一批 = 先按候选集 WHERE 取一批 pk，再**同一把 WHERE + `pk__in`** 删
        （照 prune_sync_events 的 D-0349 形状：批与批各自提交、删 0 行即收口）。"""
        total = 0
        while True:
            batch_ids = list(
                queryset.order_by("pk").values_list("pk", flat=True)[:BATCH_SIZE]
            )
            if not batch_ids:
                return total
            deleted = queryset.filter(pk__in=batch_ids).delete()[0]
            if deleted == 0:
                return total
            total += deleted
