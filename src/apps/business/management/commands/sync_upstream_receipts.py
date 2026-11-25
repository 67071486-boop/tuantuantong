"""`sync_upstream_receipts`：上家收款同步的**夜间**命令（D-0359）。 python manage.py sync_upstream_receipts               # 全部本机租户 python manage.py sync_upstream_receipts --tenant <s>  # 只跑一个租户（排障） ## 它做什么逐租户跑 **①式 + peer** 两通道的「已完成 + 未收款 → 问上家 → 翻已结款」（`apps/business/upstream_receipt_sync.run_upstream_receipt_sync(nightly=True)`：
近 90 天完成窗、放弃计数过滤、相邻两单垫 `RECEIPT_SYNC_INTERVAL_SECONDS`）。 ⚠️ **闲管家通道不进夜间**（其候选口径不筛「已完成」，接进来违背拍板③—— 计划 §2.5 的 M1，交业务方复核；手动按钮里才含它）。 ## 租户闸与关闭口径（三层，进 RUNBOOK「上家收款同步」节） 1. **机器**：`RECEIPT_SYNC_TIMER_ENABLE`（默认 1，`$R/shared/receipt-sync-unit.env` 持久） ——关了 timer 整台机器不跑； 2. **租户**：`ReceiptSyncConfig.enabled`（本命令读侧 `receipt_sync_enabled()`，
**无行 = 视为启用**、默认 True——业务方要全租户默认生效，与查码先例有意相反）； 3. **来源**：绑定停用（`enabled=False` / 已软删 / 未配查询密码 / http 形态）—— 该绑定名下候选整批不进队。 ## 运行形态（照 `prune_sync_events` 的逐租户骨架） - 命令自己**枚举本机 `Tenant`**（与备份「own tenant DB」同一口径）； - 逐租户独立异常边界：一个租户失败不挡别的租户；有未预期失败 ⇒ 退出码 1； - 同一时刻一个租户只有一个「上家收款同步」在跑（advisory lock namespace 与手动按钮共用 ⇒ 手动在跑时该租户本轮跳过，摘要 `ran=false`）； - 结尾每租户一行摘要（候选 / 翻状态 / 查无 / 错误 / 排除计数）。"""
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.business import upstream_receipt_sync
from apps.platform.models import Tenant


class Command(BaseCommand):
    help = (
        "上家收款同步（夜间）：逐租户跑①式 + peer 两通道，已完成未收款的订单逐单问上家、"
        "已结账的翻成已结款；闲管家通道只在手动按钮里跑；租户闸 ReceiptSyncConfig.enabled"
        "（无行 = 启用）"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant", default=None,
            help="只跑一个租户 schema（排障）；不在库里 ⇒ 响亮报错",
        )

    def handle(self, *args, **options):
        schema = (options.get("tenant") or "").strip() or None
        unexpected = False
        ran = disabled = skipped_busy = 0
        for tenant in self._iter_tenants(schema):
            try:
                with tenant_context(tenant):
                    if not upstream_receipt_sync.receipt_sync_enabled():
                        disabled += 1
                        self.stdout.write(
                            f"{tenant.schema_name}: 租户闸关闭（ReceiptSyncConfig.enabled=False）"
                            "⇒ 本租户零上游调用"
                        )
                        continue
                    summary = upstream_receipt_sync.run_upstream_receipt_sync(nightly=True)
                    if (
                        summary.get("jidanpu", {}).get("unexpected_error")
                        or summary.get("peer", {}).get("unexpected_error")
                    ):
                        # ⭐ M1-3：通道级边界让另一通道照跑了，但该租户
                        #    这一轮**不算干净** ⇒ 与租户级未预期同语义（退出码 1），⛔ 不静默。
                        unexpected = True
                if not summary.get("ran"):
                    skipped_busy += 1
                    self.stdout.write(
                        f"{tenant.schema_name}: 另一轮上家收款同步正在运行 ⇒ 本轮跳过"
                    )
                    continue
                ran += 1
                self.stdout.write(
                    f"{tenant.schema_name}: 夜间上家收款完成"
                    f" ①式[{upstream_receipt_sync._compact_channel(summary.get('jidanpu'))}]"
                    f" peer[{upstream_receipt_sync._compact_channel(summary.get('peer'))}]"
                )
            except Exception as exc:  # noqa: BLE001 —— 逐租户异常边界（照 prune_sync_events）
                unexpected = True
                # ⛔ 只写异常类型名：str(exc) 可能带载荷 / 凭据片段（spec §11-S1 同族）。
                self.stderr.write(
                    f"{tenant.schema_name}: 本租户上家收款同步失败（{type(exc).__name__}），"
                    "下一个租户继续"
                )
        self.stdout.write(
            f"合计：跑完 {ran} 个租户 · 租户闸关闭 {disabled} · 在跑跳过 {skipped_busy}"
            "（闲管家通道只在手动按钮里跑）"
        )
        if unexpected:
            raise CommandError("有租户的上家收款同步出现未预期失败，详见上方", returncode=1)

    # ── 跨 schema 枚举（照 prune_sync_events._iter_tenants 的既有模式）──────────
    def _iter_tenants(self, schema):
        """本命令这一趟要跑的租户（只读平台面的 `Tenant`）。
        ⚠️ `--tenant` 是排障闸门；timer 每台机跑的就是本机 `Tenant` 表里的租户
        （本机的平台面只登记本机服务的租户）。"""
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if schema:
            tenants = tenants.filter(schema_name=schema)
            if not tenants.exists():
                raise CommandError(f"没有这个租户 schema：{schema}")
        return list(tenants.order_by("schema_name"))
