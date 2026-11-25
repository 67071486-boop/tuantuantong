"""回填订单的新老客标签（2026-11-25）。

同来源 + 同客户按 `created_at, id` 的首单 `new`、其余 `old`；保留手动标签，分批、幂等、可续跑，
stdout 只出计数不出 PII。用法：`manage.py backfill_customer_kind [--schema=<租户>] [--batch-size=500]`。
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.business.models import CustomerKind, Order
from apps.business.order_intake import _phone_keys
from apps.platform.models import Tenant

DEFAULT_BATCH_SIZE = 500


class Command(BaseCommand):
    help = (
        "回填订单的新老客标签（按来源+客户分组，组内首单 new、其余 old；"
        "保留手动标签，分批、幂等、可续跑）"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema",
            default=None,
            help="只处理这一个租户 schema（默认：全部非 public 租户，逐个进 tenant_context）",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=f"每批 bulk_update 的行数（默认 {DEFAULT_BATCH_SIZE}）",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if batch_size < 1:
            raise CommandError("--batch-size 必须是正整数")

        only = options["schema"]
        if only:
            # 单 schema 档直接切当前连接：共享 Web / 租户机环境里没有中央 Tenant 表，
            # 不能靠 Tenant.objects 过滤（--schema 是这些机器上的唯一用法）。
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM pg_namespace WHERE nspname = %s", [only]
                )
                if cursor.fetchone() is None:
                    raise CommandError(f"没有这个 schema：{only}")
            previous = connection.schema_name
            connection.set_schema(only)
            try:
                counts = self._run_for_tenant(batch_size=batch_size)
            finally:
                connection.set_schema(previous)
            self.stdout.write(self._line(only, counts))
            self.stdout.write(self._summary(counts))
            return None

        tenants = Tenant.objects.exclude(
            schema_name=get_public_schema_name()
        ).order_by("schema_name")

        totals = {
            "scanned": 0,
            "updated_new": 0,
            "updated_old": 0,
            "preserved_manual": 0,
            "preserved_existing": 0,
            "written": 0,
            "batches": 0,
        }
        for tenant in tenants:
            # 订单在租户 schema 里 ⇒ 必须逐个进 tenant_context（同 backfill_hall_publisher）。
            with tenant_context(tenant):
                counts = self._run_for_tenant(batch_size=batch_size)
            self.stdout.write(self._line(tenant.schema_name, counts))
            for key in totals:
                totals[key] += counts[key]

        self.stdout.write(self._summary(totals))
        return None

    def _summary(self, totals):
        return (
            f"汇总：扫描 {totals['scanned']} 单；回填 新客 {totals['updated_new']} / "
            f"老客 {totals['updated_old']}；手动保留 {totals['preserved_manual']}；"
            f"已有标签 {totals['preserved_existing']}；本次写入 {totals['written']} "
            f"（{totals['batches']} 批）"
        )

    def _run_for_tenant(self, *, batch_size):
        counts = {
            "scanned": 0,
            "updated_new": 0,
            "updated_old": 0,
            "preserved_manual": 0,
            "preserved_existing": 0,
            "written": 0,
            "batches": 0,
        }
        # 默认 manager 已排除软删单；有来源即可，身份用「客户档案 OR 电话」。
        rows = (
            Order.objects.filter(order_source__isnull=False)
            .order_by("order_source_id", "created_at", "id")
            .only(
                "pk",
                "order_source_id",
                "customer_id",
                "customer_phone",
                "customer_kind",
                "customer_kind_is_manual",
                "split_id",
                "created_at",
            )
        )
        seen_customers = set()
        seen_phones = set()
        current_source = None
        current_event = None
        event_rows = []
        batch = []

        def flush_batch():
            if not batch:
                return
            Order.objects.filter(pk__in=[row.pk for row in batch]).bulk_update(
                batch, ["customer_kind"]
            )
            counts["written"] += len(batch)
            counts["batches"] += 1
            batch.clear()

        def flush_event():
            if not event_rows:
                return
            customers = {row.customer_id for row in event_rows if row.customer_id}
            phones = set()
            for row in event_rows:
                phones.update(_phone_keys(row.customer_phone))
            has_identity = bool(customers or phones)
            is_old = bool((customers & seen_customers) or (phones & seen_phones))
            existing_kind = next(
                (row.customer_kind for row in event_rows if row.customer_kind), ""
            )
            for row in event_rows:
                counts["scanned"] += 1
                if row.customer_kind_is_manual:
                    counts["preserved_manual"] += 1
                elif row.customer_kind:
                    counts["preserved_existing"] += 1
                elif has_identity:
                    row.customer_kind = existing_kind or (
                        CustomerKind.OLD if is_old else CustomerKind.NEW
                    )
                    if row.customer_kind == CustomerKind.OLD:
                        counts["updated_old"] += 1
                    else:
                        counts["updated_new"] += 1
                    batch.append(row)
                    if len(batch) >= batch_size:
                        flush_batch()
            seen_customers.update(customers)
            seen_phones.update(phones)
            event_rows.clear()

        for order in rows.iterator(chunk_size=batch_size):
            if order.order_source_id != current_source:
                flush_event()
                current_source = order.order_source_id
                current_event = None
                seen_customers.clear()
                seen_phones.clear()
            event = ("split", order.split_id) if order.split_id else ("order", order.pk)
            if event != current_event:
                flush_event()
                current_event = event
            event_rows.append(order)
        flush_event()
        flush_batch()
        return counts

    def _line(self, schema_name, counts):
        return (
            f"[{schema_name}] 扫描 {counts['scanned']} 单："
            f"新客 {counts['updated_new']} / 老客 {counts['updated_old']}；"
            f"手动保留 {counts['preserved_manual']}；"
            f"已有标签 {counts['preserved_existing']}；"
            f"本次写入 {counts['written']}（{counts['batches']} 批）"
        )
