"""`dailiantong_repair_push_flags`：一次性修两类**历史坏标志**的推送行（默认 dry-run）。 # 先看命中多少（默认 dry-run，⛔ 不写） python manage.py dailiantong_repair_push_flags python manage.py dailiantong_repair_push_flags --tenant <schema> # 确认后再写 python manage.py dailiantong_repair_push_flags --apply ## 修哪两类（⛔ 都是精确组合，不用宽条件）
### ① `failed + is_active=True`（真机 `20261028105707877853`） ``` WHERE state = 'failed' AND is_active = true ``` 失败的行不是「在途」；但读面 / 重发闸都按 `is_active` 判「还有没有活跃平台单」 ⇒ 把它改成 `is_active=False`。`pending`（发送中）/ `unknown`（待核对）/ `sent` 一律不动。 ### ② 「远端已收口、本地还 `sent`/`pending`/`unknown`」（真机同单）
``` WHERE state IN ('pending','sent','unknown') AND (remote_status = 17 OR remote_cancel_status IN (12,15,16)) ``` `Status=17` 已结算 / `CancelStatus ∈ {12,15,16}` 撤销已完成 ⇒ 这条推送已经结束，但 `sent` 仍在 `ACTIVE_STATES` 里，会一直挡重发、界面也仍显示平台单。 ⇒ 收成 `state=closed` + `is_active=False` + `remote_note` 可读原因。 ⚠️ `Status=16 + CancelStatus=10`（撤销中但还没处理完）**不收**——它仍算在跑。 ## 边界 - `--tenant` 不给 ⇒ 本机全部租户（每行都在自己的 `tenant_context` 里）； - 默认 **dry-run**：只打印命中行数与 id；加 `--apply` 才写； - 两类都**幂等**：已修过的行不再命中。"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone
from django_tenants.utils import tenant_context

from apps.business.models import DailiantongPush
from apps.platform.models import Tenant


FINISHED_REMOTE = Q(remote_status=17) | Q(
    remote_cancel_status__in=(12, 15, 16)
)


class Command(BaseCommand):
    help = (
        "修复历史推送坏标志：failed 行仍是活跃 / 远端已收口但本地还 sent（默认 dry-run）"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default=None,
            help="只修一个租户 schema（不给 = 本机全部租户）",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="真的写库（⚠️ 默认只 dry-run 打印命中行数与 id）",
        )

    def _repair_tenant(self, tenant, *, apply: bool) -> tuple[int, int]:
        """修一个租户；返回 `(failed+active 命中, 已收口仍活跃 命中)`。"""
        with tenant_context(tenant):
            failed_qs = DailiantongPush.objects.filter(
                state=DailiantongPush.State.FAILED,
                is_active=True,
            )
            failed_count = failed_qs.count()
            finished_qs = DailiantongPush.objects.filter(
                state__in=DailiantongPush.ACTIVE_STATES,
            ).filter(FINISHED_REMOTE)
            finished_count = finished_qs.count()
            failed_ids = list(failed_qs.order_by("id").values_list("id", flat=True))
            finished_ids = list(
                finished_qs.order_by("id").values_list("id", flat=True)
            )

            if not apply:
                self.stdout.write(
                    f"{tenant.schema_name}: failed+active={failed_count}"
                    f" ids={failed_ids}；已收口仍活跃={finished_count}"
                    f" ids={finished_ids}（dry-run，未写）"
                )
                return failed_count, finished_count

            failed_updated = failed_qs.update(
                is_active=False, updated_at=timezone.now()
            )
            now = timezone.now()
            finished_updated = 0
            for push in finished_qs.order_by("id"):
                if push.remote_status == 17:
                    reason = "远端已结算（Status=17），这条推送已收口"
                else:
                    reason = (
                        "远端已撤销完成（CancelStatus="
                        f"{push.remote_cancel_status}），这条推送已收口"
                    )
                push.state = DailiantongPush.State.CLOSED
                push.is_active = False
                push.remote_note = reason[:200]
                push.save(
                    update_fields=[
                        "state",
                        "is_active",
                        "remote_note",
                        "updated_at",
                    ]
                )
                finished_updated += 1
            self.stdout.write(
                f"{tenant.schema_name}: 已修 failed+active={failed_updated}"
                f" ids={failed_ids}；已收口仍活跃={finished_updated}"
                f" ids={finished_ids}（now={now.isoformat()}）"
                if (failed_updated or finished_updated)
                else f"{tenant.schema_name}: 没有需要修的行"
            )
            return failed_count, finished_count

    def handle(self, *args, **options):
        schema = (options.get("tenant") or "").strip() or None
        apply = bool(options.get("apply"))

        # ⭐ M2：`public` 不是租户库（业务表不在那里）；且一个租户失败不能拖垮整批。
        tenants = Tenant.objects.exclude(schema_name="public").order_by("schema_name")
        if schema:
            tenants = tenants.filter(schema_name=schema)
            if not tenants.exists():
                raise CommandError(f"没有这个租户 schema：{schema}")

        failed_total = 0
        finished_total = 0
        failed_tenants: list[str] = []
        for tenant in tenants:
            try:
                failed_count, finished_count = self._repair_tenant(
                    tenant, apply=apply
                )
            except Exception as exc:  # noqa: BLE001 —— 逐租户隔离，继续其它租户
                failed_tenants.append(tenant.schema_name)
                self.stderr.write(
                    f"{tenant.schema_name}: 处理失败（{type(exc).__name__}），已跳过；"
                    "请单独用 --tenant 重试。"
                )
                continue
            failed_total += failed_count
            finished_total += finished_count

        mode = "已写库" if apply else "dry-run（未写库）"
        self.stdout.write(
            f"合计命中：failed+active={failed_total}，"
            f"已收口仍活跃={finished_total}；{mode}。"
            + (f" 失败租户={failed_tenants}" if failed_tenants else "")
        )
