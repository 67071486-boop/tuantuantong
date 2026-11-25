"""一次性清理：批量下架「停绑孤儿」上家镜像卡（僵尸窗治理⑤；计划 `2026-11-05-binding-disable-batch-unlist.md` §3）。
判据/落写与生效点同一份（`orphan_hall_rows` 接管保护 + `batch_unlist_orphan_hall_rows`）；⛔ 默认 dry-run 只打印清单，`--apply` 才落写，幂等（重跑命中 0 行）。
⚠️ 必须自己遍历租户（`apps.business` 是 TENANT_APP，镜像行与台账在租户 schema；同 `rescue_gone_mirrors`）。"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.business import jidanpu_upstream
from apps.business.models import JidanpuUpstreamHallOrder

_LISTED = JidanpuUpstreamHallOrder.Status.LISTED


class Command(BaseCommand):
    help = "批量下架停绑孤儿镜像卡（默认 dry-run，--apply 才落写）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="真的落写（默认 dry-run：只打印会拆谁，不改任何行）",
        )
        parser.add_argument(
            "--schema", default=None,
            help="只处理这一个租户 schema（默认：全部非 public 租户，逐个进 tenant_context）",
        )

    def handle(self, *args, **options):
        from django_tenants.utils import get_public_schema_name, tenant_context

        from apps.platform.models import Tenant

        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        only = options["schema"]
        if only is not None:
            tenants = tenants.filter(schema_name=only)
            if not tenants.exists():
                raise CommandError(f"没有这个 schema：{only}")
        apply = options["apply"]
        total = 0
        for tenant in tenants.order_by("schema_name"):
            # ⚠️ 必须逐个进 tenant_context：租户表只在各自的 schema 里（见模块 docstring）。
            with tenant_context(tenant):
                total += self._unlist_schema(tenant.schema_name, apply=apply)
        verb = "已下架" if apply else "会下架（dry-run，未落写）"
        self.stdout.write(f"合计 {verb} {total} 张")

    def _unlist_schema(self, schema_name: str, *, apply: bool) -> int:
        """一个租户里的「枚举候选 → 打印真孤儿 →（--apply 才）落写」。dry-run 回计划数。"""
        candidates = (
            JidanpuUpstreamHallOrder.objects.filter(status=_LISTED)
            .filter(Q(owner_binding__isnull=True) | Q(owner_binding__enabled=False))
            .order_by("pk")
        )
        orphans = jidanpu_upstream.orphan_hall_rows(candidates)
        planned = 0
        for mirror in orphans:
            planned += 1
            owner = (
                f"#{mirror.owner_binding_id}"
                if mirror.owner_binding_id is not None else "无（已删）"
            )
            self.stdout.write(
                f"[{'apply' if apply else 'dry-run'}] {schema_name} #{mirror.pk} "
                f"{mirror.upstream_base_url}/{mirror.upstream_order_id} owner={owner}"
            )
        if not apply:
            return planned
        return jidanpu_upstream.batch_unlist_orphan_hall_rows(orphans)
