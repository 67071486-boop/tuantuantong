"""补查**闲管家收款明细里金额为空**的行（收款线 spec §4-D4 的兜底）。 **为什么需要它**：22 推送里没有金额字段，金额靠回查（出站 `order/detail`，spec §12 F1-D：买家实付减已退）；推送内的回查跑在 on_commit 后的 daemon 线程里，线程是 daemon、进程重启即丢，上游抖动也会留空 ⇒ `amount IS NULL` 的行由本命令重试。（后续可挂既有定时口径；手动口径 = `manage.py backfill_xgj_receipt_amounts`。）
**`--refetch-all`（一次性；spec §12 F3-D）**：先把选中行的 `amount` / `amount_fetched_at` / `shop_identity` **全部置空**再逐行回查重写——用途是把**实弹首日的脏数据**（金额按已废弃的 `xyb_seller_amount` 口径写成的 0.00、`shop_identity` 为空）一次性重算；回查失败的行 = 金额**缺失可见**（不猜数），可再跑一次重试。日常补空不加本旗标。 ⚠️ **必须自己遍历租户**（先例 = `purge_pending_screenshots.py` 的同一条评审教训）：
`apps.business` 是 TENANT_APP，`business_xgjreceiptrecord` 只在**租户 schema** 里， django-tenants 的连接默认停在 `public` ⇒ 只查当前连接的命令会直接 `ProgrammingError`，补查根本不运行。 ⛔ **不加 `--refetch-all` 时只碰 `amount IS NULL` 的行**：已有金额的行是已回查过的事实， **一律不重查**（重查会把「当时实收」改成「现在查到的」，那是另一条需求、不在本线）。回查本体 = `xgj_push.fetch_receipt_amount`（与推送线程**同一个函数**，⛔ 不另抄一份判据）；上游失败它自己吞掉并留 warning（本命令继续下一行）。"""
from django.core.management.base import BaseCommand

from apps.business.models import XgjReceiptRecord
from apps.business.xgj_push import fetch_receipt_amount


class Command(BaseCommand):
    help = (
        "补查闲管家收款明细里金额为空的行（出站 order/detail 回查，金额=买家实付减已退；"
        "只碰 amount IS NULL 的行；--refetch-all 一次性把选中行金额/回查时间/店铺标识"
        "清空后全量重查——首日脏数据 0.00 重算用）"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema", default=None,
            help="只补查这一个租户 schema（默认：全部非 public 租户，逐个进 tenant_context）",
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help="每个租户最多补查多少行（默认不限）",
        )
        parser.add_argument(
            "--refetch-all", action="store_true",
            help=(
                "一次性（spec §12 F3-D）：先把选中行的 amount/amount_fetched_at/shop_identity "
                "置空再逐行回查重写（首日脏金额 0.00 全量重算；不加本旗标只补空金额行）"
            ),
        )

    def handle(self, *args, **options):
        from django_tenants.utils import get_public_schema_name, tenant_context

        from apps.platform.models import Tenant

        schema_filter = options.get("schema")
        limit = options.get("limit")
        refetch_all = options.get("refetch_all", False)
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if schema_filter:
            tenants = tenants.filter(schema_name=schema_filter)
        total_refilled = 0
        total_scanned = 0
        total_cleared = 0
        for tenant in tenants:
            with tenant_context(tenant):
                if refetch_all:
                    # ⚠️ 先按**同一份行集**选中 → 清空 → 再逐行回查：清空的恰好是马上要
                    #    回查的那些行（--limit 也作用在同一份上，不会「清了不查」）。
                    ids = list(
                        XgjReceiptRecord.objects.order_by("id").values_list("id", flat=True)
                    )
                    if limit is not None:
                        ids = ids[: max(limit, 0)]
                    total_cleared += XgjReceiptRecord.objects.filter(id__in=ids).update(
                        amount=None, amount_fetched_at=None, shop_identity=None
                    )
                    rows = XgjReceiptRecord.objects.filter(id__in=ids).order_by("id")
                else:
                    rows = XgjReceiptRecord.objects.filter(
                        amount__isnull=True
                    ).order_by("id")
                    if limit is not None:
                        rows = rows[: max(limit, 0)]
                for record in rows:
                    total_scanned += 1
                    if fetch_receipt_amount(record.pk):
                        total_refilled += 1
            self.stdout.write(
                f"schema={tenant.schema_name} done"
            )
        cleared_note = f"，清空待重查 {total_cleared} 行" if refetch_all else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"补查完成：扫描 {total_scanned} 行，回填金额 {total_refilled} 行{cleared_note}"
            )
        )
