"""清理**超过 24 小时仍未被认领**的订单截图（spec §6.3 的孤儿回收）。 **为什么需要它**：直传是「先传文件、后落库」——用户传完就关页面时，对象已经在 COS 里、而业务行还停在 `pending`。本线定了**永久保留、没有 90 天兜底**（spec A3）⇒ 没有这道清理，垃圾会一直攒着。对标用「1 天临时桶」承载同一目的（spec §3.4-1 的推断），我们照**目的**、不照承载。
⛔ **只碰 `pending`**：`ready` 的行是业务数据，**一律不动**（A3 永久保留）。 ⚠️ **必须自己遍历租户**（Batch A 评审的 Critical）：`apps.business` 是 TENANT_APP， `business_orderscreenshot` 只在**租户 schema** 里，而 django-tenants 的连接默认停在 `public` （`django_tenants/postgresql_backend/base.py`）⇒ 只查当前连接的命令会直接
`ProgrammingError: relation "business_orderscreenshot" does not exist`，**清理根本不运行**。同仓先例：`apps/accounts/management/commands/report_accounts_without_games.py`。 ⚠️ **删对象失败时保留行**（偏离计划原稿，执行期裁定）：计划原写「只记 warning、不回滚行删除」，但这一行是那个对象的**唯一指针** ⇒ 行删了、对象还在，就变成**谁也找不到的永久孤儿** （正是评审刚指出的失败形态）。保留行 ⇒ 下次运行会重试。"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.business.models import OrderScreenshot
from apps.core.storage import get_storage

#: 未认领行的存活时长（小时）。spec §6.3 定的是 24 小时。
PENDING_TTL_HOURS = 24


class Command(BaseCommand):
    help = "清理超过 24 小时仍未被认领的订单截图（只碰 pending；ready 一律不动）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="只列出会被清理的行，不删任何东西"
        )
        parser.add_argument(
            "--schema", default=None,
            help="只清理这一个租户 schema（默认：全部非 public 租户，逐个进 tenant_context）",
        )
        parser.add_argument(
            "--hours", type=int, default=PENDING_TTL_HOURS,
            help=f"覆盖存活时长（默认 {PENDING_TTL_HOURS} 小时；下界 1）",
        )

    def handle(self, *args, **options):
        from django_tenants.utils import get_public_schema_name, tenant_context

        from apps.platform.models import Tenant

        hours = options["hours"]
        if hours < 1:
            # ⛔ `--hours 0` 会把刚落库、正在上传的 pending 全删掉（运维脚枪）⇒ 直接拒
            raise CommandError("--hours 必须 ≥ 1（0 会删掉正在上传的记录）")

        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        only = options["schema"]
        if only is not None:
            tenants = tenants.filter(schema_name=only)
            if not tenants.exists():
                raise CommandError(f"没有这个 schema：{only}")

        for tenant in tenants.order_by("schema_name"):
            # ⚠️ 必须逐个进 tenant_context：租户表只在各自的 schema 里（见模块 docstring）
            with tenant_context(tenant):
                self._purge_schema(tenant.schema_name, hours, options["dry_run"])

    def _purge_schema(self, schema_name: str, hours: int, dry_run: bool) -> None:
        cutoff = timezone.now() - timedelta(hours=hours)
        rows = list(
            OrderScreenshot.objects.filter(
                status=OrderScreenshot.STATUS_PENDING, created_at__lt=cutoff
            ).order_by("pk")
        )
        deleted = kept = 0
        storage = get_storage()

        for row in rows:
            if dry_run:
                self.stdout.write(f"[dry-run] {schema_name} 会清理 #{row.pk} {row.object_key}")
                continue
            try:
                # ⚠️ 本命令**只碰 `pending`**（下面每行都是刚签发、还没认领的随机段对象）⇒
                #    D-0316 的「共享前缀群码不删字节」闸**不适用**：这里删的永远是本租户自己的
                #    未认领对象，不存在链上共用（`group_qr` 的判据只服务已 `ready` 的群码行）。
                storage.delete(row.object_key)
            except Exception as exc:  # noqa: BLE001 —— 网络/权限/后端异常都要**保留行**以便重试
                kept += 1
                self.stderr.write(
                    f"跳过 #{row.pk}（{row.object_key}）：删对象失败 {type(exc).__name__}: {exc}"
                )
                continue
            row.delete()
            deleted += 1

        if dry_run:
            self.stdout.write(f"[dry-run] {schema_name} 命中 {len(rows)} 条，未删除任何东西")
        else:
            self.stdout.write(
                f"{schema_name}：删除 {deleted} 条，保留 {kept} 条（删对象失败）"
            )
